"""
04_validar.py — Confere os dados e a segurança do site antes de publicar (roda no GitHub Actions).

  python 04_validar.py

Erros bloqueiam a publicação (o site continua na última versão boa); avisos só aparecem no registro.
Confere também a regra de privacidade: nenhum arquivo publicado pode ter cara de dado individual.
"""
from __future__ import annotations

import base64
import hashlib
import re
import sys
from datetime import date, datetime, timedelta

from saude_comum import RAIZ, SAIDA, SRAG_GRUPOS, UFS, agora, ler_json

erros: list[str] = []
avisos: list[str] = []


def erro(cond: bool, msg: str) -> None:
    if not cond:
        erros.append(msg)


def aviso(cond: bool, msg: str) -> None:
    if not cond:
        avisos.append(msg)


def carregar(nome: str):
    arq = SAIDA / f"{nome}.json"
    if not arq.exists():
        erros.append(f"{nome}.json ausente")
        return None
    try:
        return ler_json(arq)
    except Exception as e:  # noqa: BLE001
        erros.append(f"{nome}.json ilegível: {e}")
        return None


def numero(v, minimo=0, maximo=None) -> bool:
    return isinstance(v, (int, float)) and v >= minimo and (maximo is None or v <= maximo)


# ---------------------------------------------------------------- dados
def validar_srag(S: dict) -> None:
    extr = date.fromisoformat(S["extracao"])
    aviso(agora().date() - extr <= timedelta(days=15), f"SRAG: extração antiga ({S['extracao']})")
    erro(set(S["territorios"]) == {"BR", *UFS}, "SRAG: faltam territórios")
    erro(set(S["grupos"]) == set(SRAG_GRUPOS), "SRAG: grupos de agentes diferentes do esperado")
    br = S["territorios"]["BR"]
    n = S["se_fim"]
    for g, serie in br["casos"].items():
        erro(len(serie) == n and all(numero(v) for v in serie), f"SRAG: série de {g} inválida")
    soma_ufs = sum(S["territorios"][uf]["resumo"]["casos_ano"] for uf in UFS)
    erro(soma_ufs == br["resumo"]["casos_ano"], f"SRAG: soma dos estados ({soma_ufs}) ≠ Brasil ({br['resumo']['casos_ano']})")
    erro(br["resumo"]["casos_ano"] > 1000, "SRAG: Brasil com casos demais de poucos")
    for uf in UFS:
        r = S["territorios"][uf]["resumo"]
        erro(r["tendencia"] in (None, "alta", "queda", "estavel"), f"SRAG {uf}: tendência inválida")
        erro(r["obitos_ano"] <= r["casos_ano"], f"SRAG {uf}: mais óbitos que casos")


def validar_arbo(A: dict) -> None:
    cols = A["cols"]
    i_nivel, i_inc = cols.index("nivel"), cols.index("inc")
    algum = False
    for d in A["doencas"]:
        X = A.get(d)
        if not X:
            avisos.append(f"arbo: sem dados de {d}")
            continue
        algum = True
        se = X["se"]
        ano, sem = divmod(se, 100)
        erro(1 <= sem <= 53 and 2020 <= ano <= agora().year + 1, f"arbo {d}: semana {se} inválida")
        aviso(agora().date() - date.fromisoformat(X["inicio"]) <= timedelta(days=21), f"arbo {d}: semana antiga ({X['inicio']})")
        for ib, v in X["mun"].items():
            if not (len(ib) == 7 and ib.isdigit()):
                erros.append(f"arbo {d}: código de município inválido {ib}")
                break
            if v[i_nivel] is not None and v[i_nivel] not in (1, 2, 3, 4):
                erros.append(f"arbo {d}: nível inválido em {ib}")
                break
            if v[i_inc] is not None and v[i_inc] < 0:
                erros.append(f"arbo {d}: incidência negativa em {ib}")
                break
        aviso(len(X["mun"]) >= 5000, f"arbo {d}: só {len(X['mun'])} municípios com dados")
    erro(algum, "arbo: nenhuma doença com dados")


