"""Gera itens.json (id -> nome, icone, tabela, pilha maxima...) a partir do elements.data do cliente.

Uso: python -I gerar_catalogo_itens.py <elements.data> <saida.json>

pw_elements_reader.py e v156.json vem do pw-universal-server (MIT, Murillo Andre Maleski),
pasta specs/elements_layouts/. O leitor segue o elementdataman::load_data do cliente e
exige que o arquivo feche no ultimo byte.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pw_elements_reader as reader  # noqa: E402

# Campos guardados de cada item (quando a tabela tem). O resto fica de fora para o JSON ser pequeno.
CAMPOS = {"pile_num_max": "pilha", "price": "preco", "level": "nivel", "require_level": "nivel_req",
          "proc_type": "proc"}


def icone(caminho):
    """file_icon vem em GBK, ex. b'Surfaces\\\\IconSet\\\\...\\\\xxx.dds'; o atlas usa so o nome, minusculo."""
    nome = caminho.decode("gbk", "replace").replace("/", "\\").rsplit("\\", 1)[-1]
    return nome.lower()


def main(origem, destino):
    tabelas = reader.load_elements_data(origem)
    itens = {}
    for tabela, registros in tabelas.items():
        if not registros or "pile_num_max" not in registros[0] and tabela != "QUIVER_ESSENCE":
            continue
        for r in registros:
            item = {"nome": r["Name"], "tabela": tabela}
            if "file_icon" in r:
                item["icone"] = icone(r["file_icon"])
            item.update({curto: r[campo] for campo, curto in CAMPOS.items() if campo in r})
            itens[str(r["ID"])] = item
    with open(destino, "w", encoding="utf-8") as f:
        json.dump(itens, f, ensure_ascii=False, separators=(",", ":"))
    print(f"{len(itens)} itens em {destino}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
