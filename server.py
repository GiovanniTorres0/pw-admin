"""PW Admin: painel web para o servidor Perfect World 1.5.5 (v156).

Escuta so em 127.0.0.1; acesso por tunel SSH. Configuracao em config.json (criado na primeira execucao).
Fases implementadas: leitura (contas, personagens), edicao de personagem offline com backup e
acoes ao vivo pelo gdeliveryd (aviso global, correio, kick, mute, ban), e nome, icone e dica
dos itens (itens.json e dados/, ideia e codigo de base do pw-universal-server, MIT).
"""
import functools
import hashlib
import hmac
import http.server
import json
import os
import secrets
import struct
import sys
import threading
import time
import urllib.parse
import zlib

import pymysql

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import dica  # noqa: E402
import equipamentos  # noqa: E402
import icones  # noqa: E402
import pwproto as p  # noqa: E402

CONFIG_PATH = os.path.join(HERE, "config.json")
DEFAULT_CONFIG = {
    "bind": "127.0.0.1",
    "port": 8080,
    "gamedbd": ["127.0.0.1", 29400],
    "gdeliveryd": ["127.0.0.1", 29100],
    "db": {"host": "localhost", "user": "pw", "password": "", "database": "pw"},
    "backup_dir": os.path.join(HERE, "backups"),
    "audit_log": os.path.join(HERE, "audit.log"),
}

CLASSES = dica.CLASSES = ["Guerreiro", "Mago", "Espiritualista", "Feiticeira", "Bárbaro", "Assassino",
           "Arqueiro", "Sacerdote", "Seeker", "Místico", "Duskblade", "Stormbringer"]

# Campos que o painel pode editar: (secao, campo) -> (minimo, maximo)
EDITABLE = {
    ("status", "level"): (1, 105),
    ("status", "exp"): (0, 2**31 - 1),
    ("status", "sp"): (0, 2**31 - 1),
    ("status", "pp"): (0, 2**31 - 1),
    ("status", "hp"): (1, 2**31 - 1),
    ("status", "mp"): (0, 2**31 - 1),
    ("status", "reputation"): (0, 2**31 - 1),
    ("pocket", "money"): (0, 2_000_000_000),
    ("storehouse", "money"): (0, 2_000_000_000),
    # Expansao: o gs le o tamanho daqui no login (userlogin.cpp: bagsize e storesize)
    ("pocket", "capacity"): (32, 64),
    ("storehouse", "capacity"): (16, 120),
}
EXPANSAO = {"pocket": "Bolsa", "storehouse": "Banco"}
EDITABLE_POS = {"posx", "posy", "posz", "worldtag"}

# Atributos base: status.property e o extend_prop do gs (cgame/gs/property.h), little-endian,
# 148 bytes (144 nos personagens antigos, sem max_ap). Os valores derivados seguem o
# player_template do gs (__LevelUp e __UpdateBasic) com o crescimento por classe do
# CHARRACTER_CLASS_CONFIG do elements.data do servidor, copiado em dados/classes.json.
PROP_FMT = "<8i4f4if20i"
PROP_ATRIBUTOS = {"vit": 0, "eng": 1, "str": 2, "agi": 3}
PROP_DERIVADOS = {"hp_max": 4, "mp_max": 5, "dano_min": 13, "dano_max": 14,
                  "magia_min": 27, "magia_max": 28, "defesa": 34}
ATRIBUTO_MIN, ATRIBUTO_MAX = 5, 9999
NOMES_ATRIBUTOS = {"vit": "Constituição", "eng": "Inteligência", "str": "Força", "agi": "Destreza"}
PONTOS_BASE = 50  # MAX_BASE_PP do userlogin.cpp: pp + atributos <= (nivel - 1) * 5 + 50
with open(os.path.join(HERE, "dados", "classes.json"), encoding="utf-8") as _f:
    CRESCIMENTO = json.load(_f)


def _por_nivel(fator, nivel):
    """int(nivel * fator) com fator float, como o gs calcula."""
    return int(struct.unpack("<f", struct.pack("<f", nivel * fator))[0])


def ler_prop(raw):
    if len(raw) == 144:
        raw += bytes(4)
    if len(raw) != 148:
        raise ValueError(f"status.property com {len(raw)} bytes; esperado 148")
    return list(struct.unpack(PROP_FMT, raw))


def recalcular_prop(prop, cls, nivel):
    """HP e MP maximos, dano, magia e defesa base pelo nivel e pelos atributos, como se o
    personagem tivesse subido jogando. Resistencias nao mudam (lvlup_magicdefence e 0 em
    todas as classes deste servidor)."""
    c = CRESCIMENTO[str(cls)]
    prop[4] = c["vit_hp"] * prop[0] + c["lvl_hp"] * (nivel - 1)
    prop[5] = c["eng_mp"] * prop[1] + c["lvl_mp"] * (nivel - 1)
    prop[13] = prop[14] = 1 + _por_nivel(c["lvl_dmg"], nivel) - _por_nivel(c["lvl_dmg"], 1)
    prop[27] = prop[28] = 1 + _por_nivel(c["lvl_magic"], nivel) - _por_nivel(c["lvl_magic"], 1)
    prop[34] = 1 + _por_nivel(c["lvl_defense"], nivel) - _por_nivel(c["lvl_defense"], 1)
    return prop


def pontos_total(nivel):
    return (nivel - 1) * 5 + PONTOS_BASE

# Catalogo de itens do elements.data do cliente (tools/gerar_catalogo_itens.py).
try:
    with open(os.path.join(HERE, "itens.json"), encoding="utf-8") as _f:
        ITENS = json.load(_f)
