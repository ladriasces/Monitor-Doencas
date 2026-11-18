"""
02_tratamento.py — Transforma os arquivos brutos (data/raw/) nos JSON lidos pelo site (docs/dados/).

  python 02_tratamento.py                  # todos os blocos
  python 02_tratamento.py --bloco srag     # só alguns

Blocos (cada um gera docs/dados/<bloco>.json):
  srag      SRAG por semana de início dos sintomas, Brasil e estados: agentes, óbitos, faixas etárias,
            comparação com 2023–2025 e tendência nas semanas já consolidadas
  arbo      dengue, chikungunya e zika: a semana mais recente em cada município (nível de alerta, casos,
            estimativa, incidência, Rt, clima) e as séries do Brasil e dos estados; as séries de cada
            município ficam em docs/dados/arbo/<UF>.json (até 60 semanas, acumuladas a cada atualização)
  clima     clima e saúde: chuva e temperatura do Monitor El Niño e a dengue em cada região imediata
  noticias  manchetes dos últimos 14 dias, com temas, estados e regiões

Se a coleta de uma fonte falhou, ou o bloco dá erro, o JSON anterior é mantido e o site mostra a data
da última atualização bem-sucedida (docs/dados/atualizacao.json).
"""
from __future__ import annotations

import argparse
import html
import re
import traceback
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

import pandas as pd
from defusedxml import ElementTree as ET   # XML vindo de fora: protegido contra entidades maliciosas

from saude_comum import (BRT, NOME_UF, RAW, REF, REGIAO_UF, RX_TEMAS, SAIDA, SRAG_FAIXAS, SRAG_GRUPOS, UFS, agora, chave,
                         codigo_se, gravar_json, inicio_semana, ler_json, r, semana_epi, url_segura)

# ================================================================ comum
def _pop() -> pd.DataFrame:
    """População por município (IBGE), com UF e região imediata."""
    mun = pd.read_csv(REF / "municipios.csv", dtype=str)
    pop = pd.read_csv(REF / "populacao.csv", dtype={"ibge": str})
    return mun.merge(pop[["ibge", "pop"]], on="ibge", how="left")


def _sem(ano: int, se: int) -> str:
    """Data (ISO) do domingo em que começa a semana epidemiológica."""
    return inicio_semana(ano, se).isoformat()


# ================================================================ SRAG
SRAG_REF_ANOS = [2023, 2024, 2025]   # anos de comparação: depois da fase de emergência da covid-19
SRAG_INCOMPLETAS = 4                 # as últimas semanas ainda recebem notificações (atraso de digitação)
TENDENCIA_LIMIAR = 10                # variação (%) a partir da qual a tendência é "alta" ou "queda"


