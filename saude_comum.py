"""
saude_comum.py — Constantes e funções compartilhadas pelos scripts do Monitor de Doenças por Território (LADRI).

Pastas do projeto:
  data/raw/    arquivos brutos baixados a cada coleta (não vão para o repositório)
  data/ref/    tabelas de referência (municípios, regiões, população, séries históricas) — versionadas
  docs/dados/  JSON lidos pelo site (gerados a cada atualização e publicados direto no GitHub Pages)

Regra de privacidade (LGPD, art. 11): os arquivos do SIVEP-Gripe e do SINAN têm uma linha por pessoa
notificada. Eles só são lidos para contar casos; nenhum dado individual é gravado em data/ref/ ou
publicado. O site recebe apenas contagens agregadas.
"""
from __future__ import annotations

import json
import os
import re
import time
import unicodedata
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

# ---------------------------------------------------------------- pastas e horário
RAIZ = Path(__file__).resolve().parent
RAW = RAIZ / "data" / "raw"
REF = RAIZ / "data" / "ref"
SAIDA = RAIZ / "docs" / "dados"

BRT = timezone(timedelta(hours=-3))   # horário de Brasília (sem horário de verão desde 2019)

# ---------------------------------------------------------------- estados e regiões
NOME_UF = {
    "RO": "Rondônia", "AC": "Acre", "AM": "Amazonas", "RR": "Roraima", "PA": "Pará", "AP": "Amapá", "TO": "Tocantins",
    "MA": "Maranhão", "PI": "Piauí", "CE": "Ceará", "RN": "Rio Grande do Norte", "PB": "Paraíba", "PE": "Pernambuco",
    "AL": "Alagoas", "SE": "Sergipe", "BA": "Bahia", "MG": "Minas Gerais", "ES": "Espírito Santo",
    "RJ": "Rio de Janeiro", "SP": "São Paulo", "PR": "Paraná", "SC": "Santa Catarina", "RS": "Rio Grande do Sul",
    "MS": "Mato Grosso do Sul", "MT": "Mato Grosso", "GO": "Goiás", "DF": "Distrito Federal",
}
# Código IBGE do estado -> sigla (os dois primeiros dígitos do código do município)
COD_UF = {11: "RO", 12: "AC", 13: "AM", 14: "RR", 15: "PA", 16: "AP", 17: "TO", 21: "MA", 22: "PI", 23: "CE", 24: "RN",
          25: "PB", 26: "PE", 27: "AL", 28: "SE", 29: "BA", 31: "MG", 32: "ES", 33: "RJ", 35: "SP", 41: "PR", 42: "SC",
          43: "RS", 50: "MS", 51: "MT", 52: "GO", 53: "DF"}
UFS = list(NOME_UF)
NOME_REGIAO = {"N": "Norte", "NE": "Nordeste", "CO": "Centro-Oeste", "SE": "Sudeste", "S": "Sul"}
REGIAO_UF = {uf: ("N" if c < 20 else "NE" if c < 30 else "SE" if c < 40 else "S" if c < 50 else "CO") for c, uf in COD_UF.items()}

# ---------------------------------------------------------------- acesso às fontes
# Identificação honesta em todas as requisições (boa prática com servidores públicos)
sessao = requests.Session()
sessao.headers["User-Agent"] = "LADRI-ASCES monitor de doencas por territorio (pesquisa academica)"


def agora() -> datetime:
    """Data e hora atuais no horário de Brasília."""
    return datetime.now(BRT)


def baixar(url: str, destino: Path | None = None, tentativas: int = 4, timeout: int = 90, cache: bool = False,
           params: dict | None = None, headers: dict | None = None) -> bytes:
    """Baixa `url` e devolve o conteúdo, tentando de novo (com espera crescente) se a fonte falhar.

    destino: se informado, grava o conteúdo nesse arquivo.
    cache:   se True e o arquivo já existir, não baixa de novo (usado nas referências fixas).
    Levanta RuntimeError depois da última tentativa.
    """
    if cache and destino is not None and destino.exists() and destino.stat().st_size > 0:
        return destino.read_bytes()
    for i in range(tentativas):
        try:
            resp = sessao.get(url, timeout=timeout, params=params, headers=headers)
            resp.raise_for_status()
            if destino is not None:
                destino.parent.mkdir(parents=True, exist_ok=True)
                destino.write_bytes(resp.content)
            return resp.content
        except requests.RequestException as erro:
            resp = getattr(erro, "response", None)
            status = getattr(resp, "status_code", None)
            if status in (401, 403, 404) or i == tentativas - 1:   # erro definitivo: não adianta insistir
                raise RuntimeError(f"Falha ao baixar {url}: {erro}") from erro
            if status == 429:   # limite de consultas da fonte: espera o que ela pedir (ou 1, 2, 4... minutos)
                pedido = (resp.headers.get("Retry-After") or "").strip()
                espera = int(pedido) if pedido.isdigit() else 60 * 2 ** i
                print(f"  limite de consultas atingido; esperando {espera} s", flush=True)
                time.sleep(min(espera, 900))
            else:
                time.sleep(3 * (i + 1))
    return b""