except FileNotFoundError:
    ITENS = {}


@functools.lru_cache(maxsize=1)
def mapa_icones():
    """id do item -> celula na folha de icones (/api/icones.png). A versao muda quando a folha
    muda, para o navegador nao usar uma folha velha do cache."""
    a = icones.atlas()
    celulas = {}
    for tid, item in ITENS.items():
        n = icones.celula(item["icone"]) if item.get("icone") else None
        if n is not None:
            celulas[tid] = n
    versao = zlib.crc32(icones.folha()) & 0xFFFFFFFF
    return {"colunas": a.colunas, "linhas": a.linhas, "largura": a.largura, "altura": a.altura,
            "versao": f"{versao:08x}", "celulas": celulas}

# Categorias do catalogo: tabelas do elements.data agrupadas. O que nao estiver aqui vai para "outros".
CATEGORIAS = [
    ("armas", "Armas e munição", ("WEAPON_ESSENCE", "PROJECTILE_ESSENCE", "QUIVER_ESSENCE")),
    ("armaduras", "Armaduras", ("ARMOR_ESSENCE",)),
    ("ornamentos", "Ornamentos", ("DECORATION_ESSENCE",)),
    ("moda", "Moda e aparência", ("FASHION_ESSENCE", "DYE_TICKET_ESSENCE", "FACETICKET_ESSENCE",
                                  "FACEPILL_ESSENCE")),
    ("voo", "Voo", ("FLYSWORD_ESSENCE", "WINGMANWING_ESSENCE")),
    ("pocoes", "Poções e consumíveis", ("MEDICINE_ESSENCE", "AUTOHP_ESSENCE", "AUTOMP_ESSENCE",
                                        "DOUBLE_EXP_ESSENCE", "REVIVESCROLL_ESSENCE", "FIREWORKS_ESSENCE",
                                        "FIREWORKS2_ESSENCE", "SPEAKER_ESSENCE")),
    ("teleporte", "Teleporte", ("TOWNSCROLL_ESSENCE", "TRANSMITSCROLL_ESSENCE", "FIX_POSITION_TRANSMIT_ESSENCE",
                                "CONGREGATE_ESSENCE")),
    ("pedras", "Pedras, refino e runas", ("STONE_ESSENCE", "SHARPENER_ESSENCE", "REFINE_TICKET_ESSENCE",
                                          "DAMAGERUNE_ESSENCE", "ARMORRUNE_ESSENCE", "ELEMENT_ESSENCE")),
    ("materiais", "Materiais", ("MATERIAL_ESSENCE", "FACTION_MATERIAL_ESSENCE", "INC_SKILL_ABILITY_ESSENCE")),
    ("habilidades", "Habilidades e livros", ("SKILLTOME_ESSENCE", "SKILLMATTER_ESSENCE", "BIBLE_ESSENCE",
                                             "DYNSKILLEQUIP_ESSENCE", "TARGET_ITEM_ESSENCE")),
    ("pets", "Pets e gênio", ("PET_EGG_ESSENCE", "PET_FOOD_ESSENCE", "PET_FACETICKET_ESSENCE", "GOBLIN_ESSENCE",
                              "GOBLIN_EQUIP_ESSENCE", "GOBLIN_EXPPILL_ESSENCE")),
    ("missao", "Missão", ("TASKNORMALMATTER_ESSENCE", "TASKDICE_ESSENCE", "TASKMATTER_ESSENCE")),
    ("cartas", "Cartas e astrolábio", ("POKER_ESSENCE", "POKER_DICE_ESSENCE", "MONSTER_SPIRIT_ESSENCE",
                                       "ASTROLABE_ESSENCE", "ASTROLABE_INC_INNER_POINT_VALUE_ESSENCE",
                                       "ASTROLABE_INC_EXP_ESSENCE", "ASTROLABE_RANDOM_ADDON_ESSENCE")),
]
_TABELA_CAT = {t: c for c, _, tabelas in CATEGORIAS for t in tabelas}
POR_PAGINA = 120


def _visivel(item):
    """Sem nome ou marcado como teste/abandonado (n/a, N/A...) nao entra no catalogo."""
    nome = item.get("nome", "").strip()
    return bool(nome) and not nome.lower().startswith("n/a")


CATALOGO = {}  # categoria -> [ids], em ordem de id
for _tid in sorted(ITENS, key=int):
    if _visivel(ITENS[_tid]):
        CATALOGO.setdefault(_TABELA_CAT.get(ITENS[_tid]["tabela"], "outros"), []).append(int(_tid))


def list_categories():
    nomes = [(c, n) for c, n, _ in CATEGORIAS] + [("outros", "Outros")]
    return [{"id": c, "nome": n, "total": len(CATALOGO.get(c, []))} for c, n in nomes if CATALOGO.get(c)]


def list_items(query):
    cat = query.get("cat", [""])[0]
    busca = query.get("q", [""])[0].strip().lower()
    pagina = max(0, int(query.get("page", ["0"])[0] or 0))
    if busca:
        # A busca vale para todas as categorias se nenhuma foi escolhida.
        base = CATALOGO.get(cat) if cat else [i for ids in CATALOGO.values() for i in ids]
        ids = [i for i in base if busca == str(i) or busca in ITENS[str(i)]["nome"].lower()]
        if not cat:
            ids.sort()
    else:
        ids = CATALOGO.get(cat, [])
    trecho = ids[pagina * POR_PAGINA:(pagina + 1) * POR_PAGINA]
    return {"total": len(ids), "page": pagina, "per_page": POR_PAGINA,
            "items": [{"id": i, "pilha": ITENS[str(i)].get("pilha", 0)} | item_info(i) for i in trecho]}