def validar_clima(C: dict) -> None:
    erro(len(C["regioes"]) >= 500, "clima: menos de 500 regiões imediatas")


def validar_noticias(N: dict) -> None:
    for n in N["noticias"]:
        if not str(n.get("link", "")).startswith(("https://", "http://")):
            erros.append("notícias: link que não é http(s)")
            break


# ---------------------------------------------------------------- privacidade
# Nomes de campos que só existem em dados individuais do SIVEP-Gripe/SINAN: não podem aparecer no que é publicado.
CAMPOS_INDIVIDUAIS = ["NU_NOTIFIC", "DT_NASC", "CS_SEXO", "CS_RACA", "NU_IDADE_N", "ID_MN_RESI", "DT_SIN_PRI", "NM_PACIENT",
                      "NU_CPF", "NU_CNS", "TEM_CPF"]


def validar_privacidade() -> None:
    for arq in SAIDA.rglob("*.json"):
        texto = arq.read_text(encoding="utf-8")
        for c in CAMPOS_INDIVIDUAIS:
            if c in texto:
                erros.append(f"privacidade: {arq.name} contém o campo individual {c}")
    for arq in (RAIZ / "docs").rglob("*"):
        if arq.suffix.lower() in (".csv", ".parquet", ".dbc", ".zip"):
            erros.append(f"privacidade: arquivo de dados brutos dentro de docs/: {arq.name}")


# ---------------------------------------------------------------- site
def validar_site() -> None:
    """A CSP autoriza o script do painel pelo hash exato: se o arquivo mudar (ex.: fim de linha \\r\\n), o site para."""
    html = (RAIZ / "docs" / "index.html").read_bytes()
    erro(b"\r\n" not in html, "docs/index.html com fim de linha \\r\\n (quebra o hash da CSP)")
    texto = html.decode("utf-8")
    m = re.search(r"script-src [^;]*'sha256-([^']+)'", texto)
    scripts = re.findall(r"<script>(.*?)</script>", texto, flags=re.S)
    erro(bool(m) and len(scripts) == 1, "CSP ou script do painel não encontrados")
    if m and len(scripts) == 1:
        h = base64.b64encode(hashlib.sha256(scripts[0].encode("utf-8")).digest()).decode("ascii")
        erro(h == m.group(1), "o hash da CSP não corresponde ao script do painel")
    erro('integrity="sha512-' in texto, "D3 sem verificação de integridade (SRI)")
    aviso('name="robots" content="noindex' not in texto,
          "site em prévia: buscadores não indexam (no lançamento, LANCADO = True no 03_painel.py)")
    erro(" onclick=" not in texto and " onerror=" not in texto, "atributo de evento embutido no HTML (bloqueado pela CSP)")
    erro((RAIZ / "docs" / "geo" / "municipios.json").exists(), "malha dos municípios ausente (rode 03_painel.py)")


VALIDADORES = {"srag": validar_srag, "arbo": validar_arbo, "clima": validar_clima, "noticias": validar_noticias}
OPCIONAIS = {"clima", "noticias"}   # o site funciona sem eles (as seções mostram um aviso)


def validar() -> None:
    for nome, funcao in VALIDADORES.items():
        if nome in OPCIONAIS and not (SAIDA / f"{nome}.json").exists():
            avisos.append(f"{nome}.json ausente (opcional)")
            continue
        dados = carregar(nome)
        if dados:
            try:
                funcao(dados)
            except (KeyError, TypeError, ValueError) as e:
                erros.append(f"{nome}: estrutura inesperada ({e!r})")
    validar_privacidade()
    validar_site()


if __name__ == "__main__":
    validar()
    for a in avisos:
        print("AVISO:", a)
    for e in erros[:40]:
        print("ERRO:", e)
    print(f"Validação: {len(erros)} erro(s), {len(avisos)} aviso(s).")
    sys.exit(1 if erros else 0)