def bloco_srag() -> dict:
    """Séries semanais de SRAG para o Brasil e cada estado, a partir das contagens agregadas na coleta
    (ano atual e anterior) e do histórico (data/ref/srag_hist.csv)."""
    info = ler_json(RAW / "srag" / "info.json")
    extracao = date.fromisoformat(info["extracao"])
    rec = pd.read_csv(RAW / "srag" / "srag_recente.csv", dtype={"uf": str})
    hist = pd.read_csv(REF / "srag_hist.csv", dtype={"uf": str})
    dados = pd.concat([hist[~hist.ano.isin(rec.ano.unique())], rec], ignore_index=True)
    dados["br"] = "BR"

    ano_fim, se_fim = semana_epi(extracao - timedelta(days=7))   # última semana já encerrada na extração
    pop_uf = _pop().groupby("uf")["pop"].sum()
    pop = {**pop_uf.to_dict(), "BR": int(pop_uf.sum())}
    grupos = list(SRAG_GRUPOS)

    # Eixo contínuo de semanas do ano anterior e do atual (a tendência atravessa a virada do ano)
    eixo = [(a, s) for a in (ano_fim - 1, ano_fim) for s in range(1, 54)
            if inicio_semana(a, s) < inicio_semana(a + 1, 1) and (a, s) <= (ano_fim, se_fim)]
    pos_fim = len(eixo) - 1
    pos_cons = pos_fim - SRAG_INCOMPLETAS            # última semana consolidada

    def territorio(df: pd.DataFrame, chave_pop: str) -> dict:
        tot = df.groupby(["ano", "se"]).agg(casos=("casos", "sum"), obitos=("obitos", "sum"))
        por_grupo = df.groupby(["ano", "se", "grupo"]).casos.sum()
        serie = lambda s, a, se: int(s.get((a, se), 0))   # noqa: E731
        atual = [(a, s) for a, s in eixo if a == ano_fim]
        casos_eixo = [serie(tot.casos, a, s) for a, s in eixo]

        # tendência: 3 últimas semanas consolidadas contra as 3 anteriores
        recentes, antes = sum(casos_eixo[pos_cons - 2:pos_cons + 1]), sum(casos_eixo[pos_cons - 5:pos_cons - 2])
        var = 100 * (recentes - antes) / antes if antes >= 10 else None
        tendencia = (None if var is None else "alta" if var >= TENDENCIA_LIMIAR else
                     "queda" if var <= -TENDENCIA_LIMIAR else "estavel")

        # comparação: mesmas semanas em 2023–2025 (mínimo, média e máximo)
        ref = {a: [serie(tot.casos, a, s) for s in range(1, 54)] for a in SRAG_REF_ANOS if a != ano_fim}
        faixa_ref = {"min": [min(v[i] for v in ref.values()) for i in range(53)],
                     "media": [r(sum(v[i] for v in ref.values()) / len(ref), 1) for i in range(53)],
                     "max": [max(v[i] for v in ref.values()) for i in range(53)]} if ref else None

        # agentes e faixas etárias nas últimas 8 semanas consolidadas
        janela = set(eixo[max(0, pos_cons - 7):pos_cons + 1])
        rec8 = df[[(a, s) in janela for a, s in zip(df.ano, df.se)]]
        agentes = rec8.groupby("grupo").casos.sum().reindex(grupos, fill_value=0)
        faixas = rec8.groupby("faixa").casos.sum().reindex(SRAG_FAIXAS, fill_value=0)
        virus = agentes[["covid", "flu_a", "flu_b", "flu_nd", "vsr", "outros_virus"]]
        identificados = int(virus.sum())
        # o principal entre os vírus com nome (covid-19, influenza A e B, VSR); com poucos casos, não se aponta nenhum
        nomeados = agentes[["covid", "flu_a", "flu_b", "vsr"]]
        principal = nomeados.idxmax() if nomeados.max() >= 5 else None

        cons4 = sum(casos_eixo[pos_cons - 3:pos_cons + 1])
        no_ano = df[df.ano == ano_fim]
        return {
            "casos": {g: [int(por_grupo.get((a, s, g), 0)) for a, s in atual] for g in grupos},
            "obitos": [serie(tot.obitos, a, s) for a, s in atual],
            "anterior": [serie(tot.casos, ano_fim - 1, s) for s in range(1, 54)],
            "ref": faixa_ref,
            "resumo": {
                "casos_ano": int(no_ano.casos.sum()), "obitos_ano": int(no_ano.obitos.sum()),
                "casos_4s": cons4, "inc_4s": r(1e5 * cons4 / pop[chave_pop], 2) if pop.get(chave_pop) else None,
                "var_pct": r(var, 0), "tendencia": tendencia,
                "agentes_8s": {g: int(v) for g, v in agentes.items()},
                "faixas_8s": {f: int(v) for f, v in faixas.items()},
                "principal": principal, "identificados_8s": identificados,
            },
        }

    terr = {"BR": territorio(dados, "BR")}
    for uf in UFS:
        terr[uf] = territorio(dados[dados.uf == uf], uf)
    return {
        "extracao": info["extracao"], "ano": ano_fim, "se_fim": se_fim, "se_consolidada": eixo[pos_cons][1],
        "ano_consolidada": eixo[pos_cons][0], "incompletas": SRAG_INCOMPLETAS,
        "inicio_semanas": [_sem(ano_fim, s) for a, s in eixo if a == ano_fim],
        "ref_anos": [a for a in SRAG_REF_ANOS if a != ano_fim], "grupos": SRAG_GRUPOS, "faixas": SRAG_FAIXAS,
        "descartadas": info.get("descartadas", 0), "territorios": terr,
    }