SESSIONS = {}
SESSION_TTL = 12 * 3600
write_lock = threading.Lock()


def load_config():
    if not os.path.exists(CONFIG_PATH):
        cfg = dict(DEFAULT_CONFIG)
        password = secrets.token_urlsafe(12)
        cfg["password_sha256"] = hashlib.sha256(password.encode()).hexdigest()
        with open(CONFIG_PATH, "w") as f:
            json.dump(cfg, f, indent=2)
        os.chmod(CONFIG_PATH, 0o600)
        print(f"config.json criado. Senha do painel: {password}", flush=True)
    with open(CONFIG_PATH) as f:
        return {**DEFAULT_CONFIG, **json.load(f)}


CFG = load_config()
os.makedirs(CFG["backup_dir"], exist_ok=True)
GAMEDB = p.RpcClient(*CFG["gamedbd"])
DELIVERY = p.ProtoClient(*CFG["gdeliveryd"])
GAMEDB_PROTO = p.ProtoClient(*CFG["gamedbd"])


def audit(action, **info):
    line = json.dumps({"t": time.strftime("%Y-%m-%d %H:%M:%S"), "action": action, **info},
                      ensure_ascii=False)
    with open(CFG["audit_log"], "a", encoding="utf-8") as f:
        f.write(line + "\n")


def db_query(sql, args=()):
    conn = pymysql.connect(**CFG["db"], charset="latin1", cursorclass=pymysql.cursors.DictCursor)
    try:
        with conn.cursor() as cur:
            cur.execute(sql, args)
            return cur.fetchall()
    finally:
        conn.close()


def online_userids():
    return {r["uid"] for r in db_query("SELECT uid FROM point WHERE zoneid IS NOT NULL")}


# ---------------------------------------------------------------- dados

def em_jogo():
    """{roleid: (userid, nome)} dos personagens em jogo agora (gdeliveryd). O authd so sabe a conta;
    isto diz qual personagem dela esta logado. None se o gdeliveryd nao responder."""
    try:
        return p.online_roles(DELIVERY)
    except Exception:  # noqa: BLE001 - cai para o status por conta
        return None


def gm_userids():
    return {r["userid"] for r in db_query("SELECT DISTINCT userid FROM auth")}


# Indice de personagens em memoria (conta -> personagens), montado em segundo plano. Com milhares
# de contas, listar tudo a cada pedido nao escala: a busca usa o indice e o SQL; contas achadas
# pelo nome ou ID sao relidas na hora, entao personagens novos aparecem sem esperar o indice.
INDICE_INTERVALO = 300  # segundos
BUSCA_MAX = 30
_indice = {}  # userid -> {"name": conta, "roles": [(roleid, nome)]}
_indice_lock = threading.Lock()


def _reler_conta(userid, nome):
    roles = p.get_user_roles(GAMEDB, userid)
    with _indice_lock:
        _indice[userid] = {"name": nome, "roles": roles}
    return roles


def _manter_indice():
    while True:
        try:
            novo = {u["id"]: {"name": u["name"], "roles": p.get_user_roles(GAMEDB, u["id"])}
                    for u in db_query("SELECT id, name FROM users")}
            with _indice_lock:
                _indice.clear()
                _indice.update(novo)
        except Exception as e:  # noqa: BLE001 - tenta de novo no proximo ciclo
            print("indice:", e, file=sys.stderr, flush=True)
        time.sleep(INDICE_INTERVALO)


def nome_conta(userid):
    with _indice_lock:
        if userid in _indice:
            return _indice[userid]["name"]
    rows = db_query("SELECT name FROM users WHERE id=%s", (userid,))
    return rows[0]["name"] if rows else ""


def buscar(q):
    """Personagens por nome, por conta ou por ID (de personagem ou de conta). No maximo BUSCA_MAX."""
    q = q.strip().lstrip("#").strip()
    if not q:
        return []
    online, gms, jogo = online_userids(), gm_userids(), em_jogo()
    grupos = [[] for _ in range(5)]  # ID exato, nome exato, nome comeca, nome contem, conta

    if q.isdigit():
        n = int(q)
        try:
            role, _ = p.get_role(GAMEDB, n)
            grupos[0].append((n, p.role_name(role), role["base"]["userid"]))
        except Exception:  # noqa: BLE001 - nao e ID de personagem
            pass
        for u in db_query("SELECT id, name FROM users WHERE id=%s", (n,)):
            grupos[0] += [(rid, nome, u["id"]) for rid, nome in _reler_conta(u["id"], u["name"])]

    like = "%" + q.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_") + "%"
    for u in db_query("SELECT id, name FROM users WHERE name LIKE %s ORDER BY LENGTH(name), name LIMIT 10", (like,)):
        grupos[4] += [(rid, nome, u["id"]) for rid, nome in _reler_conta(u["id"], u["name"])]

    ql = q.lower()
    with _indice_lock:
        todos = [(rid, nome, uid) for uid, c in _indice.items() for rid, nome in c["roles"]]
    for rid, nome, uid in todos:
        nl = nome.lower()
        if nl == ql:
            grupos[1].append((rid, nome, uid))
        elif nl.startswith(ql):
            grupos[2].append((rid, nome, uid))
        elif ql in nl:
            grupos[3].append((rid, nome, uid))
    for g in grupos[2:4]:
        g.sort(key=lambda x: (len(x[1]), x[1].lower()))

    out, vistos = [], set()
    for rid, nome, uid in (x for g in grupos for x in g):
        if rid in vistos:
            continue
        vistos.add(rid)
        out.append({"id": rid, "name": nome, "userid": uid, "account": nome_conta(uid),
                    "online": rid in jogo if jogo is not None else uid in online, "gm": uid in gms})
        if len(out) >= BUSCA_MAX:
            break
    return out


