"""Gera dados/equipamentos.json a partir do elements.data do SERVIDOR (gamed/config/elements.data).

Uso: python -I gerar_equipamentos.py <elements.data do servidor> <saida.json>

Guarda, por item, so o que o gs usa para montar o corpo de armas, armaduras e ornamentos
(cgame/gs/template/generate_item_temp.h: generate_weapon, generate_armor, generate_decoration).
O painel monta o item com os valores maximos de cada faixa (ver equipamentos.py).
Pocoes, voos, moda e ovos de pet tem corpo fixo e ja saem prontos em "dados".
"""
import json
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pw_elements_reader as reader  # noqa: E402

# Subtipos de arma que o gs nao gera (sem animacao).
SUBTIPOS_SEM_ACAO = {300, 293, 76, 291}


def requisitos(e):
    return [e["require_level"], e["character_combo_id"] & 0xFFFF, e["require_strength"],
            e["require_tili"], e["require_agility"], e["require_energy"]]


def comum(e, tabela, mascara, engastes):
    return {"tabela": tabela, "req": requisitos(e), "dur": e["durability_max"], "pilha": e["pile_num_max"],
            "proc": e["proc_type"], "mascara": mascara, "engastes": engastes}


def resistencias(e):
    return [e[f"magic_defences_{i}_high"] for i in range(1, 6)]


def main(origem, destino):
    t = reader.load_elements_data(origem)
    subarma = {s["ID"]: s for s in t["WEAPON_SUB_TYPE"]}
    subarmadura = {s["ID"]: s["equip_mask"] for s in t["ARMOR_SUB_TYPE"]}
    subornamento = {s["ID"]: s["equip_mask"] for s in t["DECORATION_SUB_TYPE"]}
    out = {}
    for e in t["WEAPON_ESSENCE"]:
        sub = subarma.get(e["id_sub_type"])
        if sub is None or e["id_sub_type"] in SUBTIPOS_SEM_ACAO:
            continue
        x = comum(e, "WEAPON_ESSENCE", 0x0001, 2)
        x["corpo"] = {"alcance_modo": e["short_range_mode"], "tipo": e["id_major_type"], "nivel": e["level"],
                      "municao": e["require_projectile"], "dano_min": e["damage_low"],
                      "dano_max": e["damage_high_max"], "magia_min": e["magic_damage_low"],
                      "magia_max": e["magic_damage_high_max"],
                      "velocidade": int(sub["attack_speed"] * 20 + 0.1),
                      "alcance": e["attack_range"], "alcance_curto": sub["attack_short_range"]}
        out[str(e["ID"])] = x
    for e in t["ARMOR_ESSENCE"]:
        mascara = subarmadura.get(e["id_sub_type"])
        if mascara is None:
            continue
        x = comum(e, "ARMOR_ESSENCE", mascara, 4)
        x["corpo"] = {"defesa": e["defence_high"], "esquiva": e["armor_enhance_high"],
                      "mp": e["mp_enhance_high"], "hp": e["hp_enhance_high"], "resist": resistencias(e)}
        out[str(e["ID"])] = x
    for e in t["DECORATION_ESSENCE"]:
        mascara = subornamento.get(e["id_sub_type"])
        if mascara is None:
            continue
        x = comum(e, "DECORATION_ESSENCE", mascara, 0)
        x["corpo"] = {"dano": e["damage_high"], "magia": e["magic_damage_high"], "defesa": e["defence_high"],
                      "esquiva": e["armor_enhance_high"], "resist": resistencias(e)}
        out[str(e["ID"])] = x
    # Itens de corpo fixo: o corpo vai pronto em "dados" (hex). Marca do fabricante: 3 (loja), sem nome.
    marca = struct.pack("<bb", 3, 0)
    for e in t["MEDICINE_ESSENCE"]:  # generate_medicine
        recarga = e["cool_time"] * 100 if e["cool_time"] < 1000 else e["cool_time"]
        corpo = {1794: (e["hp_add_total"], e["hp_add_time"], recarga, e["require_level"]),
                 1802: (e["mp_add_total"], e["mp_add_time"], recarga, e["require_level"]),
                 1810: (e["mp_add_total"], e["hp_add_total"], recarga, e["require_level"]),
                 1815: (recarga, e["require_level"]), 2038: (recarga, e["require_level"])}.get(e["id_major_type"])
        if corpo is None:
            continue
        out[str(e["ID"])] = {"tabela": "MEDICINE_ESSENCE", "pilha": e["pile_num_max"], "proc": e["proc_type"],
                             "mascara": 0, "dados": struct.pack(f"<{len(corpo)}i", *corpo).hex()}
    for e in t["FLYSWORD_ESSENCE"]:  # generate_flysword, com combustivel e velocidades no maximo
        tempo = int(e["time_max_max"])
        corpo = struct.pack("<iihbbiIff", tempo, tempo, e["require_player_level_min"], e["level"], 0,
                            e["character_combo_id"], int(e["time_increase_per_element"]),
                            e["speed_increase_max"], e["speed_rush_increase_max"]) + marca
        out[str(e["ID"])] = {"tabela": "FLYSWORD_ESSENCE", "pilha": e["pile_num_max"], "proc": e["proc_type"],
                             "mascara": 0x1000, "dados": corpo.hex()}
    submoda = {s["ID"]: s["equip_fashion_mask"] for s in t["FASHION_SUB_TYPE"]}
    for e in t["FASHION_ESSENCE"]:  # generate_fashion_item, na cor branca (0x7FFF, da para tingir)
        mascara = submoda.get(e["id_sub_type"])
        if mascara is None:
            continue
        corpo = struct.pack("<iHH", e["require_level"], 0x7FFF, e["gender"] & 0xFFFF) + marca
        out[str(e["ID"])] = {"tabela": "FASHION_ESSENCE", "pilha": e["pile_num_max"], "proc": e["proc_type"],
                             "mascara": mascara, "dados": corpo.hex()}
    pets = {p["ID"]: p for p in t["PET_ESSENCE"]}
    classe_pet = {8781: 0, 8782: 1, 8783: 2, 28752: 3, 28913: 4, 37698: 5}  # id_type -> pet_class do gs
    for e in t["PET_EGG_ESSENCE"]:  # generate_pet_egg
        pet = pets.get(e["id_pet"])
        if pet is None:
            continue
        classe = classe_pet.get(pet["id_type"], 4)
        habilidades = [(e[f"skills_{i}_id_skill"], e[f"skills_{i}_level"]) for i in range(1, 33)
                       if e[f"skills_{i}_id_skill"] > 0 and e[f"skills_{i}_level"] > 0]
        corpo = (struct.pack("<7ihHiihh", pet["level_require"], pet["character_combo_id"], e["honor_point"],
                             e["id_pet"], 0, e["ID"], classe, e["level"], 0, e["exp"], e["skill_point"],
                             0, len(habilidades)) + bytes(16)
                 + b"".join(struct.pack("<ii", *h) for h in habilidades)
                 + (bytes(24) if classe == 5 else b""))  # _evo_prop (6 int) do pet evoluido
        out[str(e["ID"])] = {"tabela": "PET_EGG_ESSENCE", "pilha": e["pile_num_max"], "proc": e["proc_type"],
                             "mascara": 0, "dados": corpo.hex()}
    with open(destino, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    print(f"{len(out)} equipamentos em {destino}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