# ================================================================ arboviroses
DOENCAS_ARBO = {"dengue": "Dengue", "chikungunya": "Chikungunya", "zika": "Zika"}
ARBO_HIST = 60                       # semanas guardadas por município (cobre a mesma semana do ano anterior)
ARBO_COLS = ["nivel", "casos", "est", "est_min", "est_max", "inc", "rt", "p_rt1", "receptivo", "transmissao", "temp", "umid"]


def _arbo_historico(uf: str) -> pd.DataFrame:
    """Séries municipais já publicadas (docs/dados/arbo/<UF>.json), em formato longo."""
    arq = SAIDA / "arbo" / f"{uf}.json"
    if not arq.exists():
        return pd.DataFrame(columns=["doenca", "ibge", "se", "casos", "est", "nivel"])
    d = ler_json(arq)
    linhas = []
    for doenca, bloco in d.get("doencas", {}).items():
        for ibge, (c, e, n) in bloco.items():
            linhas += [(doenca, ibge, se, c[i], e[i], n[i]) for i, se in enumerate(d["ses"]) if c[i] is not None]
    return pd.DataFrame(linhas, columns=["doenca", "ibge", "se", "casos", "est", "nivel"])


def bloco_arbo() -> dict:
    """Junta a coleta (últimas semanas, todos os municípios) às séries já publicadas e monta os resumos."""
    mun = _pop()
    novos = []
    for doenca in DOENCAS_ARBO:
        arq = RAW / "arbo" / f"{doenca}.csv"
        if arq.exists():
            df = pd.read_csv(arq, dtype={"ibge": str})
            df["doenca"] = doenca
            novos.append(df)
    if not novos:
        raise RuntimeError("nenhum arquivo de arboviroses na coleta")
    novo = pd.concat(novos, ignore_index=True).dropna(subset=["se"])
    novo["se"] = novo.se.astype(int)
    novo = novo.merge(mun[["ibge", "uf"]], on="ibge", how="inner")

    # séries municipais: histórico publicado + coleta (a coleta prevalece: o InfoDengue revisa as semanas recentes)
    hist = pd.concat([_arbo_historico(uf).assign(uf=uf) for uf in UFS], ignore_index=True)
    serie = pd.concat([hist, novo[["doenca", "ibge", "se", "casos", "est", "nivel", "uf"]]], ignore_index=True)
    serie = serie.drop_duplicates(["doenca", "ibge", "se"], keep="last")
    ses = sorted(serie.se.unique())[-ARBO_HIST:]
    serie = serie[serie.se.isin(ses)]

    # arquivos por estado (lidos pelo site quando a pessoa escolhe um município)
    (SAIDA / "arbo").mkdir(parents=True, exist_ok=True)
    for uf, g in serie.groupby("uf"):
        doencas = {}
        for doenca, gd in g.groupby("doenca"):
            piv = {c: gd.pivot(index="ibge", columns="se", values=c).reindex(columns=ses) for c in ["casos", "est", "nivel"]}
            doencas[doenca] = {ib: [[None if v != v else int(v) for v in piv[c].loc[ib]] for c in ["casos", "est", "nivel"]]
                               for ib in piv["casos"].index}
        gravar_json({"ses": [int(s) for s in ses], "doencas": doencas}, SAIDA / "arbo" / f"{uf}.json")

    saida = {"doencas": DOENCAS_ARBO, "cols": ARBO_COLS, "ses": [int(s) for s in ses],
             "inicio_semanas": [_sem(s // 100, s % 100) for s in ses]}
    for doenca in DOENCAS_ARBO:
        nd = novo[novo.doenca == doenca]
        if nd.empty:
            continue
        se_ult = int(nd.se.max())
        ult = nd[nd.se == se_ult].drop_duplicates("ibge", keep="last")
        sd = serie[serie.doenca == doenca]
        pop_mun = mun.set_index("ibge")["pop"]

        def resumo_territorio(filtro_ult: pd.Series, filtro_serie: pd.Series) -> dict:
            u, s = ult[filtro_ult], sd[filtro_serie]
            por_se = s.groupby("se")[["casos", "est"]].sum().reindex(ses)
            ano = se_ult // 100
            no_ano = por_se[[x // 100 == ano for x in por_se.index]]
            p = pop_mun.reindex(u.ibge).sum()
            ult4 = por_se.est.dropna().tail(4).sum()
            return {"niveis": [int((u.nivel == n).sum()) for n in (1, 2, 3, 4)],
                    "n_mun": int(len(u)), "c": [None if v != v else int(v) for v in por_se.casos],
                    "e": [None if v != v else int(v) for v in por_se.est],
                    "casos_ano": int(no_ano.casos.sum()), "semanas_ano": int(no_ano.casos.notna().sum()),
                    "inc_4s": r(1e5 * ult4 / p, 1) if p else None}

        terr = {"BR": resumo_territorio(pd.Series(True, index=ult.index), pd.Series(True, index=sd.index))}
        for uf in UFS:
            if (ult.uf == uf).any():
                terr[uf] = resumo_territorio(ult.uf == uf, sd.uf == uf)
        saida[doenca] = {
            "se": se_ult, "inicio": _sem(se_ult // 100, se_ult % 100),
            "versao": str(ult.versao.dropna().max()) if "versao" in ult and ult.versao.notna().any() else None,
            "mun": {t.ibge: [None if (v := getattr(t, c)) != v else (int(v) if c in ("nivel", "casos", "est", "est_min", "est_max",
                                                                                    "receptivo", "transmissao") else r(v, 2))
                             for c in ARBO_COLS] for t in ult.itertuples()},
            "territorios": terr,
        }
    return saida


# ================================================================ clima e saúde
def bloco_clima() -> dict:
    """Por região imediata: chuva e temperatura dos últimos 30 e 90 dias contra 1991–2020 (Monitor El Niño)
    e a dengue nas últimas 4 semanas (estimativa do InfoDengue por 100 mil habitantes)."""
    clima = ler_json(RAW / "elnino" / "clima.json")
    enso = ler_json(RAW / "elnino" / "enso.json")
    cols = clima["cols"]
    idx = {c: cols.index(c) for c in ["p30_anom", "t30_anom", "p90_anom", "t90_anom"]}

    dengue = {}
    arq_arbo = SAIDA / "arbo.json"
    if arq_arbo.exists():
        mun = _pop()
        linhas = []
        for uf in UFS:
            a = SAIDA / "arbo" / f"{uf}.json"
            if not a.exists():
                continue
            d = ler_json(a)
            for ib, (c, e, n) in d["doencas"].get("dengue", {}).items():
                ult = [v for v in e if v is not None][-4:]
                linhas.append((ib, sum(ult), len(ult)))
        if linhas:
            df = pd.DataFrame(linhas, columns=["ibge", "est4", "n"]).merge(mun, on="ibge")
            atual = ler_json(arq_arbo).get("dengue", {}).get("mun", {})
            ir = ARBO_COLS.index("receptivo")
            df["receptivo"] = [1 if (atual.get(ib) or [None] * len(ARBO_COLS))[ir] else 0 for ib in df.ibge]
            ag = df.groupby("imediata").agg(est4=("est4", "sum"), habitantes=("pop", "sum"), rec=("receptivo", "mean"),
                                            n=("ibge", "size"))
            dengue = {i: [r(1e5 * t.est4 / t.habitantes, 1) if t.habitantes else None, r(100 * t.rec, 0), int(t.n)]
                      for i, t in ag.iterrows()}

    regioes = {}
    for rid, v in clima["regioes"].items():
        regioes[rid] = [v[idx["p30_anom"]], v[idx["t30_anom"]], v[idx["p90_anom"]], v[idx["t90_anom"]],
                        *(dengue.get(rid) or [None, None, 0])]
    return {"clima_fim": clima.get("fim"), "enso": enso.get("atual"), "alerta": (enso.get("cpc") or {}).get("alerta_pt"),
            "cols": ["p30_anom", "t30_anom", "p90_anom", "t90_anom", "dengue_inc4", "pct_receptivo", "n_mun"],
            "regioes": regioes}


# ================================================================ notícias
# RSS (arquivo em data/raw/noticias/<nome>.xml -> nome exibido). Só entram manchetes com algum tema do monitor.
NOME_FONTE = {"agencia_brasil": "Agência Brasil", "agencia_fiocruz": "Agência Fiocruz", "onu_news": "ONU News"}
FONTES_PERMITIDAS = set(NOME_FONTE.values())
DOMINIOS_FONTE = {"Agência Brasil": ("https://agenciabrasil.ebc.com.br/",), "ONU News": ("https://news.un.org/",),
                  "Agência Fiocruz": ("https://agencia.fiocruz.br/", "https://portal.fiocruz.br/")}
NOMES_TEMAS = {"arboviroses": "Dengue e arboviroses", "respiratorias": "Doenças respiratórias",
               "clima-saude": "Clima e saúde", "vacinacao": "Vacinação"}
# Nomes de exibição dos veículos mais frequentes no GDELT (os demais aparecem pelo endereço do site)
VEICULOS = {"g1.globo.com": "g1", "oglobo.globo.com": "O Globo", "valor.globo.com": "Valor",
            "agenciabrasil.ebc.com.br": "Agência Brasil", "correiobraziliense.com.br": "Correio Braziliense",
            "folha.uol.com.br": "Folha de S.Paulo", "estadao.com.br": "Estadão", "cnnbrasil.com.br": "CNN Brasil",
            "metropoles.com": "Metrópoles", "terra.com.br": "Terra", "uol.com.br": "UOL", "noticias.uol.com.br": "UOL",
            "jc.ne10.uol.com.br": "Jornal do Commercio", "diariodepernambuco.com.br": "Diario de Pernambuco",
            "diariodonordeste.verdesmares.com.br": "Diário do Nordeste", "opovo.com.br": "O Povo",
            "gauchazh.clicrbs.com.br": "GZH", "poder360.com.br": "Poder360", "istoe.com.br": "IstoÉ",
            "band.uol.com.br": "Band", "r7.com": "R7", "sbtnews.sbt.com.br": "SBT News", "exame.com": "Exame",
            "brasildefato.com.br": "Brasil de Fato", "cartacapital.com.br": "CartaCapital"}


def bloqueados() -> set[str]:
    """Domínios que não podem aparecer (agregadores e veículos que pedirem para sair): data/ref/noticias_bloqueio.txt."""
    arq = REF / "noticias_bloqueio.txt"
    linhas = arq.read_text(encoding="utf-8").splitlines() if arq.exists() else []
    return {x.strip().lower() for x in linhas if x.strip() and not x.startswith("#")}


def host(link: str) -> str:
    """Domínio de um link, sem "www." (ex.: "g1.globo.com")."""
    return (urlparse(link).hostname or "").lower().removeprefix("www.")


def nome_veiculo(dominio: str) -> str:
    """Nome de exibição do veículo; sem nome cadastrado, o próprio domínio."""
    d = dominio.lower().removeprefix("www.")
    return VEICULOS.get(d) or VEICULOS.get(".".join(d.split(".")[-3:])) or d


def link_da_fonte(fonte: str, link: str | None, origem: str = "rss", bloq: frozenset = frozenset()) -> bool:
    """RSS: o link tem que ser do site oficial da fonte. GDELT: o domínio tem que corresponder ao veículo
    e não pode estar na lista de bloqueio."""
    if not link:
        return False
    if origem == "gdelt":
        h = host(link)
        return bool(h) and not any(h == b or h.endswith("." + b) for b in bloq) and nome_veiculo(h) == fonte
    return link.startswith(DOMINIOS_FONTE.get(fonte, ("\0",)))


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _itens_feed(arq):
    """Itens de um feed RSS 2.0, RSS 1.0 (RDF) ou Atom, lido com o defusedxml (XML vem de fora)."""
    try:
        raiz = ET.parse(arq).getroot()
    except Exception:  # noqa: BLE001 — feed corrompido ou fora do ar: ignora este arquivo
        return
    for it in raiz.iter():
        if _local(it.tag) not in ("item", "entry"):
            continue
        filhos = {_local(c.tag): c for c in it}
        titulo = html.unescape((filhos.get("title").text if "title" in filhos else "") or "").strip()
        link_el = filhos.get("link")
        link = (link_el.get("href") or link_el.text or "") if link_el is not None else ""
        data_txt = next((filhos[k].text for k in ("pubDate", "date", "published", "updated")
                         if k in filhos and filhos[k].text), None)
        try:
            quando = parsedate_to_datetime(data_txt) if data_txt and "," in data_txt else \
                datetime.fromisoformat(data_txt.replace("Z", "+00:00"))
            quando = quando.astimezone(BRT)
        except (TypeError, ValueError, AttributeError):
            continue
        link = url_segura(link)
        if titulo and link:
            yield {"titulo": " ".join(titulo.split()), "link": link, "data": quando}


def limpar_titulo(titulo: str, dominio: str) -> str:
    """Tira o nome do site que alguns veículos põem na manchete ("Diário do Amapá – ...", "... | MidiaNews")."""
    rotulo = dominio.split(".")[1] if dominio.startswith(("www.", "g1.")) else dominio.split(".")[0]
    base = re.sub(r"[^a-z0-9]", "", chave(rotulo))
    titulo = " ".join(titulo.split())
    for _ in range(2):
        titulo = re.sub(r"\s+\|\s+[^|]{2,30}$", "", titulo)
    for sep in (" | ", " – ", " - "):
        partes = titulo.split(sep)
        if len(partes) > 1:
            fim, ini = re.sub(r"[^a-z0-9]", "", chave(partes[-1])), re.sub(r"[^a-z0-9]", "", chave(partes[0]))
            if len(partes[-1]) <= 45 and base and (base[:6] in fim or fim[:6] in base):
                titulo = sep.join(partes[:-1])
            elif len(partes[0]) <= 30 and base and (base[:6] in ini or ini[:6] in base):
                titulo = sep.join(partes[1:])
    return titulo.strip()


CAPITAIS = {"RO": "Porto Velho", "AC": "Rio Branco", "AM": "Manaus", "RR": "Boa Vista", "PA": "Belém", "AP": "Macapá",
            "TO": "Palmas", "MA": "São Luís", "PI": "Teresina", "CE": "Fortaleza", "RN": "Natal", "PB": "João Pessoa",
            "PE": "Recife", "AL": "Maceió", "SE": "Aracaju", "BA": "Salvador", "MG": "Belo Horizonte", "ES": "Vitória",
            "RJ": "Rio de Janeiro", "SP": "São Paulo", "PR": "Curitiba", "SC": "Florianópolis", "RS": "Porto Alegre",
            "MS": "Campo Grande", "MT": "Cuiabá", "GO": "Goiânia", "DF": "Brasília"}
AMBIGUAS = {"RN", "ES", "TO", "BA"}   # capitais que também são palavras comuns só contam com contexto
DEMONIMOS = {"PE": "pernambucan", "CE": "cearense", "BA": "baian", "PB": "paraiban", "RN": "potiguar", "AL": "alagoan",
             "PI": "piauiense", "MA": "maranhense", "SE": "sergipan", "AM": "amazonense", "PA": "paraense", "RS": "gaúch",
             "SC": "catarinense", "PR": "paranaense", "MG": "mineir", "SP": "paulista", "RJ": "fluminense", "ES": "capixaba",
             "GO": "goian", "MT": "mato-grossense", "MS": "sul-mato-grossense", "TO": "tocantinense", "RO": "rondoniense",
             "AC": "acrean", "AP": "amapaense", "RR": "roraimense", "DF": "brasiliense"}


def _padroes_uf() -> dict:
    """Expressões (sensíveis a acento e maiúsculas) que indicam que o título fala de um estado."""
    pad = {}
    for uf, nome in NOME_UF.items():
        nm = re.escape(nome) + (r"(?! do Sul)" if uf == "MT" else "")
        partes = [rf"\b{nm}\b", rf"\({uf}\)", rf"[-/]\s?{uf}\b", rf"\b(no|na|em|do|da|pelo|pela) {uf}\b", rf"^{uf} "]
        if uf == "MG":
            partes.append(r"\bBH\b")
        cap = CAPITAIS[uf]
        if uf not in AMBIGUAS:
            partes.append(rf"\b{re.escape(cap)}\b(?! do Sul)" if uf == "MS" else rf"\b{re.escape(cap)}\b")
        else:
            partes.append(rf"\b(em|de|para) {re.escape(cap)}\b(?! *[a-z])")
        partes.append(rf"(?i:(?<!sul-)\b{re.escape(DEMONIMOS[uf])})")
        pad[uf] = re.compile("|".join(partes))
    return pad


PALAVRAS_COMUNS = {"Brasil", "Bonito", "Formosa", "Esperança", "Vitória", "Paraíso", "Cruzeiro", "Liberdade", "Alegria",
                   "Saudade", "Harmonia", "Planalto", "Floresta", "Pontal", "Caridade", "Primavera", "Independência",
                   "União", "Progresso", "Triunfo", "Amparo", "Aurora", "Mirante", "Central", "General", "Presidente",
                   "Governador", "Fortuna", "Glória", "Vigia", "Socorro", "Salvaterra", "Coronel", "Pedreira", "Sertão",
                   "Paulista", "Tabuleiro", "Cachoeira", "Tapera", "Serrano", "Itapeva", "Pacatuba", "Palestina",
                   "Jardim", "Princesa", "Ouvidor", "Cristal", "Sapucaia", "Guarani", "Anchieta", "Saúde", "Gripe",
                   "Campos Gerais", "Mata", "Agreste", "Cariri", "Pantanal", "Serra"}


def _padrao_municipios() -> tuple[re.Pattern, dict]:
    """Expressão que acha nomes de municípios (de um único estado) na manchete e o dicionário nome -> estado."""
    mun = pd.read_csv(REF / "municipios.csv", dtype=str)
    por_nome = mun.groupby("nome").uf.agg(lambda s: set(s))
    nomes = {n: next(iter(u)) for n, u in por_nome.items()
             if len(u) == 1 and n not in PALAVRAS_COMUNS and (" " in n or len(n) >= 5)}
    rx = re.compile(r"(?<![\w-])(" + "|".join(sorted(map(re.escape, nomes), key=len, reverse=True)) + r")(?![\w-])")
    return rx, nomes


SIGLAS_G1 = {uf.lower() for uf in NOME_UF}


def ufs_da_url(link: str) -> set[str]:
    """No g1, o estado vem no endereço (g1.globo.com/pe/pernambuco/...)."""
    m = re.match(r"https?://g1\.globo\.com/([a-z]{2})/", link)
    return {m.group(1).upper()} if m and m.group(1) in SIGLAS_G1 else set()


RX_REGIOES = {"Norte": re.compile(r"\b(no|do|na|o|ao|pelo) Norte\b(?! d[aoe])"),
              "Nordeste": re.compile(r"\bNordeste\b|\bSemiárido\b|\bSertão\b"),
              "Centro-Oeste": re.compile(r"\bCentro-Oeste\b|\bPantanal\b"),
              "Sudeste": re.compile(r"\bSudeste\b(?! (Asiático|da Ásia))"),
              "Sul": re.compile(r"\b(no|do|na|o|ao|pelo|e) Sul\b(?! d[aoe]|-)|\bSul do (país|Brasil)\b"),
              "Amazônia": re.compile(r"\bAmazônia\b|\bAmazonas\b|\bamazônic")}


def bloco_noticias() -> dict:
    """Junta as manchetes novas às já publicadas e mantém os últimos 14 dias."""
    pasta = RAW / "noticias"
    limite = agora() - timedelta(days=14)
    padroes = _padroes_uf()
    rx_mun, uf_mun = _padrao_municipios()
    bloq = frozenset(bloqueados())
    itens = {}

    def add(titulo, link, quando, fonte, origem="rss"):
        """Inclui a manchete se for dos últimos 14 dias, de fonte autorizada e de um tema do monitor;
        repetidas (o mesmo texto de agência em vários veículos) entram uma vez só."""
        if quando < limite or quando > agora() + timedelta(hours=1) or not link_da_fonte(fonte, link, origem, bloq):
            return
        temas = sorted(k for k, rx in RX_TEMAS.items() if rx.search(titulo))
        k = chave(titulo)[:70]
        if not temas or k in itens:
            return
        ufs = sorted({sg for sg, rx in padroes.items() if rx.search(titulo)} | ufs_da_url(link)
                     | {uf_mun[m] for m in rx_mun.findall(titulo)})
        regioes = sorted({reg for reg, rx in RX_REGIOES.items() if rx.search(titulo)})
        itens[k] = {"titulo": titulo, "fonte": fonte, "link": link, "data": quando, "temas": temas, "ufs": ufs,
                    "regioes": regioes, "origem": origem}

    for arq in sorted(pasta.glob("*.xml")):
        fonte = next((v for k, v in NOME_FONTE.items() if arq.stem.startswith(k)), None)
        if fonte:
            for it in _itens_feed(arq):
                add(it["titulo"], it["link"], it["data"], fonte)
    arq = pasta / "gdelt.json"
    for it in (ler_json(arq) if arq.exists() else []):
        link = url_segura(it.get("link"))
        if not link:
            continue
        quando = datetime.strptime(it["data"], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc).astimezone(BRT)
        add(limpar_titulo(it["titulo"], host(link)), link, quando, nome_veiculo(host(link)), "gdelt")
    anterior = SAIDA / "noticias.json"
    if anterior.exists():
        for x in ler_json(anterior).get("noticias", []):
            link = url_segura(x.get("link"))
            origem = x.get("origem", "rss")
            if link and (origem == "gdelt" or x.get("fonte") in FONTES_PERMITIDAS):
                add(x["titulo"], link, datetime.fromisoformat(x["data"]), x["fonte"], origem)
    lista = sorted(itens.values(), key=lambda x: x["data"], reverse=True)[:4000]
    return {"temas": NOMES_TEMAS, "noticias": [{**x, "data": x["data"].isoformat(timespec="minutes")} for x in lista]}


# ================================================================ execução
# Bloco -> (função, fontes da coleta de que ele depende). O bloco "clima" usa o arbo.json já gerado.
BLOCOS = {"srag": (bloco_srag, ["srag"]), "arbo": (bloco_arbo, ["arbo"]), "clima": (bloco_clima, ["elnino"]),
          "noticias": (bloco_noticias, ["noticias"])}


def main(blocos: list[str]) -> None:
    """Gera o JSON de cada bloco pedido. Se a coleta de alguma fonte do bloco falhou, ou o bloco dá erro,
    mantém o JSON anterior e registra isso em docs/dados/atualizacao.json (o site mostra a data)."""
    arq_at = SAIDA / "atualizacao.json"
    at = ler_json(arq_at) if arq_at.exists() else {}
    coleta = ler_json(RAW / "status_coleta.json") if (RAW / "status_coleta.json").exists() else {}
    for nome in blocos:
        fn, fontes = BLOCOS[nome]
        print(f"== {nome}")
        tentado = agora().isoformat(timespec="minutes")   # usado pela agenda (01_coleta.py --plano)
        at.setdefault(nome, {})["tentado"] = tentado
        if any(not coleta.get(f, {}).get("ok", False) for f in fontes):
            print("  coleta falhou: mantendo os dados anteriores")
            at[nome]["ok_hoje"] = False
            continue
        if (SAIDA / f"{nome}.json").exists() and all(coleta[f].get("novo") is False for f in fontes):
            print("  a fonte ainda não publicou dados novos: mantendo os atuais")
            at[nome]["ok_hoje"] = True
            continue
        try:
            dados = fn()
            gravar_json(dados, SAIDA / f"{nome}.json")
            at[nome] = {"ok_hoje": True, "atualizado": tentado, "tentado": tentado}
            print(f"  ok ({(SAIDA / f'{nome}.json').stat().st_size / 1e3:.0f} kB)")
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            at[nome]["ok_hoje"] = False
    at["gerado"] = agora().isoformat(timespec="minutes")
    gravar_json(at, arq_at, compacto=False)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Tratamento dos dados do Monitor de Doenças por Território.")
    ap.add_argument("--bloco", nargs="*", default=list(BLOCOS), choices=list(BLOCOS))
    main(ap.parse_args().bloco)