def lista_online():
    """Personagens em jogo agora. Sem o gdeliveryd, cai para as contas online e todos os personagens delas."""
    gms, jogo = gm_userids(), em_jogo()
    if jogo is not None:
        return sorted(({"id": rid, "name": nome, "userid": uid, "account": nome_conta(uid), "online": True,
                        "gm": uid in gms} for rid, (uid, nome) in jogo.items()), key=lambda x: x["name"].lower())
    out = []
    for uid in sorted(online_userids()):
        with _indice_lock:
            c = _indice.get(uid)
        conta = c["name"] if c else nome_conta(uid)
        roles = c["roles"] if c else _reler_conta(uid, conta)
        out += [{"id": rid, "name": nome, "userid": uid, "account": conta, "online": True, "gm": uid in gms}
                for rid, nome in roles]
    return out


# Mascotes: status.petcorral = GPetCorral (capacity, [GPet(index, data)]) em GNET; data e o
# pet_data do gs (petman.h), little-endian: honor, hunger, feed, pet_tid, vis_tid, egg_tid,
# classe, hp_factor, level, color, exp, skill_point, bind, unused, name_len, name[16].
PET_FMT = "<7ifhHiibbH16s"
PET_MAX = 10  # espacos do curral que o painel aceita
PET_CLASSES = ["Montaria", "Combate", "Seguidor", "Invocado", "Planta", "Evolução"]


def ler_curral(role):
    b = role["status"]["petcorral"]
    if not b:
        return 0, []
    r = p.Reader(b)
    cap, pets = r._take(">i"), []
    for _ in range(r.cuint()):
        idx, data = r._take(">i"), r.octets()
        pets.append({"pos": idx, "id": struct.unpack_from("<i", data, 20)[0], "count": 1, "data": data})
    return cap, pets


def gravar_curral(role, cap, pets):
    w = p.Writer()
    w.put(">i", cap)
    w.cuint(len(pets))
    for pet in sorted(pets, key=lambda x: x["pos"]):
        w.put(">i", pet["pos"])
        w.octets(pet["data"])
    role["status"]["petcorral"] = w.bytes()


def pets_json(role):
    cap, pets = ler_curral(role)
    out = []
    for pet in pets:
        try:
            (_h, _f, _p, pet_tid, _v, egg, classe, hp, level, _c, exp, sp, _b, _u,
             nlen, nome) = struct.unpack_from(PET_FMT, pet["data"])
        except struct.error:
            out.append({"pos": pet["pos"], "id": pet["id"], "count": 1, "nome": "mascote", "dica": ["^ffffffMascote"]})
            continue
        ovo = ITENS.get(str(egg), {}).get("nome", "")
        nome = nome[:max(0, min(nlen, 16))].decode("utf-16-le", "replace") or ovo or f"mascote {pet_tid}"
        tipo = PET_CLASSES[classe] if 0 <= classe < len(PET_CLASSES) else str(classe)
        dica = [f"^ffcb4a{nome}", f"^ffffffNível {level} · {tipo}", f"^ffffffXP {exp} · pontos de perícia {sp}"]
        if hp <= 0:
            dica.append("^ff4040Morto")
        if ovo:
            dica.append(f"^a0a0a0Ovo: {ovo} (#{egg})")
        out.append({"pos": pet["pos"], "id": egg, "count": 1, "nome": nome, "dica": dica})
    return {"capacity": cap, "items": out}


def item_info(tid, count=1):
    item = ITENS.get(str(tid), {})
    return {"nome": item.get("nome", ""), "dica": dica.linhas_da_dica(tid, item, count)}


def inv_json(items):
    return [{k: it[k] for k in ("id", "pos", "count", "max_count", "proctype", "expire_date")}
            | {"data_len": len(it["data"])} | item_info(it["id"], it["count"]) for it in items]


def role_json(role):
    b, s, pk, sh = role["base"], role["status"], role["pocket"], role["storehouse"]
    cls = b["cls"]
    try:
        prop = ler_prop(s["property"])
        atributos = {k: prop[i] for k, i in (PROP_ATRIBUTOS | PROP_DERIVADOS).items()}
        certo = recalcular_prop(list(prop), cls, s["level"])
        atributos["desatualizado"] = certo != prop
    except (ValueError, KeyError):
        atributos = None
    return {
        "atributos": atributos, "pontos_total": pontos_total(s["level"]),
        "crescimento": CRESCIMENTO.get(str(cls)),
        "id": b["id"], "name": p.role_name(role), "userid": b["userid"],
        "cls": cls, "cls_name": CLASSES[cls] if 0 <= cls < len(CLASSES) else str(cls),
        "gender": "feminino" if b["gender"] else "masculino",
        "create_time": b["create_time"], "lastlogin_time": b["lastlogin_time"],
        "spouse": b["spouse"],
        "forbid": [{"type": f["type"], "time": f["time"], "createtime": f["createtime"]}
                   for f in b["forbid"]],
        "status": {k: s[k] for k in ("level", "level2", "exp", "sp", "pp", "hp", "mp", "posx",
                                     "posy", "posz", "worldtag", "reputation", "storesize")},
        "pocket": {"capacity": pk["capacity"], "money": pk["money"], "items": inv_json(pk["items"])},
        "equipment": inv_json(role["equipment"]["inv"]),
        "storehouse": {"capacity": sh["capacity"], "money": sh["money"],
                       "items": inv_json(sh["items"])},
        # Bancos extras do 1.5.5: moda (size1), materiais (size2) e cartas (size3); itens de missao
        "dress": {"capacity": sh["size1"], "items": inv_json(sh["dress"])},
        "material": {"capacity": sh["size2"], "items": inv_json(sh["material"])},
        "generalcard": {"capacity": sh["size3"], "items": inv_json(sh["generalcard"])},
        "task_items": inv_json(role["task"]["task_inventory"]),
        "pets": pets_json(role),
        "online": b["userid"] in online_userids(),  # conta logada: bloqueia gravacao
        "em_jogo": b["id"] in (em_jogo() or {}),
    }