def ler_json(caminho: Path):
    """Lê um arquivo JSON em UTF-8."""
    return json.loads(caminho.read_text(encoding="utf-8"))


def gravar_json(obj, caminho: Path, compacto: bool = True) -> None:
    """Grava `obj` em JSON (UTF-8, fim de linha LF). `compacto=False` deixa o arquivo legível."""
    caminho.parent.mkdir(parents=True, exist_ok=True)
    texto = json.dumps(obj, ensure_ascii=False, separators=(",", ":") if compacto else None, indent=None if compacto else 1)
    caminho.write_text(texto, encoding="utf-8", newline="\n")


def chave_api(nome: str) -> str | None:
    """Lê uma chave de acesso da variável de ambiente `nome` ou do arquivo `.nome` na raiz (fora do repositório).
    A chave nunca é escrita no código nem nos dados publicados."""
    valor = os.environ.get(nome)
    if not valor:
        arq = RAIZ / f".{nome.lower()}"
        valor = arq.read_text(encoding="utf-8").strip() if arq.exists() else None
    return valor or None


# ---------------------------------------------------------------- semana epidemiológica
def inicio_ano_epi(ano: int) -> date:
    """Domingo em que começa a semana epidemiológica 1 do ano (regra do Ministério da Saúde: a SE 1 é a
    primeira semana, de domingo a sábado, com pelo menos quatro dias no ano novo)."""
    d4 = date(ano, 1, 4)
    return d4 - timedelta(days=(d4.weekday() + 1) % 7)


def semana_epi(d: date) -> tuple[int, int]:
    """(ano epidemiológico, semana) de uma data."""
    ano = d.year + 1
    while d < inicio_ano_epi(ano):
        ano -= 1
    return ano, (d - inicio_ano_epi(ano)).days // 7 + 1


def inicio_semana(ano: int, se: int) -> date:
    """Domingo em que começa a semana epidemiológica `se` do ano `ano`."""
    return inicio_ano_epi(ano) + timedelta(weeks=se - 1)


def codigo_se(ano: int, se: int) -> int:
    """Semana no formato AAAASS (ex.: 202645), o mesmo do InfoDengue."""
    return ano * 100 + se


# ---------------------------------------------------------------- SRAG (SIVEP-Gripe)
SRAG_DATASET = "https://dadosabertos.saude.gov.br/dataset/srag-2019-a-2026"
# Grupos de agente etiológico, na ordem em que aparecem no site. Classificação pela CLASSI_FIN do SIVEP-Gripe
# (1 influenza, 2 outro vírus respiratório, 3 outro agente, 4 não especificado, 5 covid-19, vazio = em investigação);
# dentro de "outro vírus", o VSR é separado pelo resultado positivo no RT-PCR ou no teste de antígeno.
SRAG_GRUPOS = {
    "covid": "Covid-19",
    "flu_a": "Influenza A",
    "flu_b": "Influenza B",
    "flu_nd": "Influenza (tipo não informado)",
    "vsr": "VSR",
    "outros_virus": "Outros vírus",
    "outro_agente": "Outro agente",
    "nao_especificado": "Não especificado",
    "em_investigacao": "Em investigação",
}
SRAG_FAIXAS = ["0-4", "5-14", "15-59", "60+"]
_SRAG_COLUNAS = ["SEM_PRI", "DT_SIN_PRI", "SG_UF", "CLASSI_FIN", "EVOLUCAO", "NU_IDADE_N", "TP_IDADE",
                 "PCR_VSR", "AN_VSR", "TP_FLU_PCR", "TP_FLU_AN"]


_RX_INFLUD = re.compile(r"https://s3\.sa-east-1\.amazonaws\.com/ckan\.saude\.gov\.br/SRAG/(\d{4})/INFLUD\d{2}-(\d{2})-(\d{2})-(\d{4})\.parquet")


def arquivos_srag() -> dict[int, dict]:
    """Arquivos anuais do SIVEP-Gripe publicados no Portal de Dados Abertos do SUS: {ano: {"url", "data"}}.
    O nome do arquivo traz a data da extração (INFLUD26-16-11-2026); quando há mais de um, vale o mais novo."""
    pagina = baixar(SRAG_DATASET, timeout=120).decode("utf-8", "replace")
    achados: dict[int, dict] = {}
    for m in _RX_INFLUD.finditer(pagina):
        ano, extracao = int(m.group(1)), date(int(m.group(4)), int(m.group(3)), int(m.group(2)))
        if ano not in achados or extracao > achados[ano]["data"]:
            achados[ano] = {"url": m.group(0), "data": extracao}
    if not achados:
        raise RuntimeError("nenhum arquivo do SIVEP-Gripe encontrado na página do Portal de Dados Abertos do SUS")
    return dict(sorted(achados.items()))


