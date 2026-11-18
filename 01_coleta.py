"""
01_coleta.py — Baixa as fontes abertas para data/raw/ (roda no GitHub Actions, ou à mão).

  python 01_coleta.py                       # todas as fontes
  python 01_coleta.py --fonte srag arbo     # só algumas
  python 01_coleta.py --arbo-semanas 60     # carga inicial das arboviroses (padrão: 12 semanas)
  python 01_coleta.py --plano [--completo]  # só diz o que está na hora de atualizar (usado pelo GitHub Actions)

Fontes:
  srag      SIVEP-Gripe (Ministério da Saúde, Portal de Dados Abertos do SUS): arquivos do ano atual e do
            anterior. Cada arquivo é reduzido a contagens por UF × semana × agente × faixa etária e apagado
            em seguida: nenhum dado individual fica guardado.
  arbo      InfoDengue (Fiocruz/FGV), pela API do Mosqlimate: indicadores semanais de dengue, chikungunya e
            zika em todos os municípios (casos, estimativa, incidência, nível de alerta, Rt, clima).
            Precisa da chave MOSQLIMATE_KEY (variável de ambiente ou arquivo .mosqlimate_key, fora do repositório).
  elnino    Monitor El Niño (LADRI): situação do fenômeno e chuva/temperatura por região imediata, já publicadas.
  noticias  GDELT (manchetes de veículos brasileiros) e RSS oficiais (Agência Brasil Saúde, Agência Fiocruz, ONU News Saúde)

As fontes de saúde publicam dados novos uma vez por semana. Para não baixar à toa, a coleta confere antes
se há extração nova (SRAG: data no nome do arquivo; InfoDengue: semana e versão do modelo).
Uma fonte que falha não interrompe as outras: o 02_tratamento.py mantém os dados anteriores dela.
"""
from __future__ import annotations

import argparse
import csv
import html
import io
import json
import re
import time
import traceback
import zipfile
from datetime import date, datetime, timedelta, timezone

import pandas as pd

from saude_comum import (RAW, REF, RX_TEMAS, SAIDA, UFS, agora, agregar_srag, arquivos_srag, baixar, chave_api,
                         gravar_json, inicio_semana, ler_json, semana_epi)

csv.field_size_limit(2**31 - 1)   # campos do GDELT podem ser muito longos

MOSQLIMATE = "https://api.mosqlimate.org/api/datastore/infodengue/"
DOENCAS_ARBO = ["dengue", "chikungunya", "zika"]
ARBO_SEMANAS = 12          # semanas buscadas a cada atualização (o InfoDengue revisa as semanas recentes)
POR_PAGINA = 300           # registros por página (máximo aceito pela API em 17/11/2026)
PAUSA = 2.0                # segundos entre consultas: a API do Mosqlimate limita o número de consultas por usuário
ELNINO = "https://ladriasces.github.io/Monitor-El-Nino/dados"
GDELT = "https://data.gdeltproject.org/gdeltv2"
GDELT_HORAS = 3


# ---------------------------------------------------------------- SRAG
def srag(forcar: bool = False) -> bool:
    """Ano atual e anterior do SIVEP-Gripe. Devolve False (sem novidade) se a extração já está publicada."""
    pasta = RAW / "srag"
    anos = [agora().year - 1, agora().year]
    publicados = arquivos_srag()
    anterior = ler_json(SAIDA / "srag.json").get("extracao") if (SAIDA / "srag.json").exists() else None
    novos = {a: publicados[a] for a in anos if a in publicados}
    if not novos:
        raise RuntimeError(f"SIVEP-Gripe: nenhum arquivo de {anos} no portal")
    extracao = max(i["data"] for i in novos.values()).isoformat()
    if anterior == extracao and not forcar:
        print(f"  SRAG: extração de {extracao} já publicada; nada a baixar")
        return False
    partes, descartadas = [], 0
    for ano, info in novos.items():
        arq = pasta / f"INFLUD{ano % 100:02d}.parquet"
        baixar(info["url"], arq, timeout=600)
        try:
            ag, desc = agregar_srag(arq, ano)
        finally:
            arq.unlink(missing_ok=True)   # o arquivo individual não fica guardado
        partes.append(ag)
        descartadas += desc
        print(f"  SRAG {ano}: {ag.casos.sum():,} casos (extração {info['data']:%d/%m/%Y})".replace(",", "."))
    pd.concat(partes, ignore_index=True).to_csv(pasta / "srag_recente.csv", index=False)
    gravar_json({"extracao": extracao, "arquivos": {str(a): i["url"] for a, i in novos.items()}, "descartadas": descartadas},
                pasta / "info.json", compacto=False)
    return True