def edit_role(roleid, changes):
    """Aplica mudancas a um personagem offline. Faz backup antes e confere depois."""
    with write_lock:
        role, raw = p.get_role(GAMEDB, roleid)
        if p.encode(p.GRoleData, role, p.Writer()).bytes() != raw:
            raise ValueError("este personagem nao passa no teste de ida e volta; escrita bloqueada")
        if role["base"]["userid"] in online_userids():
            raise ValueError("personagem online: desconecte o jogador antes de editar")

        applied = {}
        novos_atributos = {}
        for key, value in changes.items():
            section, _, field = key.partition(".")
            if section == "attr" and field in PROP_ATRIBUTOS:
                value = int(value)
                if not ATRIBUTO_MIN <= value <= ATRIBUTO_MAX:
                    raise ValueError(f"{NOMES_ATRIBUTOS[field]} = {value}: use um valor entre "
                                     f"{ATRIBUTO_MIN} e {ATRIBUTO_MAX}.")
                novos_atributos[field] = value
                continue
            if key == "pets.capacity":
                value = int(value)
                cap, pets = ler_curral(role)
                if not 1 <= value <= PET_MAX:
                    raise ValueError(f"Mascotes: use de 1 a {PET_MAX} espaços.")
                ultimo = max((x["pos"] + 1 for x in pets), default=0)
                if value < ultimo:
                    raise ValueError(f"Mascotes: há mascote no espaço {ultimo}; tire-o antes de reduzir para {value}.")
                if value != cap:
                    applied[key] = [cap, value]
                    gravar_curral(role, value, pets)
                continue
            if (section, field) in EDITABLE:
                lo, hi = EDITABLE[(section, field)]
                value = int(value)
                if not lo <= value <= hi:
                    raise ValueError(f"{key} fora do intervalo {lo}..{hi}")
                if field == "capacity":
                    if value % 8:
                        raise ValueError(f"{EXPANSAO[section]}: use múltiplos de 8 (uma linha do jogo).")
                    ultimo = max((i["pos"] + 1 for i in role[section]["items"]), default=0)
                    if value < ultimo:
                        raise ValueError(f"{EXPANSAO[section]}: há item no espaço {ultimo}; "
                                         f"tire-o antes de reduzir para {value}.")
            elif section == "status" and field in EDITABLE_POS:
                value = int(value) if field == "worldtag" else float(value)
            else:
                raise ValueError(f"campo nao editavel: {key}")
            if role[section][field] != value:
                applied[key] = [role[section][field], value]
                role[section][field] = value

        # Atributos e status base: aplica os novos atributos e recalcula pelo nivel (tambem
        # corrige personagens que mudaram de nivel sem ganhar HP, dano etc.).
        s = role["status"]
        prop = ler_prop(s["property"])
        antes = list(prop)
        for field, value in novos_atributos.items():
            prop[PROP_ATRIBUTOS[field]] = value
        recalcular_prop(prop, role["base"]["cls"], s["level"])
        usados = s["pp"] + sum(prop[i] for i in PROP_ATRIBUTOS.values())
        if usados > pontos_total(s["level"]):
            raise ValueError(
                f"Pontos demais: {usados} de {pontos_total(s['level'])} do nível {s['level']}. "
                "Tire pontos ou suba o nível.")
        prop_nova = None
        if prop != antes:
            for nome, i in (PROP_ATRIBUTOS | PROP_DERIVADOS).items():
                if prop[i] != antes[i]:
                    applied[f"attr.{nome}"] = [antes[i], prop[i]]
            prop_nova = struct.pack(PROP_FMT, *prop)
            s["property"] = prop_nova
        if not applied:
            return {"applied": {}}

        backup = os.path.join(CFG["backup_dir"], f"{roleid}_{time.strftime('%Y%m%d_%H%M%S')}.bin")
        with open(backup, "wb") as f:
            f.write(raw)
        retcode = p.put_role(GAMEDB, roleid, role)
        if retcode != 0:
            raise ValueError(f"PutRoleData retcode={retcode}")
        check, _ = p.get_role(GAMEDB, roleid)
        if prop_nova is not None and check["status"]["property"] != prop_nova:
            raise ValueError(f"gravado, mas os atributos voltaram diferentes; backup em {backup}")
        for key, (_, new) in applied.items():
            section, _, field = key.partition(".")
            if section == "attr":
                continue
            if check[section][field] != new:
                raise ValueError(f"gravado, mas {key} voltou diferente; backup em {backup}")
        audit("edit_role", roleid=roleid, name=p.role_name(role), changes=applied, backup=backup)
        return {"applied": applied, "backup": os.path.basename(backup)}


