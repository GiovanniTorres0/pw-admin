"""Monta itens prontos para gravar no personagem (GRoleInventory), com o corpo binario que o gs espera.

O gs descarta, ao carregar o personagem, o item cujo corpo nao consegue ler (item.cpp
MakeItemEntry -> LoadBody); equipamento com corpo vazio cai no LoadLimit e some. Por isso:

- arma, armadura e ornamento: corpo montado como no generate_weapon/armor/decoration do gs
  (cgame/gs/template/generate_item_temp.h), com o maximo de cada faixa, sem adicionais e com
  os engastes pedidos (vazios). Dados em dados/equipamentos.json (tools/gerar_equipamentos.py);
- pocao, voo e moda: corpo fixo, pronto em "dados" (generate_medicine/flysword/fashion_item);
- tipos que o proprio jogo guarda sem corpo (vistos no banco com 0 bytes): corpo vazio;
- o resto e recusado, ate o corpo desses tipos ser montado aqui.

Layout do corpo (little-endian): requisitos <6h (nivel, classes, forca, constituicao, destreza,
inteligencia) + durabilidade e maxima <2i (x100), tamanho do corpo <h, marca <bb (3 = loja),
corpo, engastes <hh + <i por engaste, quantidade de adicionais <i.
"""
import json
import os
import struct

PASTA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dados")
with open(os.path.join(PASTA, "equipamentos.json"), encoding="utf-8") as _f:
    EQUIPAMENTOS = json.load(_f)

# Tabelas cujos itens o jogo guarda com corpo vazio (conferido no banco do servidor).
SEM_CORPO = {"MATERIAL_ESSENCE", "TASKNORMALMATTER_ESSENCE", "POKER_DICE_ESSENCE",
             "TRANSMITSCROLL_ESSENCE",
             "SKILLTOME_ESSENCE"}  # generate_skilltome nao poe corpo

UNIDADE_DURABILIDADE = 100  # DURABILITY_UNIT_COUNT do gs
MARCA_LOJA = 3


def _corpo_arma(c):
    alcance = {0: 1, 1: 0, 2: 2}.get(c["alcance_modo"], 1)  # 1 distancia, 0 perto, 2 assassino
    return struct.pack("<hh8i2f", alcance, 0, c["tipo"], c["nivel"], c["municao"], c["dano_min"],
                       c["dano_max"], c["magia_min"], c["magia_max"], c["velocidade"],
                       c["alcance"], c["alcance_curto"])


def _corpo_armadura(c):
    return struct.pack("<9i", c["defesa"], c["esquiva"], c["mp"], c["hp"], *c["resist"])


def _corpo_ornamento(c):
    return struct.pack("<9i", c["dano"], c["magia"], c["defesa"], c["esquiva"], *c["resist"])


CORPOS = {"WEAPON_ESSENCE": _corpo_arma, "ARMOR_ESSENCE": _corpo_armadura,
          "DECORATION_ESSENCE": _corpo_ornamento}


def montar(tid, quantidade, item, engastes=0):
    """Item para GRoleInventory (sem pos). item: entrada do itens.json. Levanta ValueError se
    o tipo ainda nao e suportado ou se os valores nao cabem."""
    e = EQUIPAMENTOS.get(str(tid))
    if e is not None and "dados" in e:  # pocao, voo, moda: corpo fixo
        if engastes:
            raise ValueError("so equipamento tem engastes")
        return {"id": tid, "count": quantidade, "max_count": e["pilha"] or quantidade,
                "data": bytes.fromhex(e["dados"]), "proctype": e["proc"], "expire_date": 0,
                "guid1": 0, "guid2": 0, "mask": e["mascara"]}
    if e is not None:
        if quantidade != 1:
            raise ValueError("equipamento vai de um em um (quantidade 1)")
        if not 0 <= engastes <= e["engastes"]:
            raise ValueError(f"este item aceita de 0 a {e['engastes']} engastes")
        corpo = CORPOS[e["tabela"]](e["corpo"])
        dur = e["dur"] * UNIDADE_DURABILIDADE
        dados = (struct.pack("<6H2i", *[v & 0xFFFF for v in e["req"]], dur, dur)
                 + struct.pack("<hbb", len(corpo), MARCA_LOJA, 0) + corpo
                 + struct.pack("<hh", engastes, 0) + bytes(4 * engastes) + struct.pack("<i", 0))
        return {"id": tid, "count": 1, "max_count": e["pilha"] or 1, "data": dados,
                "proctype": e["proc"], "expire_date": 0, "guid1": 0, "guid2": 0, "mask": e["mascara"]}
    if item.get("tabela") in SEM_CORPO:
        if engastes:
            raise ValueError("so equipamento tem engastes")
        return {"id": tid, "count": quantidade, "max_count": item.get("pilha") or quantidade, "data": b"",
                "proctype": item.get("proc") or 0, "expire_date": 0, "guid1": 0, "guid2": 0, "mask": 0}
    raise ValueError(f"{item.get('nome', tid)} ({item.get('tabela', '?')}): este tipo de item ainda nao "
                     "pode ser criado pelo painel; o jogo descartaria o item sem os dados dele")


def suporte(tid, item):
    """O que o painel sabe criar deste item, para a pagina."""
    e = EQUIPAMENTOS.get(str(tid))
    if e is not None and "dados" in e:
        return {"pode": True, "equipamento": False, "engastes": 0}
    if e is not None:
        return {"pode": True, "equipamento": True, "engastes": e["engastes"]}
    return {"pode": item.get("tabela") in SEM_CORPO, "equipamento": False, "engastes": 0}
