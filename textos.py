"""Textos de item do cliente para a dica (tooltip) do painel.

Baseado no web-admin/backend/painel/textos.py do pw-universal-server (MIT, Murillo Andre
Maleski), reduzido ao que a dica sem atributos de equipamento usa. Arquivos em dados/, extraidos
do configs.pck do cliente com tools/pw_pck_extract.py:

- item_desc.txt: frases-modelo na ordem da enumeracao ITEMDESC_* (indices em item_desc_indices.json);
- item_ext_desc.txt: `id "texto"`, a descricao propria do item;
- item_color.txt: `id indice`, a cor do nome.

item_desc e item_ext_desc vem em UTF-16 com BOM; as frases usam \\r como quebra e ^RRGGBB como cor.
"""
import functools
import json
import os
import re

PASTA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dados")

# l_aNameCols do cliente (EC_Game.cpp): indice 0-9 -> cor do nome.
CORES_DO_NOME = ["ffffff", "8080ff", "ffdc50", "aa32ff", "ff6000", "ffffff", "b0b0b0", "00ffae", "ff0000", "ffffff"]
_FRASE = re.compile(r'"((?:[^"\\]|\\.)*)"')
# Alem do 112 o arquivo do cliente diverge do cabecalho do fonte; so ate ali as posicoes conferem.
ULTIMA_FRASE_CONFERIDA = 112


def _ler(nome):
    with open(os.path.join(PASTA, nome), "rb") as f:
        bruto = f.read()
    if bruto[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return bruto.decode("utf-16")
    return bruto.decode("latin-1")


def _desfazer_escape(s):
    return s.replace('\\"', '"').replace("\\\\", "\\")


@functools.lru_cache(maxsize=1)
def frases():
    corpo = _ler("item_desc.txt").split("#_begin", 1)[1]
    lista = [_desfazer_escape(m.group(1)) for m in _FRASE.finditer(corpo)]
    with open(os.path.join(PASTA, "item_desc_indices.json"), encoding="utf-8") as f:
        indices = json.load(f)
    return {nome: lista[i] for nome, i in indices.items() if i <= ULTIMA_FRASE_CONFERIDA and i < len(lista)}


@functools.lru_cache(maxsize=1)
def descricoes():
    corpo = _ler("item_ext_desc.txt").split("#_begin", 1)[1]
    tabela = {}
    for linha in corpo.splitlines():
        m = re.match(r'^\s*(\d+)\s+(".*)$', linha)
        if m:
            f = _FRASE.match(m.group(2))
            if f:
                tabela.setdefault(int(m.group(1)), _desfazer_escape(f.group(1)))
    return tabela


@functools.lru_cache(maxsize=1)
def cores():
    tabela = {}
    for linha in _ler("item_color.txt").splitlines():
        partes = linha.split()
        if len(partes) >= 2 and partes[0].isdigit() and partes[1].isdigit():
            tabela.setdefault(int(partes[0]), int(partes[1]))
    return tabela


def cor_do_nome(tid):
    i = cores().get(tid, 0)
    return CORES_DO_NOME[i if 0 <= i < 10 else 0]


def frase(nome, *args):
    """Frase-modelo ITEMDESC_* com os %d/%s/%.2f do cliente preenchidos."""
    modelo = frases().get(nome, "")
    try:
        return modelo % args if args else modelo
    except (TypeError, ValueError):
        return modelo