def add_item(roleid, body):
    """Poe um item no primeiro espaco livre da bolsa ou do banco de um personagem offline (backup
    antes, confere depois). O corpo do item vem de equipamentos.montar."""
    destino = body.get("local", "pocket")
    if destino not in EXPANSAO:
        raise ValueError("destino invalido: use bolsa ou banco")
    tid = int(body.get("item_id") or 0)
    count = int(body.get("count") or 1)
    engastes = int(body.get("engastes") or 0)
    item = ITENS.get(str(tid))
    if item is None:
        raise ValueError(f"item {tid} nao existe no elements.data do cliente")
    pilha = item.get("pilha") or 1
    if not 1 <= count <= pilha:
        raise ValueError(f"{item['nome']}: quantidade de 1 a {pilha}")
    novo = equipamentos.montar(tid, count, item, engastes)
    with write_lock:
        role, raw = p.get_role(GAMEDB, roleid)
        if p.encode(p.GRoleData, role, p.Writer()).bytes() != raw:
            raise ValueError("este personagem nao passa no teste de ida e volta; escrita bloqueada")
        if role["base"]["userid"] in online_userids():
            raise ValueError("personagem online: desconecte o jogador antes de editar")
        bolsa = role[destino]
        ocupados = {it["pos"] for it in bolsa["items"]}
        livre = next((i for i in range(bolsa["capacity"]) if i not in ocupados), None)
        if livre is None:
            raise ValueError(f"{EXPANSAO[destino]}: sem espaço livre")
        bolsa["items"].append(novo | {"pos": livre})
        bolsa["items"].sort(key=lambda it: it["pos"])

        backup = os.path.join(CFG["backup_dir"], f"{roleid}_{time.strftime('%Y%m%d_%H%M%S')}.bin")
        with open(backup, "wb") as f:
            f.write(raw)
        retcode = p.put_role(GAMEDB, roleid, role)
        if retcode != 0:
            raise ValueError(f"PutRoleData retcode={retcode}")
        check, _ = p.get_role(GAMEDB, roleid)
        if not any(it["pos"] == livre and it["id"] == tid and it["data"] == novo["data"]
                   for it in check[destino]["items"]):
            raise ValueError(f"gravado, mas o item nao apareceu ({EXPANSAO[destino]}); backup em {backup}")
        audit("add_item", roleid=roleid, name=p.role_name(role), item_id=tid, count=count,
              engastes=engastes, local=destino, pos=livre, backup=backup)
        return {"ok": True, "pos": livre, "nome": item["nome"], "backup": os.path.basename(backup)}


LOCAIS = {"pocket": lambda r: r["pocket"]["items"], "storehouse": lambda r: r["storehouse"]["items"],
          "equipment": lambda r: r["equipment"]["inv"], "dress": lambda r: r["storehouse"]["dress"],
          "material": lambda r: r["storehouse"]["material"],
          "generalcard": lambda r: r["storehouse"]["generalcard"],
          "task": lambda r: r["task"]["task_inventory"], "pets": lambda r: ler_curral(r)[1]}


def remove_item(roleid, body):
    """Tira um item (ou parte da pilha) da bolsa, do banco ou do equipado de um personagem
    offline. So mexe neste personagem: confere id e posicao contra o que esta gravado agora
    (pagina desatualizada nao apaga o item errado), faz backup e confere depois."""
    local = str(body.get("local", ""))
    pos = int(body.get("pos", -1))
    tid = int(body.get("item_id") or 0)
    count = int(body.get("count") or 0)
    if local not in LOCAIS:
        raise ValueError("local invalido")
    with write_lock:
        role, raw = p.get_role(GAMEDB, roleid)
        if p.encode(p.GRoleData, role, p.Writer()).bytes() != raw:
            raise ValueError("este personagem nao passa no teste de ida e volta; escrita bloqueada")
        if role["base"]["userid"] in online_userids():
            raise ValueError("personagem online: desconecte o jogador antes de editar")
        itens = LOCAIS[local](role)
        alvo = next((it for it in itens if it["pos"] == pos and it["id"] == tid), None)
        if alvo is None:
            raise ValueError("o item nao esta mais nesse lugar; atualize a pagina")
        antes = alvo["count"]
        if not 1 <= count <= antes:
            raise ValueError(f"quantidade de 1 a {antes}")
        if count == antes:
            itens.remove(alvo)
        else:
            alvo["count"] = antes - count
        if local == "pets":
            gravar_curral(role, ler_curral(role)[0], itens)

        backup = os.path.join(CFG["backup_dir"], f"{roleid}_{time.strftime('%Y%m%d_%H%M%S')}.bin")
        with open(backup, "wb") as f:
            f.write(raw)
        retcode = p.put_role(GAMEDB, roleid, role)
        if retcode != 0:
            raise ValueError(f"PutRoleData retcode={retcode}")
        check, _ = p.get_role(GAMEDB, roleid)
        resto = next((it["count"] for it in LOCAIS[local](check) if it["pos"] == pos and it["id"] == tid), 0)
        if resto != antes - count:
            raise ValueError(f"gravado, mas a bolsa nao ficou como esperado; backup em {backup}")
        audit("remove_item", roleid=roleid, name=p.role_name(role), local=local, pos=pos, item_id=tid,
              count=count, backup=backup)
        return {"ok": True, "restante": resto, "backup": os.path.basename(backup)}


def list_backups(roleid):
    prefix = f"{roleid}_"
    return sorted((f for f in os.listdir(CFG["backup_dir"]) if f.startswith(prefix)), reverse=True)


