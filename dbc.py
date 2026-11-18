"""
dbc.py — Leitura dos arquivos .dbc do DATASUS em Python puro (sem dependências externas).

Um .dbc é um arquivo DBF (dBase) cujo cabeçalho fica como está e cujos registros são comprimidos pelo
método "PKWare Data Compression Library — implode". Este módulo:
  1. descomprime o conteúdo (porte para Python do blast.c de Mark Adler, zlib/contrib/blast, licença zlib);
  2. lê os registros do DBF como dicionários de texto.

Uso:
    from dbc import ler_dbc
    for registro in ler_dbc(caminho, campos=["ID_MN_RESI", "SEM_PRI"]):
        ...
"""
from __future__ import annotations

import struct
from pathlib import Path
from typing import Iterable, Iterator

MAXBITS = 13

# Tabelas fixas do formato (blast.c): comprimentos dos códigos de Huffman em forma compacta
_LITLEN = bytes([
    11, 124, 8, 7, 28, 7, 188, 13, 76, 4, 10, 8, 12, 10, 12, 10, 8, 23, 8,
    9, 7, 6, 7, 8, 7, 6, 55, 8, 23, 24, 12, 11, 7, 9, 11, 12, 6, 7, 22, 5,
    7, 24, 6, 11, 9, 6, 7, 22, 7, 11, 38, 7, 9, 8, 25, 11, 8, 11, 9, 12,
    8, 12, 5, 38, 5, 38, 5, 11, 7, 5, 6, 21, 6, 10, 53, 8, 7, 24, 10, 27,
    44, 253, 253, 253, 252, 252, 252, 13, 12, 45, 12, 45, 12, 61, 12, 45,
    44, 173])
_LENLEN = bytes([2, 35, 36, 53, 38, 23])
_DISTLEN = bytes([2, 20, 53, 230, 247, 151, 248])
_BASE = (3, 2, 4, 5, 6, 7, 8, 9, 10, 12, 16, 24, 40, 72, 136, 264)
_EXTRA = (0, 0, 0, 0, 0, 0, 0, 0, 1, 2, 3, 4, 5, 6, 7, 8)


def _tabela(rep: bytes) -> dict[tuple[int, int], int]:
    """Monta o código de Huffman canônico como {(comprimento, código): símbolo}."""
    comprimentos = []
    for b in rep:
        comprimentos += [b & 15] * ((b >> 4) + 1)
    contagem = [0] * (MAXBITS + 1)
    for c in comprimentos:
        contagem[c] += 1
    proximo, codigo = [0] * (MAXBITS + 2), 0
    for n in range(1, MAXBITS + 1):
        codigo = (codigo + contagem[n - 1]) << 1 if n > 1 else 0
        proximo[n] = codigo
    tabela = {}
    for simbolo, c in enumerate(comprimentos):
        if c:
            tabela[(c, proximo[c])] = simbolo
            proximo[c] += 1
    return tabela


_LIT, _LEN, _DIST = _tabela(_LITLEN), _tabela(_LENLEN), _tabela(_DISTLEN)


class _Bits:
    __slots__ = ("dados", "pos", "buf", "n")

    def __init__(self, dados: bytes, inicio: int = 0):
        self.dados, self.pos, self.buf, self.n = dados, inicio, 0, 0

    def ler(self, k: int) -> int:
        while self.n < k:
            if self.pos >= len(self.dados):
                raise ValueError("dbc: fim inesperado dos dados comprimidos")
            self.buf |= self.dados[self.pos] << self.n
            self.pos += 1
            self.n += 8
        v = self.buf & ((1 << k) - 1)
        self.buf >>= k
        self.n -= k
        return v

    def decodificar(self, tabela: dict) -> int:
        # os códigos vêm com os bits invertidos (característica do formato PKWare)
        codigo = 0
        for comprimento in range(1, MAXBITS + 1):
            codigo = (codigo << 1) | (self.ler(1) ^ 1)
            s = tabela.get((comprimento, codigo))
            if s is not None:
                return s
        raise ValueError("dbc: código de Huffman inválido")


def explodir(dados: bytes, inicio: int = 0) -> bytes:
    """Descomprime um fluxo "implode" do PKWare DCL."""
    b = _Bits(dados, inicio)
    literal_codificado = b.ler(8)
    dicionario = b.ler(8)
    if literal_codificado > 1 or not 4 <= dicionario <= 6:
        raise ValueError("dbc: cabeçalho de compressão inválido")
    saida = bytearray()
    while True:
        if b.ler(1):
            s = b.decodificar(_LEN)
            comprimento = _BASE[s] + b.ler(_EXTRA[s])
            if comprimento == 519:          # código de fim
                break
            k = 2 if comprimento == 2 else dicionario
            distancia = (b.decodificar(_DIST) << k) + b.ler(k) + 1
            if distancia > len(saida):
                raise ValueError("dbc: distância maior que os dados já lidos")
            ini = len(saida) - distancia
            for i in range(comprimento):     # pode sobrepor (repetição): copia byte a byte
                saida.append(saida[ini + i])
        else:
            saida.append(b.decodificar(_LIT) if literal_codificado else b.ler(8))
    return bytes(saida)


def dbc_para_dbf(conteudo: bytes) -> bytes:
    """Cabeçalho do DBF (tamanho nos bytes 8–9) + 4 bytes de verificação + registros comprimidos."""
    tam_cabecalho = struct.unpack_from("<H", conteudo, 8)[0]
    return conteudo[:tam_cabecalho] + explodir(conteudo, tam_cabecalho + 4)


def ler_dbf(dbf: bytes, campos: Iterable[str] | None = None, codificacao: str = "latin-1") -> Iterator[dict]:
    """Registros de um DBF como dicionários {campo: texto sem espaços}. Registros apagados são ignorados."""
    n_reg, tam_cab, tam_reg = struct.unpack_from("<IHH", dbf, 4)
    descricao, pos, desloc = [], 32, 1
    while dbf[pos] != 0x0D:
        nome = dbf[pos:pos + 11].split(b"\0")[0].decode("ascii")
        tamanho = dbf[pos + 16]
        descricao.append((nome, desloc, tamanho))
        desloc += tamanho
        pos += 32
    if desloc != tam_reg:
        raise ValueError(f"dbf: soma dos campos ({desloc}) ≠ tamanho do registro ({tam_reg})")
    if len(dbf) < tam_cab + n_reg * tam_reg:
        raise ValueError("dbf: arquivo menor que o número de registros indicado")
    if campos is not None:
        querer = set(campos)
        faltam = querer - {d[0] for d in descricao}
        if faltam:
            raise KeyError(f"dbf: campos ausentes {sorted(faltam)}")
        descricao = [d for d in descricao if d[0] in querer]
    for i in range(n_reg):
        ini = tam_cab + i * tam_reg
        if dbf[ini] == 0x2A:   # "*": registro apagado
            continue
        yield {nome: dbf[ini + d:ini + d + t].decode(codificacao).strip() for nome, d, t in descricao}


def campos_dbf(dbf: bytes) -> list[str]:
    nomes, pos = [], 32
    while dbf[pos] != 0x0D:
        nomes.append(dbf[pos:pos + 11].split(b"\0")[0].decode("ascii"))
        pos += 32
    return nomes


def ler_dbc(caminho: Path | str, campos: Iterable[str] | None = None) -> Iterator[dict]:
    return ler_dbf(dbc_para_dbf(Path(caminho).read_bytes()), campos)