def agregar_srag(arquivo: Path, ano: int):
    """Lê um arquivo anual do SIVEP-Gripe (parquet, uma linha por pessoa notificada) e devolve só contagens:
    uma linha por UF de residência × semana de início dos sintomas × grupo de agente × faixa etária, com
    `casos` e `obitos` (evolução = óbito por SRAG). Linhas sem UF ou sem semana são descartadas (não se adivinha).

    Devolve (tabela agregada, número de linhas descartadas). Os dados individuais não saem desta função."""
    import pandas as pd

    df = pd.read_parquet(arquivo, columns=_SRAG_COLUNAS)
    total = len(df)
    se = pd.to_numeric(df.pop("SEM_PRI"), errors="coerce")
    # semana a partir da data de início dos sintomas quando o campo de semana vier vazio
    falta = se.isna() & df.DT_SIN_PRI.notna()
    if falta.any():
        se[falta] = [s if a == ano else None for a, s in (semana_epi(d.date()) for d in df.DT_SIN_PRI[falta])]
    df["se"] = se
    df = df[df.se.between(1, 53) & df.SG_UF.isin(UFS)].copy()
    df["se"] = df.se.astype(int)

    def txt(c: str) -> pd.Series:
        """Campo como texto simples, sem valores nulos (vazio vira "")."""
        return df[c].astype("string").fillna("").str.strip().str.replace(r"\.0$", "", regex=True).astype(object)

    cf = pd.to_numeric(txt("CLASSI_FIN"), errors="coerce").fillna(0).astype(int)
    tipo_flu = txt("TP_FLU_PCR").where(txt("TP_FLU_PCR") != "", txt("TP_FLU_AN"))
    vsr = txt("PCR_VSR").eq("1") | txt("AN_VSR").eq("1")
    grupo = pd.Series("em_investigacao", index=df.index)
    grupo[cf.eq(5)] = "covid"
    grupo[cf.eq(1)] = "flu_nd"                      # influenza sem o tipo informado
    grupo[cf.eq(1) & tipo_flu.eq("1")] = "flu_a"
    grupo[cf.eq(1) & tipo_flu.eq("2")] = "flu_b"
    grupo[cf.eq(2)] = "outros_virus"
    grupo[cf.eq(2) & vsr] = "vsr"
    grupo[cf.eq(3)] = "outro_agente"
    grupo[cf.eq(4)] = "nao_especificado"
    df["grupo"] = grupo

    idade = pd.to_numeric(txt("NU_IDADE_N"), errors="coerce")
    anos = idade.where(txt("TP_IDADE").eq("3"), 0).where(idade.notna())   # TP_IDADE: 1 dias, 2 meses, 3 anos
    df["faixa"] = pd.cut(anos, [-1, 4, 14, 59, 200], labels=SRAG_FAIXAS).astype(object).where(anos.notna(), "ign")
    df["obito"] = txt("EVOLUCAO").eq("2").astype(int)

    ag = (df.groupby(["SG_UF", "se", "grupo", "faixa"], observed=True)
            .agg(casos=("obito", "size"), obitos=("obito", "sum")).reset_index()
            .rename(columns={"SG_UF": "uf"}))
    ag.insert(0, "ano", ano)
    return ag, total - len(df)


# ---------------------------------------------------------------- texto e números
def chave(nome: str) -> str:
    """Normaliza um nome para comparação: sem acentos, minúsculas, sem hífens nem espaços extras."""
    s = unicodedata.normalize("NFKD", str(nome)).encode("ascii", "ignore").decode("ascii")
    return " ".join(s.lower().replace("-", " ").replace("'", " ").split())


UF_POR_NOME = {chave(nome): sg for sg, nome in NOME_UF.items()}


def url_segura(u) -> str | None:
    """Devolve o link só se for http(s). Um link 'javascript:' vindo de uma fonte externa viraria código no site."""
    u = str(u or "").strip()
    return u if u.lower().startswith(("https://", "http://")) else None


def r(x, n: int = 2):
    """Arredonda para `n` casas, devolvendo None para valores ausentes (None ou NaN)."""
    try:
        if x is None or x != x:
            return None
        return round(float(x), n)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- temas das notícias
# Classificação pela manchete. Expressões com limite de palavra para evitar falsos positivos.
TEMAS = {
    "arboviroses": r"\bdengue\b|chikungunya|\bzika\b|aedes|arbovirose|febre amarela|oropouche|mosquito da dengue",
    "respiratorias": r"\bsrag\b|síndrome respiratória|\bgripe\b|influenza|\bh[13]n\d\b|covid|coronav[íi]rus|sars-cov|\bvsr\b"
                     r"|vírus sincicial|bronquiolite|doenças? respiratórias?",
    "clima-saude": r"\bel ni[nñ]o\b|onda de calor|calor (extremo|intenso)|enchentes?|leptospirose|seca\b.*saúde",
    "vacinacao": r"vacina|vacinação|imuniza|cobertura vacinal",
}
RX_TEMAS = {k: re.compile(v, flags=re.I) for k, v in TEMAS.items()}