def restore_role(roleid, filename):
    if filename not in list_backups(roleid):
        raise ValueError("backup nao encontrado")
    with write_lock:
        if p.get_role(GAMEDB, roleid)[0]["base"]["userid"] in online_userids():
            raise ValueError("personagem online: desconecte o jogador antes de restaurar")
        with open(os.path.join(CFG["backup_dir"], filename), "rb") as f:
            raw = f.read()
        role = p.decode(p.GRoleData, p.Reader(raw))
        current_raw = p.get_role(GAMEDB, roleid)[1]
        safety = os.path.join(CFG["backup_dir"], f"{roleid}_{time.strftime('%Y%m%d_%H%M%S')}.bin")
        with open(safety, "wb") as f:
            f.write(current_raw)
        retcode = p.put_role(GAMEDB, roleid, role)
        if retcode != 0:
            raise ValueError(f"PutRoleData retcode={retcode}")
        audit("restore_role", roleid=roleid, source=filename, safety_backup=os.path.basename(safety))
        return {"restored": filename, "backup": os.path.basename(safety)}


# ---------------------------------------------------------------- acoes ao vivo (gdeliveryd)

MAX_MINUTES = 365 * 24 * 60


def _minutes(body):
    minutes = int(body.get("minutes", 0))
    if not 0 <= minutes <= MAX_MINUTES:
        raise ValueError(f"minutos fora do intervalo 0..{MAX_MINUTES}")
    return minutes


def _text(body, key, max_bytes, required=True):
    text = str(body.get(key, "")).strip()
    if required and not text:
        raise ValueError(f"{key} vazio")
    if len(text.encode("utf-16-le")) > max_bytes:
        raise ValueError(f"{key} longo demais (máx. {max_bytes // 2} caracteres)")
    return text


def live_broadcast(body):
    text = _text(body, "text", 400)
    p.broadcast(DELIVERY, text)
    audit("broadcast", text=text)
    return {"ok": True}


def live_kick(roleid, body):
    # forbid_time 1 s: derruba sem deixar bloqueio (com 0 o gdeliveryd so remove o bloqueio)
    retcode = p.kickout_role(DELIVERY, roleid, 1, _text(body, "reason", 200, required=False))
    audit("kick", roleid=roleid, retcode=retcode)
    return {"retcode": retcode}


def live_ban(roleid, body):
    minutes = _minutes(body)
    reason = _text(body, "reason", 200, required=False)
    retcode = p.kickout_role(DELIVERY, roleid, minutes * 60, reason)
    audit("ban" if minutes else "unban", roleid=roleid, minutes=minutes, reason=reason,
          retcode=retcode)
    return {"retcode": retcode}


def live_mute(roleid, body):
    minutes = _minutes(body)
    reason = _text(body, "reason", 200, required=False)
    p.shutup_role(DELIVERY, roleid, minutes * 60, reason)
    audit("mute" if minutes else "unmute", roleid=roleid, minutes=minutes, reason=reason)
    return {"ok": True}


def live_mail(roleid, body):
    title = _text(body, "title", 40)
    text = _text(body, "text", 400, required=False)
    money = int(body.get("money") or 0)
    item_id = int(body.get("item_id") or 0)
    count = int(body.get("count") or 0) if item_id else 0
    if not 0 <= money <= 2_000_000_000:
        raise ValueError("gold fora do intervalo 0..2.000.000.000")
    if item_id and not (0 < item_id < 200000 and 1 <= count <= 9999):
        raise ValueError("item: id 1..199999 e quantidade 1..9999")
    item = ITENS.get(str(item_id), {}) if item_id else {}
    if item_id and ITENS and not item:
        raise ValueError(f"item {item_id} nao existe no elements.data do cliente")
    pilha = item.get("pilha") or 0
    if pilha and count > pilha:
        raise ValueError(f"{item['nome']}: a pilha maxima e {pilha}")
    # Item so vai com o corpo montado: o gs descarta, ao pegar do correio, o item cujo corpo nao le
    # (Cajado Divino 36885, voo, sumiu assim em 2026-10-30). Tipo que o painel nao monta e recusado.
    objeto = None
    if item_id:
        objeto = equipamentos.montar(item_id, count, item, int(body.get("engastes") or 0))
    retcode = p.send_mail(DELIVERY, roleid, title, text, money, item_id, count, pilha,
                          item.get("proc") or 0, objeto)
    audit("mail", roleid=roleid, title=title, money=money, item_id=item_id, count=count,
          retcode=retcode)
    if retcode != 0:
        raise ValueError(f"SysSendMail retcode={retcode}")
    return {"ok": True}


CASH_MAX = 10_000_000  # 100.000 de cash no jogo por envio


def add_cash(body):
    """Cash direto na conta pelo DebugAddCash do gamedbd (o authd daqui nao le a fila usecashnow).
    O gamedbd soma em user.cash_add (e na pontuacao do VIP de cash); o jogo mostra ao relogar."""
    userid = int(body.get("userid") or 0)
    gold = int(body.get("cash") or 0)  # em cash do jogo; o banco guarda x100
    if not db_query("SELECT id FROM users WHERE id = %s", (userid,)):
        raise ValueError(f"conta {userid} nao existe")
    if not 1 <= gold * 100 <= CASH_MAX:
        raise ValueError(f"cash fora do intervalo 1..{CASH_MAX // 100}")
    retcode = p.add_cash(GAMEDB_PROTO, userid, gold * 100)
    audit("cash", userid=userid, cash=gold, retcode=retcode)
    if retcode != 0:
        raise ValueError(f"DebugAddCash retcode={retcode}")
    return {"ok": True, "online": userid in online_userids()}


LIVE_ROLE_ACTIONS = {"kick": live_kick, "ban": live_ban, "mute": live_mute, "mail": live_mail}


