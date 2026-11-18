"""
03_painel.py — Gera o site (docs/index.html) a partir de painel/template.html.

  python 03_painel.py

O HTML leva embutido só o que é fixo: as malhas dos estados e das regiões imediatas, a lista de municípios
(para a busca do "Meu lugar") e o logo. A malha dos 5.570 municípios vai num arquivo à parte
(docs/geo/municipios.json), lido pelo navegador só quando um mapa precisa dela. Os dados de cada semana
ficam em docs/dados/*.json. Por isso este script só precisa rodar quando o visual (template) mudar.
Precisa de geopandas; o GitHub Actions não usa este script.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re

import geopandas as gpd
import pandas as pd
import shapely
from shapely.geometry import MultiPolygon, Polygon
from shapely.geometry.polygon import orient

from saude_comum import COD_UF, RAIZ, RAW, REF

PAINEL = RAIZ / "painel"
GEO = RAW / "geo"   # malhas baixadas pelo 00_referencias.py
# Versão fixa do script do GoatCounter, com o hash publicado (SRI): se o arquivo mudar, o navegador não o executa
SRI_GOATCOUNTER = "sha384-nRw6qfbWyJha9LhsOtSb2YJDyZdKvvCFh0fJYlkquSFjUxp9FVNugbfy8q1jdxI+"
GOATCOUNTER = "ladri"   # contador anônimo do LADRI (o endereço do site separa os painéis)
REPO = "https://github.com/ladriasces/Monitor-Doencas"
SITE = "https://ladriasces.github.io/Monitor-Doencas/"
# Até o lançamento (depois da revisão da área da saúde), o site pede aos buscadores que não o indexem.
# No lançamento: LANCADO = True, regerar e enviar.
LANCADO = False
DESCRICAO = ("Acompanhe a dengue, a chikungunya, a zika e as doenças respiratórias graves no seu município e no Brasil, "
             "com dados oficiais do Ministério da Saúde e da Fiocruz e a ligação com o clima. LADRI · ASCES-UNITA.")


# ---------------------------------------------------------------- malhas compactas para o D3
def sentido_d3(g):
    """Orienta os polígonos como o D3 espera (anel externo no sentido horário). Pedaços sem área são
    descartados: o D3 os interpretaria como o globo inteiro."""
    partes = [g] if isinstance(g, Polygon) else list(g.geoms) if isinstance(g, MultiPolygon) else []
    partes = [orient(p, sign=-1.0) for p in partes if p.area > 0]
    if not partes:
        return g
    return partes[0] if len(partes) == 1 else MultiPolygon(partes)


def geo_compacto(gdf: gpd.GeoDataFrame, tolerancia: float, casas: int = 3) -> dict:
    """Simplifica as geometrias (mantendo as fronteiras comuns), arredonda as coordenadas e devolve um GeoJSON pequeno."""
    gdf = gdf.copy()
    simpl = shapely.set_precision(gdf.geometry.simplify(tolerancia, preserve_topology=True).values, 10 ** -casas)
    gdf["geometry"] = gpd.GeoSeries(simpl, index=gdf.index, crs=gdf.crs).map(sentido_d3)
    gdf = gdf[~gdf.geometry.is_empty]
    gj = json.loads(gdf.to_json(drop_id=True))

    def arred(c):
        return [arred(x) for x in c] if isinstance(c[0], list) else [round(c[0], casas), round(c[1], casas)]

    for f in gj["features"]:
        f["geometry"]["coordinates"] = arred(f["geometry"]["coordinates"])
    return gj


def base() -> dict:
    """Dados fixos embutidos no site: malhas dos estados e regiões imediatas e listas de lugares."""
    uf = gpd.read_file(GEO / "br_uf.geojson")
    uf["id"] = uf.codarea.astype(int).map(COD_UF)
    imed = gpd.read_file(GEO / "br_imediatas.geojson").rename(columns={"codarea": "id"})
    ri = pd.read_csv(REF / "regioes_imediatas.csv", dtype={"id": str})
    mun = pd.read_csv(REF / "municipios.csv", dtype=str)
    return {
        "geo": {"uf": geo_compacto(uf[["id", "geometry"]], 0.02), "imed": geo_compacto(imed[["id", "geometry"]], 0.008)},
        "imed": [[t.id, t.nome, t.uf] for t in ri.itertuples()],
        "mun": [[t.ibge, t.nome, t.uf, t.imediata] for t in mun.itertuples()],
    }


def malha_municipios() -> str:
    """Grava docs/geo/municipios.json e devolve um código curto do conteúdo (para renovar o cache do navegador)."""
    mun = gpd.read_file(GEO / "br_municipios.geojson").rename(columns={"codarea": "id"})
    gj = geo_compacto(mun[["id", "geometry"]], 0.012, 3)
    texto = json.dumps(gj, ensure_ascii=False, separators=(",", ":"))
    destino = RAIZ / "docs" / "geo" / "municipios.json"
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_text(texto, encoding="utf-8", newline="\n")
    print(f"docs/geo/municipios.json: {len(gj['features'])} municípios, {len(texto.encode('utf-8')) / 1e6:.2f} MB")
    return hashlib.sha256(texto.encode("utf-8")).hexdigest()[:10]


def politica_seguranca(script: str) -> str:
    """Política de segurança de conteúdo (CSP): o navegador só executa o script do painel (pelo hash exato do
    conteúdo), o D3 (servido pelo próprio site) e o contador anônimo, os dois com verificação de integridade
    (SRI). Tudo o mais vem do próprio site; qualquer script injetado é bloqueado."""
    hash_script = base64.b64encode(hashlib.sha256(script.encode("utf-8")).digest()).decode("ascii")
    gc = f"https://{GOATCOUNTER}.goatcounter.com"
    return ("default-src 'self'; "
            f"script-src 'self' 'sha256-{hash_script}' https://gc.zgo.at; "
            "style-src 'self' 'unsafe-inline'; font-src 'self'; "
            f"img-src 'self' data: {gc}; connect-src 'self' {gc}; "
            "object-src 'none'; base-uri 'none'; form-action 'none'")


def main() -> None:
    """Preenche o template com os dados fixos e grava docs/index.html."""
    versao_geo = malha_municipios()
    dados = base()
    texto = json.dumps(dados, ensure_ascii=False, separators=(",", ":"))
    logo = base64.b64encode((PAINEL / "logo_ladri.png").read_bytes()).decode("ascii")
    corpo = ((PAINEL / "template.html").read_text(encoding="utf-8")
             .replace("/*__BASE__*/null", texto).replace("__LOGO__", f"data:image/png;base64,{logo}")
             .replace("__REPO__", REPO).replace("__VERSAO_GEO__", versao_geo)
             .replace('<meta charset="utf-8">\n', "", 1))
    fim_head = corpo.index("</style>") + len("</style>")
    cabeca, resto = corpo[:fim_head], corpo[fim_head:]
    scripts = re.findall(r"<script>(.*?)</script>", resto, flags=re.S)
    if len(scripts) != 1:
        raise RuntimeError(f"esperava 1 script embutido no template, achei {len(scripts)}")
    robos = "" if LANCADO else '\n<meta name="robots" content="noindex, nofollow">'   # prévia: fora dos buscadores
    pagina = f"""<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="{politica_seguranca(scripts[0])}">
