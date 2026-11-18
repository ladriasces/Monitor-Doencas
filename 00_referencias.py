"""
00_referencias.py — Monta, uma única vez, as tabelas de referência da plataforma (data/ref/).
Roda no computador do LADRI (precisa do geopandas: pip install geopandas); o resultado vai para o repositório.

  python 00_referencias.py                  # tudo o que ainda não existe
  python 00_referencias.py --refazer srag   # refaz uma etapa (malhas|locais|populacao|srag)

Etapas:
  malhas     IBGE: estados, regiões imediatas e municípios -> data/raw/geo/
  locais     municípios, regiões imediatas e mesorregiões -> data/ref/*.csv
  populacao  IBGE, população residente estimada (ano mais recente) por município -> data/ref/populacao.csv
  srag       SIVEP-Gripe, anos fechados (2019 até dois anos atrás), só contagens por UF × semana × agente ×
             faixa etária -> data/ref/srag_hist.csv. O ano anterior e o atual ainda recebem notificações
             e são lidos a cada coleta (01_coleta.py).
"""
from __future__ import annotations

import argparse
import json

import pandas as pd

from saude_comum import RAW, REF, agora, agregar_srag, arquivos_srag, baixar

GEO = RAW / "geo"
IBGE = "https://servicodados.ibge.gov.br/api"
SIDRA = "https://apisidra.ibge.gov.br/values/t/6579/n6/all/v/9324/p/last%201"   # população residente estimada
SRAG_INICIO = 2019


# ---------------------------------------------------------------- malhas
def malhas() -> None:
    """Baixa as malhas do IBGE (estados, regiões imediatas e municípios) em qualidade mínima."""
    fmt = "formato=application/vnd.geo+json&qualidade=minima"
    for nome, intra in [("br_uf", "UF"), ("br_imediatas", "regiao-imediata"), ("br_municipios", "municipio")]:
        baixar(f"{IBGE}/v3/malhas/paises/BR?intrarregiao={intra}&{fmt}", GEO / f"{nome}.geojson", cache=True, timeout=300)
    print("Malhas: ok")


# ---------------------------------------------------------------- locais
def locais() -> None:
    """Tabelas de lugares a partir da API de localidades do IBGE: municipios.csv (município -> estado, região
    imediata, mesorregião), regioes_imediatas.csv e mesorregioes.csv."""
    muns = json.loads(baixar(f"{IBGE}/v1/localidades/municipios", GEO / "ibge_municipios.json", cache=True))
    imeds = json.loads(baixar(f"{IBGE}/v1/localidades/regioes-imediatas", GEO / "ibge_imediatas.json", cache=True))

    linhas = []
    for m in muns:
        ri = m.get("regiao-imediata") or {}
        me = (m.get("microrregiao") or {}).get("mesorregiao") or {}
        uf = (ri.get("regiao-intermediaria") or {}).get("UF") or me.get("UF") or {}
        linhas.append({"ibge": str(m["id"]), "nome": m["nome"], "uf": uf.get("sigla"), "imediata": str(ri.get("id", ""))})
    mun = pd.DataFrame(linhas).sort_values("ibge")
    REF.mkdir(parents=True, exist_ok=True)
    mun.to_csv(REF / "municipios.csv", index=False)

    ri = pd.DataFrame([{"id": str(x["id"]), "nome": x["nome"], "uf": x["regiao-intermediaria"]["UF"]["sigla"]} for x in imeds])
    ri["n_mun"] = ri.id.map(mun.imediata.value_counts()).fillna(0).astype(int)
    ri.sort_values("id").to_csv(REF / "regioes_imediatas.csv", index=False)
    print(f"Locais: {len(mun)} municípios, {len(ri)} regiões imediatas")


# ---------------------------------------------------------------- população
def populacao() -> None:
    """População residente estimada pelo IBGE (tabela 6579 do SIDRA), ano mais recente, por município."""
    dados = json.loads(baixar(SIDRA, timeout=300))[1:]   # a primeira linha é o cabeçalho
    pop = pd.DataFrame({"ibge": [d["D1C"] for d in dados], "ano": [int(d["D3C"]) for d in dados],
                        "pop": pd.to_numeric([d["V"] for d in dados], errors="coerce")})
    pop = pop.dropna().astype({"pop": int}).sort_values("ibge")
    mun = pd.read_csv(REF / "municipios.csv", dtype=str)
    faltam = sorted(set(mun.ibge) - set(pop.ibge))
    if faltam:
        print(f"População: {len(faltam)} municípios sem estimativa: {faltam[:10]}")
    pop.to_csv(REF / "populacao.csv", index=False)
    print(f"População: {len(pop)} municípios, ano {pop.ano.max()}, total {pop['pop'].sum():,}".replace(",", "."))


# ---------------------------------------------------------------- SRAG
def srag() -> None:
    """Agrega os anos fechados do SIVEP-Gripe. Cada arquivo é baixado, reduzido a contagens e apagado."""
    ultimo_fechado = agora().year - 2
    partes = []
    for ano, info in arquivos_srag().items():
        if not SRAG_INICIO <= ano <= ultimo_fechado:
            continue
        arq = RAW / "srag" / f"INFLUD{ano % 100:02d}.parquet"
        baixar(info["url"], arq, timeout=600)
        ag, descartadas = agregar_srag(arq, ano)
        arq.unlink()   # o arquivo individual não fica guardado
        partes.append(ag)
        print(f"SRAG {ano}: {ag.casos.sum():,} casos, {ag.obitos.sum():,} óbitos (extração {info['data']:%d/%m/%Y}; "
              f"{descartadas} linhas sem UF ou semana)".replace(",", "."))
    hist = pd.concat(partes, ignore_index=True).sort_values(["ano", "se", "uf", "grupo", "faixa"])
    hist.to_csv(REF / "srag_hist.csv", index=False)
    print(f"SRAG histórico: {len(hist)} linhas, {hist.ano.min()}–{hist.ano.max()}")


ETAPAS = {"malhas": (malhas, GEO / "br_municipios.geojson"), "locais": (locais, REF / "municipios.csv"),
          "populacao": (populacao, REF / "populacao.csv"), "srag": (srag, REF / "srag_hist.csv")}

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--refazer", nargs="*", choices=list(ETAPAS), default=[], help="etapas a refazer mesmo se já existirem")
    args = ap.parse_args()
    for nome, (funcao, saida) in ETAPAS.items():
        if nome in args.refazer or not saida.exists():
            funcao()
        else:
            print(f"{nome}: já existe ({saida.name})")