# ---------------------------------------------------------------- HTTP

class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "PWAdmin"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8", headers=None):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", (headers or {}).pop("Cache-Control", "no-store"))
        self.send_header("X-Frame-Options", "DENY")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _session(self):
        for part in self.headers.get("Cookie", "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "pwadmin" and SESSIONS.get(v, 0) > time.time():
                return v
        return None

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/":
            with open(os.path.join(HERE, "index.html"), "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        if not self._session():
            return self._send(401, {"error": "login necessario"})
        try:
            if path == "/api/busca":
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("q", [""])[0]
                return self._send(200, buscar(q[:40]))
            if path == "/api/online":
                return self._send(200, lista_online())
            if path == "/api/categorias":
                return self._send(200, list_categories())
            if path == "/api/itens":
                return self._send(200, list_items(urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)))
            if path == "/api/icones.json":
                return self._send(200, mapa_icones(), headers={"Cache-Control": "private, max-age=86400"})
            if path == "/api/icones.png":
                return self._send(200, icones.folha(), "image/png",
                                  {"Cache-Control": "private, max-age=2592000, immutable"})
            parts = path.strip("/").split("/")
            if len(parts) == 3 and parts[:2] == ["api", "icone"] and parts[2].endswith(".png"):
                item = ITENS.get(parts[2][:-4].lstrip("0") or "0", {})
                png = icones.png_do_icone(item["icone"]) if item.get("icone") else None
                if png is None:
                    return self._send(404, {"error": "sem icone"})
                return self._send(200, png, "image/png", {"Cache-Control": "private, max-age=86400"})
            if len(parts) == 3 and parts[:2] == ["api", "item"]:
                tid = int(parts[2])
                item = ITENS.get(str(tid))
                if item is None:
                    return self._send(404, {"error": "item nao existe"})
                return self._send(200, {"id": tid, "pilha": item.get("pilha", 0)} | item_info(tid)
                                  | {"criar": equipamentos.suporte(tid, item)})
            if len(parts) == 4 and parts[:2] == ["api", "role"] and parts[3] == "versao":
                # Impressao digital leve para a atualizacao automatica da interface
                role, raw = p.get_role(GAMEDB, int(parts[2]))
                uid = role["base"]["userid"]
                return self._send(200, {"v": hashlib.sha1(raw).hexdigest()[:16],
                                        "online": uid in online_userids(),
                                        "em_jogo": role["base"]["id"] in (em_jogo() or {})})
            if len(parts) == 3 and parts[:2] == ["api", "role"]:
                role, raw = p.get_role(GAMEDB, int(parts[2]))
                return self._send(200, role_json(role) | {"backups": list_backups(int(parts[2])),
                                                          "versao": hashlib.sha1(raw).hexdigest()[:16],
                                                          "account": nome_conta(role["base"]["userid"])})
            return self._send(404, {"error": "nao encontrado"})
        except Exception as e:  # noqa: BLE001 - devolve o erro para a interface
            return self._send(500, {"error": str(e)})

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        # Exigir o cabecalho bloqueia formularios de outros sites (CSRF).
        if self.headers.get("X-PWAdmin") != "1":
            return self._send(403, {"error": "cabecalho ausente"})
        try:
            body = self._body()
            if path == "/api/login":
                digest = hashlib.sha256(str(body.get("password", "")).encode()).hexdigest()
                if not hmac.compare_digest(digest, CFG["password_sha256"]):
                    time.sleep(1)
                    audit("login_fail", ip=self.client_address[0])
                    return self._send(403, {"error": "senha incorreta"})
                token = secrets.token_urlsafe(32)
                SESSIONS[token] = time.time() + SESSION_TTL
                audit("login", ip=self.client_address[0])
                return self._send(200, {"ok": True}, headers={
                    "Set-Cookie": f"pwadmin={token}; HttpOnly; SameSite=Strict; Path=/"})
            if not self._session():
                return self._send(401, {"error": "login necessario"})
            parts = path.strip("/").split("/")
            if len(parts) == 4 and parts[:2] == ["api", "role"] and parts[3] == "edit":
                return self._send(200, edit_role(int(parts[2]), body.get("changes", {})))
            if len(parts) == 4 and parts[:2] == ["api", "role"] and parts[3] == "additem":
                return self._send(200, add_item(int(parts[2]), body))
            if len(parts) == 4 and parts[:2] == ["api", "role"] and parts[3] == "removeitem":
                return self._send(200, remove_item(int(parts[2]), body))
            if len(parts) == 4 and parts[:2] == ["api", "role"] and parts[3] == "restore":
                return self._send(200, restore_role(int(parts[2]), str(body.get("backup", ""))))
            if len(parts) == 4 and parts[:2] == ["api", "role"] and parts[3] in LIVE_ROLE_ACTIONS:
                return self._send(200, LIVE_ROLE_ACTIONS[parts[3]](int(parts[2]), body))
            if path == "/api/broadcast":
                return self._send(200, live_broadcast(body))
            if path == "/api/cash":
                return self._send(200, add_cash(body))
            return self._send(404, {"error": "nao encontrado"})
        except (ValueError, p.RpcError) as e:
            return self._send(400, {"error": str(e)})
        except Exception as e:  # noqa: BLE001
            return self._send(500, {"error": str(e)})


def main():
    threading.Thread(target=_manter_indice, daemon=True).start()
    srv = http.server.ThreadingHTTPServer((CFG["bind"], CFG["port"]), Handler)
    print(f"PW Admin em http://{CFG['bind']}:{CFG['port']}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