<meta name="referrer" content="strict-origin-when-cross-origin">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="description" content="{DESCRICAO}">{robos}
<link rel="canonical" href="{SITE}">
<meta property="og:type" content="website">
<meta property="og:locale" content="pt_BR">
<meta property="og:site_name" content="Monitor de Doenças por Território · LADRI">
<meta property="og:title" content="Monitor de Doenças por Território">
<meta property="og:description" content="{DESCRICAO}">
<meta property="og:url" content="{SITE}">
<meta name="twitter:card" content="summary">
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='7' fill='%231d6b6f'/%3E%3Cpath d='M13 7h6v6h6v6h-6v6h-6v-6H7v-6h6z' fill='white'/%3E%3C/svg%3E">
<style>[hidden] {{ display: none !important; }} img {{ max-width: 100%; }}</style>
<script data-goatcounter="https://{GOATCOUNTER}.goatcounter.com/count" async src="https://gc.zgo.at/count.v4.js" integrity="{SRI_GOATCOUNTER}" crossorigin="anonymous"></script>
{cabeca}
</head>
<body>
{resto}
</body>
</html>
"""
    destino = RAIZ / "docs" / "index.html"
    destino.parent.mkdir(exist_ok=True)
    # newline="\n": no Windows o Python gravaria "\r\n", o script mudaria e o hash da CSP deixaria de valer
    destino.write_text(pagina, encoding="utf-8", newline="\n")
    (RAIZ / "docs" / ".nojekyll").write_text("", encoding="utf-8")
    print(f"docs/index.html gerado: {len(pagina.encode('utf-8')) / 1e6:.2f} MB (dados fixos {len(texto.encode('utf-8')) / 1e6:.2f} MB)")


if __name__ == "__main__":
    main()
