"""Dica (tooltip) de item, montada na ordem do cliente.

Baseado no web-admin/backend/painel/dica.py do pw-universal-server (MIT, Murillo Andre Maleski).
Esta versao so usa o que vem do elements.data (itens.json) e dos textos do cliente: nome na cor
do item_color.txt, quantidade, nivel exigido, preco e a descricao do item_ext_desc.txt. Os
atributos de equipamento (dano, defesa, efeitos, pedras) ficam para a fase 6, que precisa ler o
corpo binario do item (GRoleInventory.data).

Cada linha e texto com codigos de cor ^RRGGBB, como no cliente; o navegador pinta.
"""
import json
import os

import textos

BRANCO = "^ffffff"
VERMELHO = "^ff6060"

# Requisitos dos equipamentos [nivel, classes, forca, constituicao, destreza, inteligencia], do
# elements.data do servidor (tools/gerar_equipamentos.py).
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "dados", "equipamentos.json"),
          encoding="utf-8") as _f:
    REQUISITOS = {int(k): v["req"] for k, v in json.load(_f).items() if "req" in v}
CLASSES = None  # server.CLASSES, ligado no import do server
REQ_FRASES = ((2, "ITEMDESC_STRENGTHREQ"), (4, "ITEMDESC_AGILITYREQ"), (3, "ITEMDESC_VITALITYREQ"),
              (5, "ITEMDESC_ENERGYREQ"))


def linhas_da_dica(tid, item, quantidade=1):
    t = textos.frase
    nome = item.get("nome") or f"Item {tid}"
    linhas = ["^" + textos.cor_do_nome(tid) + t("ITEMDESC_NAME", nome)]
    if quantidade > 1:
        linhas.append(f"{BRANCO}×{quantidade}")
    req = REQUISITOS.get(int(tid))
    nivel = (req[0] if req else 0) or item.get("nivel_req")
    if nivel:
        linhas.append(BRANCO + t("ITEMDESC_LEVELREQ", nivel))
    if req:
        # Atributos minimos para usar o equipamento, como o cliente mostra
        for i, frase in REQ_FRASES:
            if req[i]:
                linhas.append(BRANCO + " ".join(t(frase, req[i]).split()))
        if CLASSES and req[1] and req[1] & ((1 << len(CLASSES)) - 1) != (1 << len(CLASSES)) - 1:
            nomes = [c for i, c in enumerate(CLASSES) if req[1] & (1 << i)]
            linhas.append(VERMELHO + f"{t('ITEMDESC_PROFESSIONREQ')}: {', '.join(nomes)}")
    if item.get("preco"):
        linhas.append(f"{BRANCO}{t('ITEMDESC_PRICE')} {int(item['preco']):,}".replace(",", "."))
    descricao = textos.descricoes().get(tid)
    if descricao:
        linhas.extend(parte for parte in descricao.replace("\\r", "\r").split("\r") if parte)
    return linhas