# ---------------------------------------------------------------- arboviroses (InfoDengue / Mosqlimate)
CAMPOS_ARBO = {   # nome no InfoDengue -> nome usado aqui
    "SE": "se", "casos": "casos", "casos_est": "est", "casos_est_min": "est_min", "casos_est_max": "est_max",
    "p_inc100k": "inc", "nivel": "nivel", "Rt": "rt", "p_rt1": "p_rt1", "receptivo": "receptivo",
    "transmissao": "transmissao", "tempmed": "temp", "umidmed": "umid", "pop": "pop", "versao_modelo": "versao",
}


def normalizar_arbo(itens: list[dict], geocode: str | None = None) -> pd.DataFrame:
    """Registros do InfoDengue (API do Mosqlimate ou alertcity) -> tabela com os nomes usados aqui."""
    linhas = []
    for it in itens:
        cod = str(it.get("municipio_geocodigo") or geocode or "")
        linha = {"ibge": cod}
        for orig, novo in CAMPOS_ARBO.items():
            linha[novo] = it.get(orig)
        linhas.append(linha)
    df = pd.DataFrame(linhas, columns=["ibge", *CAMPOS_ARBO.values()])
    for c in ["se", "casos", "est", "est_min", "est_max", "inc", "nivel", "rt", "p_rt1", "receptivo", "transmissao",
              "temp", "umid", "pop"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def _pagina_arbo(chave: str, doenca: str, inicio: date, fim: date, pagina: int, uf: str | None = None,
                 por_pagina: int | None = None) -> dict:
    params = {"disease": doenca, "start": inicio.isoformat(), "end": fim.isoformat(),
              "page": pagina, "per_page": por_pagina or POR_PAGINA}
    if uf:
        params["uf"] = uf
    # até 6 tentativas: com o limite de consultas (429), as esperas chegam a 1, 2, 4, 8 e 15 minutos
    return json.loads(baixar(MOSQLIMATE, params=params, headers={"X-UID-Key": chave}, timeout=180, tentativas=6))


def _consulta_arbo(chave: str, doenca: str, inicio: date, fim: date, uf: str) -> list[dict]:
    """Uma consulta (todas as páginas) conferida: a API não ordena os resultados, e consultas grandes
    paginadas repetem umas linhas e perdem outras (visto em 17/11/2026: 38% de repetição na dengue de 2026
    inteira). Por isso cada consulta é pequena e só é aceita se as linhas distintas somarem o total informado."""
    for tentativa in range(3):
        primeira = _pagina_arbo(chave, doenca, inicio, fim, 1, uf=uf)
        pag = primeira.get("pagination", {})
        total, paginas = int(pag.get("total_items", 0)), int(pag.get("total_pages", 1))
        itens = list(primeira.get("items", []))
        for p in range(2, paginas + 1):
            time.sleep(PAUSA)
            itens += _pagina_arbo(chave, doenca, inicio, fim, p, uf=uf).get("items", [])
        unicos = {(it.get("municipio_geocodigo"), it.get("SE")): it for it in itens}
        if len(unicos) == total:
            return list(unicos.values())
        print(f"    {doenca} {uf} {inicio}: {len(unicos)} de {total} linhas; repetindo", flush=True)
        time.sleep(PAUSA * (tentativa + 2))
    raise RuntimeError(f"InfoDengue: consulta incompleta ({doenca}, {uf}, {inicio}–{fim})")


def _arbo_brasil(chave: str, doenca: str, inicio: date, fim: date) -> list[dict]:
    """Todas as semanas de `inicio` (domingo) até antes de `fim`, estado por estado, uma consulta de cada vez.
    Cada consulta cobre tantas semanas quantas couberem em ~1 página (estados grandes: uma semana por vez)."""
    n_mun = pd.read_csv(REF / "municipios.csv", dtype=str).groupby("uf").size()
    semanas = (fim - inicio).days // 7
    itens: list[dict] = []
    for i, uf in enumerate(UFS):
        passo = max(1, POR_PAGINA // int(n_mun.get(uf, POR_PAGINA)))
        for k in range(0, semanas, passo):
            a = inicio + timedelta(weeks=k)
            b = min(a + timedelta(weeks=passo), fim) - timedelta(days=1)   # sábado da última semana do bloco
            itens += _consulta_arbo(chave, doenca, a, b, uf)
            time.sleep(PAUSA)
        if (i + 1) % 9 == 0:
            print(f"    {doenca}: {i + 1}/27 estados", flush=True)
    return itens


def arbo(semanas: int = ARBO_SEMANAS, forcar: bool = False) -> bool:
    """Indicadores semanais do InfoDengue das últimas `semanas` semanas, em todos os municípios.
    Antes, uma consulta pequena (Distrito Federal) confere se já há semana ou versão do modelo nova;
    se não houver, devolve False sem baixar o resto."""
    chave = chave_api("MOSQLIMATE_KEY")
    if not chave:
        raise RuntimeError("falta a chave do Mosqlimate (MOSQLIMATE_KEY): crie em mosqlimate.org e guarde como secret")
    hoje = agora().date()
    ano, se = semana_epi(hoje)
    fim = inicio_semana(ano, se)                       # domingo desta semana (a semana em curso ainda não tem dados)
    inicio = fim - timedelta(weeks=semanas)
    if not forcar and (SAIDA / "arbo.json").exists():
        pub = ler_json(SAIDA / "arbo.json").get("dengue") or {}
        teste = normalizar_arbo(_pagina_arbo(chave, "dengue", fim - timedelta(weeks=3), fim, 1, uf="DF").get("items", []))
        if len(teste) and int(teste.se.max()) == pub.get("se") and str(teste.versao.dropna().max()) == str(pub.get("versao")):
            print(f"  InfoDengue: semana {pub.get('se')} (modelo {pub.get('versao')}) já publicada; nada a baixar")
            return False
    pasta = RAW / "arbo"
    for velho in pasta.glob("*.csv"):   # não deixa arquivos de uma coleta anterior para o tratamento
        velho.unlink()
    for doenca in DOENCAS_ARBO:
        t0 = time.time()
        existe = _pagina_arbo(chave, doenca, inicio, fim - timedelta(days=1), 1, por_pagina=1)
        time.sleep(PAUSA)
        if not int(existe.get("pagination", {}).get("total_items", 0)):
            df = pd.DataFrame()                      # ex.: zika, sem registros no InfoDengue em 2026
        else:
            df = normalizar_arbo(_arbo_brasil(chave, doenca, inicio, fim))
        if df.empty:
            if doenca == "dengue":
                raise RuntimeError("InfoDengue: nenhum registro de dengue")
            print(f"  {doenca}: nenhum registro")
            continue
        pasta.mkdir(parents=True, exist_ok=True)
        df.to_csv(pasta / f"{doenca}.csv", index=False)
        print(f"  {doenca}: {len(df):,} registros, {df.ibge.nunique():,} municípios, semanas {int(df.se.min())}–{int(df.se.max())} "
              f"({time.time() - t0:.0f} s)".replace(",", "."))
    return True


# ---------------------------------------------------------------- Monitor El Niño
def elnino() -> None:
    """Dados já publicados pelo Monitor El Niño (mesmo laboratório): fase do fenômeno e clima por região imediata."""
    for nome in ["enso", "clima"]:
        baixar(f"{ELNINO}/{nome}.json", RAW / "elnino" / f"{nome}.json", timeout=60)


# ---------------------------------------------------------------- notícias
# RSS de publicadores que autorizam a reprodução com indicação da fonte (só manchete e link são exibidos).
FEEDS = {
    # Agência Brasil (EBC): "Reprodução autorizada mediante indicação da fonte"; robots.txt pede 10 s entre acessos
    "agencia_brasil_saude": "https://agenciabrasil.ebc.com.br/rss/saude/feed.xml",
    "agencia_fiocruz": "https://agencia.fiocruz.br/rss.xml",
    "onu_news_saude": "https://news.un.org/feed/subscribe/pt/news/topic/health/feed/rss.xml",
}
DOMINIOS_FORA = (".pt", ".ao", ".mz", ".cv", ".gw", ".st", ".tl", ".mo")   # países lusófonos fora do Brasil


def _gdelt_arquivo(url: str) -> list[dict]:
    """Lê um arquivo GKG "translingual" de 15 minutos do GDELT e devolve só as notícias em português,
    de veículos brasileiros (ou que citam o Brasil), cuja manchete trata dos temas do monitor."""
    z = zipfile.ZipFile(io.BytesIO(baixar(url, timeout=180, tentativas=2)))
    saida = []
    with z.open(z.namelist()[0]) as f:
        for row in csv.reader(io.TextIOWrapper(f, encoding="utf-8", errors="replace"), delimiter="\t"):
            # Colunas do GKG 2.1: 1 data, 3 domínio, 4 link, 9/10 locais, 25 idioma de origem, 26 extras (título)
            if len(row) < 27 or "srclc:por" not in (row[25] or ""):
                continue
            dominio, link, locais = row[3].lower(), row[4], row[10] or row[9]
            if dominio.endswith(DOMINIOS_FORA) or not (dominio.endswith(".br") or "#BR" in locais):
                continue
            m = re.search(r"<PAGE_TITLE>(.*?)</PAGE_TITLE>", row[26] or "")
            titulo = " ".join(html.unescape(m.group(1)).split()) if m else ""
            if titulo and any(rx.search(titulo) for rx in RX_TEMAS.values()):
                saida.append({"titulo": titulo, "link": link, "dominio": dominio, "data": row[1]})
    return saida


def gdelt(horas: int) -> list[dict]:
    """GDELT Project (uso livre, com citação e link): arquivos de 15 minutos das últimas `horas`."""
    ultimo = baixar(f"{GDELT}/lastupdate-translation.txt", timeout=60).decode()
    fim = datetime.strptime(re.search(r"/(\d{14})\.translation\.gkg", ultimo).group(1), "%Y%m%d%H%M%S")
    itens, falhas, n = [], 0, horas * 4 + 1
    for i in range(n):
        t = fim - timedelta(minutes=15 * i)
        try:
            itens += _gdelt_arquivo(f"{GDELT}/{t:%Y%m%d%H%M%S}.translation.gkg.csv.zip")
        except (RuntimeError, zipfile.BadZipFile):
            falhas += 1   # o índice anuncia arquivos alguns minutos antes de eles existirem
        time.sleep(1)
        if i and i % 48 == 0:
            print(f"  GDELT: {i}/{n} arquivos lidos, {len(itens)} manchetes até agora")
    print(f"  GDELT: {len(itens)} manchetes em {horas} h ({falhas} arquivos indisponíveis)")
    if falhas > n // 2:
        raise RuntimeError(f"GDELT: {falhas} arquivos indisponíveis")
    return itens


def noticias(horas: int = GDELT_HORAS) -> None:
    """Manchetes do GDELT e dos RSS. Sem o GDELT, seguem os RSS; só falha se tudo falhar."""
    pasta = RAW / "noticias"
    pasta.mkdir(parents=True, exist_ok=True)
    for velho in [*pasta.glob("*.xml"), *pasta.glob("*.json")]:   # não deixa arquivos antigos para o tratamento
        velho.unlink()
    try:
        gravar_json(gdelt(horas), pasta / "gdelt.json")
    except Exception as erro:  # noqa: BLE001
        print("  GDELT indisponível:", erro)
    erros = []
    for i, (nome, url) in enumerate(FEEDS.items()):
        if i:
            time.sleep(11)
        try:
            baixar(url, pasta / f"{nome}.xml", timeout=60, tentativas=2)
        except RuntimeError as erro:
            erros.append(str(erro))
    if len(erros) == len(FEEDS):
        raise RuntimeError("; ".join(erros))
    for e in erros:
        print("  ", e)


# ---------------------------------------------------------------- agenda
# O GitHub não garante o horário das execuções agendadas; a rotina é disparada a cada hora (acionador do
# Google) e cada execução decide o que buscar pela idade dos dados já publicados (docs/dados/atualizacao.json).
# As fontes de saúde são semanais: conferir a cada 6 horas põe a semana nova no site no mesmo dia, e a
# coleta não baixa nada se a extração for a mesma.
ORDEM_BLOCOS = ["srag", "arbo", "clima", "noticias"]   # a do 02_tratamento
FONTES_DO_BLOCO = {"clima": ["elnino"]}                 # os demais blocos usam a fonte de mesmo nome
INTERVALO = {"srag": timedelta(hours=5, minutes=50), "arbo": timedelta(hours=5, minutes=50),
             "clima": timedelta(hours=5, minutes=50), "noticias": timedelta(minutes=50)}


def plano(completo: bool = False, t: datetime | None = None) -> list[str]:
    """Blocos que estão na hora de atualizar, pela última tentativa de cada um."""
    t = (t or datetime.now(timezone.utc)).astimezone(timezone.utc)
    arq = SAIDA / "atualizacao.json"
    at = ler_json(arq) if arq.exists() else {}

    def quando(bloco: str) -> datetime:
        txt = at.get(bloco, {}).get("tentado") or at.get(bloco, {}).get("atualizado")
        return datetime.fromisoformat(txt).astimezone(timezone.utc) if txt else datetime.min.replace(tzinfo=timezone.utc)

    return [b for b in ORDEM_BLOCOS if completo or t - quando(b) >= INTERVALO[b]]


# ---------------------------------------------------------------- execução
FONTES = {"srag": srag, "arbo": arbo, "elnino": elnino, "noticias": noticias}


def main(fontes: list[str], gdelt_horas: int = GDELT_HORAS, arbo_semanas: int = ARBO_SEMANAS, forcar: bool = False) -> None:
    """Coleta cada fonte pedida e registra em data/raw/status_coleta.json se ela funcionou e se trouxe dados
    novos ("novo": False quando a fonte ainda não publicou nada depois da última atualização do site)."""
    arq = RAW / "status_coleta.json"
    status = ler_json(arq) if arq.exists() else {}
    for nome in fontes:
        print(f"== {nome}")
        try:
            if nome == "noticias":
                novo = noticias(gdelt_horas)
            elif nome == "arbo":
                novo = arbo(arbo_semanas, forcar)
            elif nome == "srag":
                novo = srag(forcar)
            else:
                novo = FONTES[nome]()
            status[nome] = {"ok": True, "novo": novo is not False, "quando": agora().isoformat(timespec="minutes")}
        except Exception as erro:  # noqa: BLE001
            traceback.print_exc()
            status[nome] = {"ok": False, "quando": agora().isoformat(timespec="minutes"), "erro": str(erro)[:300]}
    gravar_json(status, arq, compacto=False)
    falhas = [k for k in fontes if not status[k]["ok"]]
    print("Coleta concluída." + (f" Falharam: {', '.join(falhas)}" if falhas else ""))


def _inteiro(minimo: int, maximo: int):
    def conferir(v: str) -> int:
        if not v.isdigit() or not minimo <= int(v) <= maximo:
            raise argparse.ArgumentTypeError(f"use um número inteiro de {minimo} a {maximo}")
        return int(v)
    return conferir


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Coleta das fontes do Monitor de Doenças por Território.")
    ap.add_argument("--fonte", nargs="*", default=list(FONTES), choices=list(FONTES))
    ap.add_argument("--gdelt-horas", type=_inteiro(1, 336), default=GDELT_HORAS, metavar="1-336",
                    help="horas de notícias do GDELT a ler (padrão 3; na carga inicial, 72)")
    ap.add_argument("--arbo-semanas", type=_inteiro(2, 110), default=ARBO_SEMANAS, metavar="2-110",
                    help="semanas de arboviroses a buscar (padrão 12; na carga inicial, 60)")
    ap.add_argument("--forcar", action="store_true", help="baixa SRAG e arboviroses mesmo sem extração nova")
    ap.add_argument("--plano", action="store_true",
                    help="não coleta: escreve as fontes e os blocos que estão na hora (formato do GitHub Actions)")
    ap.add_argument("--completo", action="store_true", help="com --plano: todos os blocos")
    args = ap.parse_args()
    if args.plano:
        blocos = plano(args.completo)
        fontes = [f for b in blocos for f in FONTES_DO_BLOCO.get(b, [b])]
        print(f"fontes={' '.join(fontes)}")
        print(f"blocos={' '.join(blocos)}")
    else:
        main(args.fonte, args.gdelt_horas, args.arbo_semanas, args.forcar)
