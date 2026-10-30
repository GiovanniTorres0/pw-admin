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
import re
import os
import secrets
import shutil
import struct
import subprocess
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


SESSIONS = {}  # token -> (expira, nome do admin)
SESSION_TTL = 12 * 3600
CTX = threading.local()  # admin e IP da requisicao atual, para a auditoria
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
BACKUP_DIAS = 30  # backups mais velhos que isso sao apagados (o mais recente de cada personagem fica)
_NOME_BACKUP = re.compile(r"^(\d+)_(\d{8})_(\d{6})(?:_\d+)?\.bin$")


def pasta_backup(roleid):
    """Uma pasta por personagem: abrir um personagem nao varre os backups de todos."""
    return os.path.join(CFG["backup_dir"], str(int(roleid)))


def arquivo_backup(roleid, nome):
    return os.path.join(pasta_backup(roleid), nome)


def novo_backup(roleid, raw):
    """Grava os bytes do personagem num backup novo e devolve o nome do arquivo."""
    os.makedirs(pasta_backup(roleid), exist_ok=True)
    base = f"{roleid}_{time.strftime('%Y%m%d_%H%M%S')}"
    nome, n = f"{base}.bin", 1
    while os.path.exists(arquivo_backup(roleid, nome)):
        n += 1
        nome = f"{base}_{n}.bin"
    with open(arquivo_backup(roleid, nome), "wb") as f:
        f.write(raw)
    return nome


def _migrar_backups():
    """Backups antigos ficavam todos na raiz: move cada um para a pasta do personagem."""
    for nome in os.listdir(CFG["backup_dir"]):
        m = _NOME_BACKUP.match(nome)
        if m and os.path.isfile(os.path.join(CFG["backup_dir"], nome)):
            os.makedirs(pasta_backup(m.group(1)), exist_ok=True)
            os.replace(os.path.join(CFG["backup_dir"], nome), arquivo_backup(m.group(1), nome))


def _limpar_backups():
    limite = time.strftime("%Y%m%d", time.localtime(time.time() - BACKUP_DIAS * 86400))
    apagados = 0
    for rid in os.listdir(CFG["backup_dir"]):
        if not rid.isdigit():
            continue
        nomes = sorted((n for n in os.listdir(pasta_backup(rid)) if _NOME_BACKUP.match(n)), reverse=True)
        for nome in nomes[1:]:
            if _NOME_BACKUP.match(nome).group(2) < limite:
                os.remove(arquivo_backup(rid, nome))
                apagados += 1
    return apagados


_migrar_backups()
GAMEDB = p.RpcClient(*CFG["gamedbd"])
DELIVERY = p.ProtoClient(*CFG["gdeliveryd"])
GAMEDB_PROTO = p.ProtoClient(*CFG["gamedbd"])


def audit(action, **info):
    """Registra a acao na tabela pwadmin_auditoria, com o admin e o IP da requisicao. Se o banco
    falhar, vai para o audit.log, para nenhuma acao ficar sem registro."""
    admin = info.pop("admin", None) or getattr(CTX, "admin", "") or "sistema"
    ip = info.pop("ip", None) or getattr(CTX, "ip", "")
    backup = info.get("backup") or info.get("safety_backup")
    backup = os.path.basename(backup) if backup else None
    dados = json.dumps(info, ensure_ascii=False, default=str)
    try:
        db_exec("INSERT INTO pwadmin_auditoria (quando, admin, ip, acao, roleid, userid, backup, dados) "
                "VALUES (NOW(), %s, %s, %s, %s, %s, %s, %s)",
                (admin, ip, action, info.get("roleid"), info.get("userid"), backup, dados))
    except Exception as e:  # noqa: BLE001 - nunca perder o registro
        line = json.dumps({"t": time.strftime("%Y-%m-%d %H:%M:%S"), "admin": admin, "ip": ip, "action": action,
                           **info, "erro_banco": str(e)}, ensure_ascii=False, default=str)
        with open(CFG["audit_log"], "a", encoding="utf-8") as f:
            f.write(line + "\n")


def db_exec(sql, args=()):
    """Tabelas do painel (utf8mb4; as do authd usam latin1)."""
    conn = pymysql.connect(**CFG["db"], charset="utf8mb4", cursorclass=pymysql.cursors.DictCursor,
                           autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute(sql, args)
            return cur.fetchall()
    finally:
        conn.close()


TABELAS = [
    """CREATE TABLE IF NOT EXISTS pwadmin_admins (
        id INT AUTO_INCREMENT PRIMARY KEY,
        nome VARCHAR(32) NOT NULL UNIQUE,
        senha VARCHAR(200) NOT NULL,
        ativo TINYINT NOT NULL DEFAULT 1,
        criado DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        ultimo_login DATETIME NULL
    ) DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS pwadmin_auditoria (
        id BIGINT AUTO_INCREMENT PRIMARY KEY,
        quando DATETIME NOT NULL,
        admin VARCHAR(32) NOT NULL,
        ip VARCHAR(45) NOT NULL DEFAULT '',
        acao VARCHAR(32) NOT NULL,
        roleid INT NULL,
        userid INT NULL,
        backup VARCHAR(80) NULL,
        dados TEXT NOT NULL,
        KEY k_role (roleid, id),
        KEY k_backup (backup),
        KEY k_quando (quando),
        KEY k_admin (admin, id)
    ) DEFAULT CHARSET=utf8mb4""",
    # Um registro por varredura: total de gold no jogo, para ver saltos (duplicacao)
    """CREATE TABLE IF NOT EXISTS pwadmin_economia (
        quando DATETIME NOT NULL PRIMARY KEY,
        personagens INT NOT NULL,
        bolsa BIGINT NOT NULL,
        banco BIGINT NOT NULL
    ) DEFAULT CHARSET=utf8mb4""",
    # Admins de antes dos niveis continuam com tudo
    "ALTER TABLE pwadmin_admins ADD COLUMN IF NOT EXISTS nivel VARCHAR(10) NOT NULL DEFAULT 'admin'",
]


def preparar_banco():
    for sql in TABELAS:
        db_exec(sql)
    # Primeiro uso: a senha antiga do config.json vira o admin "admin"
    if not db_exec("SELECT id FROM pwadmin_admins LIMIT 1") and CFG.get("password_sha256"):
        db_exec("INSERT INTO pwadmin_admins (nome, senha) VALUES ('admin', %s)", ("sha256$" + CFG["password_sha256"],))
    # audit.log antigo entra na tabela uma vez
    if os.path.exists(CFG["audit_log"]):
        with open(CFG["audit_log"], encoding="utf-8") as f:
            linhas = [json.loads(x) for x in f if x.strip()]
        for e in linhas:
            acao, quando = e.pop("action", "?"), e.pop("t", None)
            admin, ip = e.pop("admin", "antigo"), e.pop("ip", "")
            backup = e.get("backup") or e.get("safety_backup")
            db_exec("INSERT INTO pwadmin_auditoria (quando, admin, ip, acao, roleid, userid, backup, dados) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                    (quando, admin, ip, acao, e.get("roleid"), e.get("userid"),
                     os.path.basename(backup) if backup else None, json.dumps(e, ensure_ascii=False)))
        os.replace(CFG["audit_log"], CFG["audit_log"] + f".importado_{time.strftime('%Y%m%d_%H%M%S')}")


# Senhas: PBKDF2-SHA256 com sal. "sha256$..." e o formato antigo do config.json, trocado no 1o login.
PBKDF2_ITER = 200_000


def hash_senha(senha):
    sal = secrets.token_bytes(16)
    h = hashlib.pbkdf2_hmac("sha256", senha.encode(), sal, PBKDF2_ITER)
    return f"pbkdf2${PBKDF2_ITER}${sal.hex()}${h.hex()}"


def confere_senha(senha, guardado):
    if guardado.startswith("sha256$"):
        return hmac.compare_digest(hashlib.sha256(senha.encode()).hexdigest(), guardado[7:])
    _, it, sal, h = guardado.split("$")
    calc = hashlib.pbkdf2_hmac("sha256", senha.encode(), bytes.fromhex(sal), int(it))
    return hmac.compare_digest(calc.hex(), h)


def login_admin(nome, senha):
    rows = db_exec("SELECT id, senha FROM pwadmin_admins WHERE nome=%s AND ativo=1", (nome,))
    if not rows or not confere_senha(senha, rows[0]["senha"]):
        return False
    novo = hash_senha(senha) if rows[0]["senha"].startswith("sha256$") else rows[0]["senha"]
    db_exec("UPDATE pwadmin_admins SET ultimo_login=NOW(), senha=%s WHERE id=%s", (novo, rows[0]["id"]))
    return True


# Niveis: suporte so ve; GM edita personagem e faz moderacao; admin restaura backup, da cash,
# cuida de contas, de GMs e do servidor.
NIVEIS = {"suporte": 1, "gm": 2, "admin": 3}
_ativos = {}  # nome -> (nivel, conferido_em): admin desativado ou rebaixado muda em ate 60 s


def admin_ativo(nome):
    """Nivel do admin (1..3), ou 0 se nao existe ou esta desativado."""
    nivel, quando = _ativos.get(nome, (0, 0))
    if time.time() - quando > 60:
        rows = db_exec("SELECT nivel FROM pwadmin_admins WHERE nome=%s AND ativo=1", (nome,))
        nivel = NIVEIS.get(rows[0]["nivel"], 0) if rows else 0
        _ativos[nome] = (nivel, time.time())
    return nivel


class SemPermissao(Exception):
    pass


def exigir(nivel):
    if getattr(CTX, "nivel", 0) < NIVEIS[nivel]:
        raise SemPermissao(f"sem permissão: precisa de nível {nivel}")


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


# Indice de personagens em memoria (conta -> personagens). Montagem completa ao iniciar e a cada
# INDICE_COMPLETO; no meio, a cada minuto relê so contas novas e contas que entraram ou sairam
# (criar, apagar e renomear personagem acontece com a conta logada). A busca por conta ou ID
# tambem relê a conta na hora.
INDICE_COMPLETO = 6 * 3600
INDICE_TICK = 60
BUSCA_MAX = 30
_indice = {}  # userid -> {"name": conta, "roles": [(roleid, nome)]}
_indice_lock = threading.Lock()


def _reler_conta(userid, nome):
    roles = p.get_user_roles(GAMEDB, userid)
    with _indice_lock:
        _indice[userid] = {"name": nome, "roles": roles}
    return roles


def _manter_indice():
    completo_em, maior_id, antes = 0, 0, set()
    ultima_limpeza = 0
    while True:
        try:
            if time.time() - completo_em > INDICE_COMPLETO:
                novo = {}
                for i, u in enumerate(db_query("SELECT id, name FROM users")):
                    novo[u["id"]] = {"name": u["name"], "roles": p.get_user_roles(GAMEDB, u["id"])}
                    if i % 200 == 199:
                        time.sleep(0.2)  # nao sobrecarregar o gamedbd
                with _indice_lock:
                    _indice.clear()
                    _indice.update(novo)
                maior_id, completo_em = max(novo, default=0), time.time()
            else:
                for u in db_query("SELECT id, name FROM users WHERE id > %s", (maior_id,)):
                    _reler_conta(u["id"], u["name"])
                    maior_id = max(maior_id, u["id"])
            agora = online_userids()
            for uid in agora ^ antes:
                _reler_conta(uid, nome_conta(uid))
            antes = agora
            if time.time() - ultima_limpeza > 86400:
                n = _limpar_backups()
                ultima_limpeza = time.time()
                if n:
                    audit("limpeza_backups", apagados=n, dias=BACKUP_DIAS)
        except Exception as e:  # noqa: BLE001 - tenta de novo no proximo ciclo
            print("indice:", e, file=sys.stderr, flush=True)
        time.sleep(INDICE_TICK)


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
    """So o total: o GM acha o jogador pela busca."""
    jogo = em_jogo()
    return {"total": len(jogo) if jogo is not None else len(online_userids())}


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

        backup = novo_backup(roleid, raw)
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
            atual = ler_curral(check)[0] if key == "pets.capacity" else check[section][field]
            if atual != new:
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

        backup = novo_backup(roleid, raw)
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

        backup = novo_backup(roleid, raw)
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
    try:
        return sorted((f for f in os.listdir(pasta_backup(roleid)) if _NOME_BACKUP.match(f)), reverse=True)
    except FileNotFoundError:
        return []


# Descricao dos backups: o motivo vem do audit.log (acao que gerou o backup) e o resumo vem do
# proprio arquivo (nivel, gold, itens). Arquivos nao mudam, entao o resumo fica em cache.
BACKUPS_LISTADOS = 50
ROTULOS = {"status.level": "nível", "status.exp": "XP", "status.sp": "SP", "status.pp": "pontos",
           "status.hp": "HP", "status.mp": "MP", "status.reputation": "reputação",
           "pocket.money": "gold da bolsa", "storehouse.money": "gold do banco",
           "pocket.capacity": "espaços da bolsa", "storehouse.capacity": "espaços do banco",
           "pets.capacity": "espaços de mascotes", "status.worldtag": "posição", "status.posx": "posição",
           "status.posy": "posição", "status.posz": "posição"}
_resumos = {}


def _data_backup(nome):
    """1024_20261030_030838.bin -> '30/10 03:08'."""
    try:
        d, h = _NOME_BACKUP.match(nome).groups()[1:3]
        return f"{d[6:8]}/{d[4:6]} {h[:2]}:{h[2:4]}"
    except (ValueError, IndexError, AttributeError):
        return nome


def _motivos_backup(roleid):
    """backup -> registro da auditoria que o criou (acao, admin e dados)."""
    mapa = {}
    for row in db_exec("SELECT backup, acao, admin, dados FROM pwadmin_auditoria WHERE roleid=%s "
                       "AND backup IS NOT NULL ORDER BY id DESC LIMIT %s", (roleid, BACKUPS_LISTADOS * 2)):
        try:
            e = json.loads(row["dados"])
        except ValueError:
            e = {}
        mapa.setdefault(row["backup"], e | {"action": row["acao"], "admin": row["admin"]})
    return mapa


def _motivo(e):
    if not e:
        return ""
    a = e.get("action")
    nome = lambda: ITENS.get(str(e.get("item_id")), {}).get("nome") or f"#{e.get('item_id')}"
    if a == "edit_role":
        campos = []
        for k in e.get("changes", {}):
            r = "atributos" if k.startswith("attr.") else ROTULOS.get(k, k)
            if r not in campos:
                campos.append(r)
        return "antes de editar " + ", ".join(campos[:4]) + ("…" if len(campos) > 4 else "")
    if a == "add_item":
        return f"antes de colocar {nome()} ×{e.get('count', 1)}"
    if a == "remove_item":
        return f"antes de tirar {nome()} ×{e.get('count', 1)}"
    if a == "restore_role":
        return f"antes de restaurar {_data_backup(e.get('source', ''))}"
    if a == "snapshot":
        return "manual" + (f": {e['nota']}" if e.get("nota") else "")
    return a or ""


def _resumo(nome):
    if nome not in _resumos:
        try:
            with open(arquivo_backup(nome.split("_")[0], nome), "rb") as f:
                role = p.decode(p.GRoleData, p.Reader(f.read()))
            pk, sh = role["pocket"], role["storehouse"]
            _resumos[nome] = {"nivel": role["status"]["level"], "gold": pk["money"] + sh["money"],
                              "itens": len(pk["items"]) + len(sh["items"]) + len(role["equipment"]["inv"])}
        except Exception:  # noqa: BLE001 - arquivo antigo ou corrompido: lista sem resumo
            _resumos[nome] = {}
    return _resumos[nome]


def backups_json(roleid):
    try:
        motivos = _motivos_backup(roleid)
    except Exception:  # noqa: BLE001 - sem banco: lista sem motivo
        motivos = {}
    out = []
    for f in list_backups(roleid)[:BACKUPS_LISTADOS]:
        e = motivos.get(f)
        out.append({"file": f, "quando": _data_backup(f), "motivo": _motivo(e),
                    "admin": (e or {}).get("admin", "")} | _resumo(f))
    return out


def snapshot_role(roleid, body):
    """Backup manual (nao altera o personagem). Com o jogador online, guarda o ultimo salvamento do jogo."""
    nota = str(body.get("nota", ""))[:60]
    _, raw = p.get_role(GAMEDB, roleid)
    backup = novo_backup(roleid, raw)
    audit("snapshot", roleid=roleid, nota=nota, backup=backup)
    return {"ok": True, "backup": backup}


def restore_role(roleid, filename):
    if filename not in list_backups(roleid):
        raise ValueError("backup nao encontrado")
    with write_lock:
        if p.get_role(GAMEDB, roleid)[0]["base"]["userid"] in online_userids():
            raise ValueError("personagem online: desconecte o jogador antes de restaurar")
        with open(arquivo_backup(roleid, filename), "rb") as f:
            raw = f.read()
        role = p.decode(p.GRoleData, p.Reader(raw))
        current_raw = p.get_role(GAMEDB, roleid)[1]
        safety = novo_backup(roleid, current_raw)
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


# ---------------------------------------------------------------- varredura
# Le todos os personagens de tempos em tempos, devagar, e monta em memoria: quem tem cada item
# (bolsa, banco, equipado, bancos extras, missao, curral), o gold de cada um e os bloqueios
# gravados no personagem. A primeira varredura do dia tambem guarda um backup de todos.
# Correio nao entra: o gamedbd guarda em outra tabela, que o painel ainda nao le.
VARREDURA_INTERVALO = 3600
VARREDURA_PAUSA = 0.02  # entre personagens, para nao pesar no gamedbd
DIARIO_DIAS = 7
_varredura = {"quando": 0, "duracao": 0, "itens": {}, "gold": {}, "gold_antes": {}, "forbid": {}, "nomes": {}}
_varrer_agora = threading.Event()


def _pasta_diario(dia):
    return os.path.join(CFG["backup_dir"], "diario", dia)


def _varrer():
    with _indice_lock:
        roles = [(rid, nome, uid) for uid, c in _indice.items() for rid, nome in c["roles"]]
    inicio, dia = time.time(), time.strftime("%Y%m%d")
    pasta = _pasta_diario(dia)
    diario = not os.path.isdir(pasta)
    tmp = pasta + ".parcial"
    if diario:
        os.makedirs(tmp, exist_ok=True)
    itens, gold, forbid, nomes = {}, {}, {}, {}
    for rid, nome, uid in roles:
        try:
            role, raw = p.get_role(GAMEDB, rid)
        except Exception:  # noqa: BLE001 - apagado no meio da varredura
            continue
        nomes[rid] = (nome, uid)
        for local, ler in LOCAIS.items():
            for it in ler(role):
                itens.setdefault(it["id"], []).append((rid, local, it["count"]))
        gold[rid] = (role["pocket"]["money"], role["storehouse"]["money"])
        f = [x for x in role["base"]["forbid"] if x["time"] > 0]
        if f:
            forbid[rid] = f
        if diario:
            with open(os.path.join(tmp, f"{rid}.bin"), "wb") as fh:
                fh.write(raw)
        time.sleep(VARREDURA_PAUSA)
    if diario:
        os.replace(tmp, pasta)
        base = os.path.dirname(pasta)
        for velho in sorted(n for n in os.listdir(base) if n.isdigit())[:-DIARIO_DIAS]:
            shutil.rmtree(os.path.join(base, velho), ignore_errors=True)
    _varredura.update(quando=time.time(), duracao=round(time.time() - inicio, 1), itens=itens,
                      gold_antes=_varredura["gold"], gold=gold, forbid=forbid, nomes=nomes)
    db_exec("INSERT IGNORE INTO pwadmin_economia (quando, personagens, bolsa, banco) VALUES (NOW(), %s, %s, %s)",
            (len(gold), sum(g[0] for g in gold.values()), sum(g[1] for g in gold.values())))


def _manter_varredura():
    time.sleep(90)  # deixa o indice montar primeiro
    while True:
        try:
            _varrer()
        except Exception as e:  # noqa: BLE001
            print("varredura:", e, file=sys.stderr, flush=True)
        _varrer_agora.wait(VARREDURA_INTERVALO)
        _varrer_agora.clear()


def _quem(rid):
    nome, uid = _varredura["nomes"].get(rid, ("?", 0))
    return {"roleid": rid, "nome": nome, "userid": uid, "conta": nome_conta(uid) if uid else ""}


def _status_varredura():
    return {"quando": int(_varredura["quando"]), "duracao": _varredura["duracao"],
            "personagens": len(_varredura["nomes"])}


def quem_tem(q):
    """Quem tem o item (ID exato, ou ate 20 itens pelo nome)."""
    q = q.strip().lstrip("#")
    if q.isdigit():
        tids = [int(q)]
    else:
        ql = q.lower()
        tids = [int(k) for k, v in ITENS.items() if ql and ql in v.get("nome", "").lower()][:20] if len(ql) >= 2 else []
    out = []
    for tid in tids:
        donos = {}
        for rid, local, n in _varredura["itens"].get(tid, []):
            d = donos.setdefault(rid, {"total": 0, "locais": {}})
            d["total"] += n
            d["locais"][local] = d["locais"].get(local, 0) + n
        lista = sorted(donos.items(), key=lambda x: -x[1]["total"])
        out.append({"id": tid, "nome": ITENS.get(str(tid), {}).get("nome", ""), "total": sum(d["total"] for d in donos.values()),
                    "donos": [_quem(rid) | d for rid, d in lista[:200]], "mais": max(0, len(lista) - 200)})
    out.sort(key=lambda x: -x["total"])
    return {"varredura": _status_varredura(), "itens": out}


def economia():
    gold = _varredura["gold"]
    antes = _varredura["gold_antes"]
    ricos = sorted(gold.items(), key=lambda x: -sum(x[1]))[:20]
    saltos = sorted(((rid, sum(g) - sum(antes.get(rid, (0, 0)))) for rid, g in gold.items() if rid in antes),
                    key=lambda x: -x[1])[:10]
    hist = db_exec("SELECT quando, personagens, bolsa, banco FROM pwadmin_economia ORDER BY quando DESC LIMIT 48")
    return {"varredura": _status_varredura(),
            "total": {"bolsa": sum(g[0] for g in gold.values()), "banco": sum(g[1] for g in gold.values())},
            "ricos": [_quem(rid) | {"bolsa": g[0], "banco": g[1]} for rid, g in ricos],
            "saltos": [_quem(rid) | {"ganho": d} for rid, d in saltos if d > 0],
            "historico": [{"quando": str(h["quando"]), "personagens": h["personagens"], "total": h["bolsa"] + h["banco"]}
                          for h in hist]}


# ---------------------------------------------------------------- punicoes e auditoria

FORBID_TIPOS = {100: "ban", 101: "mute", 102: "troca", 103: "venda", 105: "ban"}


def punicoes():
    """Bans e mutes ainda valendo: os do painel (auditoria, com quem e motivo) e os gravados no
    personagem pelo jogo (varredura)."""
    agora = time.time()
    rows = db_exec("SELECT a.id, a.quando, a.admin, a.acao, a.roleid, a.dados FROM pwadmin_auditoria a "
                   "JOIN (SELECT roleid, MAX(id) id FROM pwadmin_auditoria WHERE acao IN ('ban','unban','mute','unmute') "
                   "AND quando > NOW() - INTERVAL 366 DAY GROUP BY roleid, IF(acao IN ('ban','unban'), 'b', 'm')) u ON u.id = a.id")
    out, vistos = [], set()
    for r in rows:
        if r["acao"] not in ("ban", "mute"):
            continue
        d = json.loads(r["dados"] or "{}")
        ate = r["quando"].timestamp() + int(d.get("minutes", 0)) * 60
        if ate > agora:
            vistos.add((r["roleid"], r["acao"]))
            out.append(_quem(r["roleid"]) | {"tipo": r["acao"], "desde": int(r["quando"].timestamp()), "ate": int(ate),
                                             "por": r["admin"], "motivo": d.get("reason", "")})
    for rid, fs in _varredura["forbid"].items():
        for f in fs:
            tipo = FORBID_TIPOS.get(f["type"], str(f["type"]))
            ate = f["createtime"] + f["time"]
            if ate > agora and (rid, tipo) not in vistos:
                out.append(_quem(rid) | {"tipo": tipo, "desde": f["createtime"], "ate": ate, "por": "jogo",
                                         "motivo": f["reason"].decode("utf-16-le", "replace") if isinstance(f["reason"], bytes) else ""})
    return sorted(out, key=lambda x: x["ate"])


AUDITORIA_POR_PAGINA = 50


def auditoria(q):
    onde, args = [], []
    for campo, col in (("admin", "admin"), ("acao", "acao")):
        if q.get(campo, [""])[0]:
            onde.append(f"{col} = %s")
            args.append(q[campo][0][:32])
    if q.get("roleid", [""])[0].isdigit():
        onde.append("roleid = %s")
        args.append(int(q["roleid"][0]))
    for campo, op in (("de", ">="), ("ate", "<")):
        v = q.get(campo, [""])[0]
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
            onde.append(f"quando {op} %s" + (" + INTERVAL 1 DAY" if campo == "ate" else ""))
            args.append(v)
    pagina = max(0, int(q.get("pagina", ["0"])[0] or 0))
    where = ("WHERE " + " AND ".join(onde)) if onde else ""
    rows = db_exec(f"SELECT id, quando, admin, ip, acao, roleid, userid, backup, dados FROM pwadmin_auditoria {where} "
                   f"ORDER BY id DESC LIMIT %s OFFSET %s", (*args, AUDITORIA_POR_PAGINA + 1, pagina * AUDITORIA_POR_PAGINA))
    return {"linhas": [r | {"quando": str(r["quando"])} for r in rows[:AUDITORIA_POR_PAGINA]],
            "mais": len(rows) > AUDITORIA_POR_PAGINA,
            "admins": [r["admin"] for r in db_exec("SELECT DISTINCT admin FROM pwadmin_auditoria ORDER BY admin")],
            "acoes": [r["acao"] for r in db_exec("SELECT DISTINCT acao FROM pwadmin_auditoria ORDER BY acao")]}


# ---------------------------------------------------------------- contas (authd)
# Senha do jogo: md5(login + senha), em binario, como o gauthd confere.

def _senha_jogo(login, senha):
    if not 6 <= len(senha) <= 20 or not senha.isprintable() or " " in senha:
        raise ValueError("senha: 6 a 20 caracteres, sem espaço")
    return hashlib.md5((login + senha).encode()).digest()


def db_auth(sql, args=()):
    conn = pymysql.connect(**CFG["db"], charset="latin1", cursorclass=pymysql.cursors.DictCursor, autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute(sql, args)
            return cur.fetchall()
    finally:
        conn.close()


def contas(q):
    q = q.strip()
    if q.isdigit():
        rows = db_query("SELECT id, name, creatime FROM users WHERE id=%s", (int(q),))
    elif q:
        like = "%" + q.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_") + "%"
        rows = db_query("SELECT id, name, creatime FROM users WHERE name LIKE %s ORDER BY LENGTH(name), name LIMIT 20", (like,))
    else:
        rows = db_query("SELECT id, name, creatime FROM users ORDER BY id DESC LIMIT 20")
    online, gms = online_userids(), gm_userids()
    return [{"id": r["id"], "nome": r["name"], "criada": str(r["creatime"] or ""), "online": r["id"] in online,
             "gm": r["id"] in gms, "personagens": [{"id": rid, "nome": n} for rid, n in _reler_conta(r["id"], r["name"])]}
            for r in rows]


def criar_conta(body):
    login = str(body.get("login", "")).strip().lower()
    if not re.fullmatch(r"[a-z0-9]{4,20}", login):
        raise ValueError("login: 4 a 20 letras minúsculas ou números")
    if db_query("SELECT id FROM users WHERE name=%s", (login,)):
        raise ValueError("login já existe")
    h = _senha_jogo(login, str(body.get("senha", "")))
    db_auth("CALL adduser(%s, %s, '', '', '', '', '', '', '', '', '', '', '', 0, '', '', %s)", (login, h, h))
    uid = db_query("SELECT id FROM users WHERE name=%s", (login,))[0]["id"]
    audit("conta_criada", userid=uid, login=login)
    return {"ok": True, "id": uid}


def senha_conta(userid, body):
    rows = db_query("SELECT name FROM users WHERE id=%s", (userid,))
    if not rows:
        raise ValueError("conta não existe")
    login = rows[0]["name"]
    db_auth("CALL changePasswd(%s, %s)", (login, _senha_jogo(login, str(body.get("senha", "")))))
    audit("conta_senha", userid=userid, login=login)
    return {"ok": True}


# ---------------------------------------------------------------- servidor

# nome mostrado -> nome do processo (/proc/PID/comm); o authd daqui e java
PROCESSOS = {"authd": "java", "gamedbd": "gamedbd", "uniquenamed": "uniquenamed", "gdeliveryd": "gdeliveryd",
             "glinkd": "glinkd", "gfactiond": "gfactiond", "logservice": "logservice", "gacd": "gacd", "gs": "gs"}
_reinicio = {"ate": 0, "por": "", "cancelado": threading.Event()}


def _contar_processos():
    comms = []
    for pid in filter(str.isdigit, os.listdir("/proc")):
        try:
            with open(f"/proc/{pid}/comm") as f:
                comms.append(f.read().strip())
        except OSError:
            continue
    return {n: comms.count(c) for n, c in PROCESSOS.items()}


def servidor():
    mem = {}
    with open("/proc/meminfo") as f:
        for linha in f:
            k, v = linha.split(":")
            mem[k] = int(v.split()[0]) * 1024
    disco = shutil.disk_usage("/")
    jogo = em_jogo()
    return {"processos": _contar_processos(), "carga": os.getloadavg(),
            "memoria": {"total": mem["MemTotal"], "livre": mem["MemAvailable"]},
            "disco": {"total": disco.total, "livre": disco.free},
            "online": len(jogo) if jogo is not None else len(online_userids()),
            "reinicio": {"ate": int(_reinicio["ate"]), "por": _reinicio["por"]} if _reinicio["ate"] else None}


def _reiniciar(minutos, admin):
    cancelado = _reinicio["cancelado"]
    for falta in range(minutos, 0, -1):
        if falta in (minutos, 10, 5, 3, 2, 1):
            p.broadcast(DELIVERY, f"O servidor vai reiniciar em {falta} minuto{'s' if falta > 1 else ''}. Fique em local seguro.")
        if cancelado.wait(60):
            p.broadcast(DELIVERY, "Reinício do servidor cancelado.")
            return
    r = subprocess.run(["sudo", "-n", "systemctl", "restart", "pwserver"], capture_output=True, text=True, timeout=300)
    audit("servidor_reiniciado", admin=admin, ip="", retcode=r.returncode, erro=r.stderr[-300:])
    _reinicio["ate"] = 0


def reiniciar_servidor(body):
    minutos = int(body.get("minutos", 5))
    if not 1 <= minutos <= 30:
        raise ValueError("minutos: 1 a 30")
    if _reinicio["ate"]:
        raise ValueError("já tem um reinício marcado")
    _reinicio.update(ate=time.time() + minutos * 60, por=CTX.admin, cancelado=threading.Event())
    threading.Thread(target=_reiniciar, args=(minutos, CTX.admin), daemon=True).start()
    audit("servidor_reinicio", minutos=minutos)
    return {"ok": True}


def cancelar_reinicio():
    if not _reinicio["ate"]:
        raise ValueError("nenhum reinício marcado")
    _reinicio["cancelado"].set()
    _reinicio["ate"] = 0
    audit("servidor_reinicio_cancelado")
    return {"ok": True}


# ---------------------------------------------------------------- admins do painel

def lista_admins():
    return [r | {"criado": str(r["criado"]), "ultimo_login": str(r["ultimo_login"] or "")}
            for r in db_exec("SELECT nome, nivel, ativo, criado, ultimo_login FROM pwadmin_admins ORDER BY nome")]


def mudar_admin(body):
    """acao: criar (devolve a senha uma vez), nivel, senha (nova senha), desativar, ativar."""
    nome, acao = str(body.get("nome", "")).strip(), body.get("acao")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{2,32}", nome):
        raise ValueError("nome: 2 a 32 letras, números, _ . -")
    nivel = body.get("nivel", "gm")
    if nivel not in NIVEIS:
        raise ValueError("nível inválido")
    existe = bool(db_exec("SELECT id FROM pwadmin_admins WHERE nome=%s", (nome,)))
    if acao != "criar" and not existe:
        raise ValueError("admin não existe")
    if nome == CTX.admin and acao in ("nivel", "desativar"):
        raise ValueError("não dá para mudar o próprio nível ou se desativar")
    out = {"ok": True}
    if acao == "criar":
        if existe:
            raise ValueError("já existe")
        out["senha"] = secrets.token_urlsafe(12)
        db_exec("INSERT INTO pwadmin_admins (nome, senha, nivel) VALUES (%s, %s, %s)", (nome, hash_senha(out["senha"]), nivel))
    elif acao == "senha":
        out["senha"] = secrets.token_urlsafe(12)
        db_exec("UPDATE pwadmin_admins SET senha=%s WHERE nome=%s", (hash_senha(out["senha"]), nome))
    elif acao == "nivel":
        db_exec("UPDATE pwadmin_admins SET nivel=%s WHERE nome=%s", (nivel, nome))
    elif acao in ("desativar", "ativar"):
        db_exec("UPDATE pwadmin_admins SET ativo=%s WHERE nome=%s", (int(acao == "ativar"), nome))
    else:
        raise ValueError("ação inválida")
    _ativos.pop(nome, None)
    if acao in ("senha", "desativar"):  # derruba as sessoes abertas dele
        for t, (_, n) in list(SESSIONS.items()):
            if n == nome:
                SESSIONS.pop(t, None)
    audit("admin_" + acao, alvo=nome, nivel=nivel if acao in ("criar", "nivel") else None)
    return out


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
        """Nome do admin logado (e guarda admin e IP para a auditoria), ou None."""
        for part in self.headers.get("Cookie", "").split(";"):
            k, _, v = part.strip().partition("=")
            expira, nome = SESSIONS.get(v, (0, ""))
            nivel = admin_ativo(nome) if k == "pwadmin" and expira > time.time() else 0
            if nivel:
                CTX.admin, CTX.ip, CTX.token, CTX.nivel = nome, self.client_address[0], v, nivel
                return nome
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
            if path == "/api/eu":
                return self._send(200, {"admin": CTX.admin, "nivel": CTX.nivel})
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if path == "/api/quemtem":
                return self._send(200, quem_tem(q.get("q", [""])[0][:40]))
            if path == "/api/economia":
                return self._send(200, economia())
            if path == "/api/punicoes":
                return self._send(200, punicoes())
            if path == "/api/servidor":
                return self._send(200, servidor())
            if path == "/api/auditoria":
                exigir("gm")
                return self._send(200, auditoria(q))
            if path == "/api/contas":
                exigir("admin")
                return self._send(200, contas(q.get("q", [""])[0][:32]))
            if path == "/api/admins":
                exigir("admin")
                return self._send(200, lista_admins())
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
            if len(parts) == 4 and parts[:2] == ["api", "role"] and parts[3] == "backup":
                # Ver um backup sem restaurar: mesmo formato do personagem, so leitura
                rid = int(parts[2])
                nome = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("file", [""])[0]
                if nome not in list_backups(rid):
                    return self._send(404, {"error": "backup nao encontrado"})
                with open(arquivo_backup(rid, nome), "rb") as f:
                    role = p.decode(p.GRoleData, p.Reader(f.read()))
                return self._send(200, role_json(role) | {"backups": backups_json(rid), "backup": nome,
                                                          "account": nome_conta(role["base"]["userid"])})
            if len(parts) == 3 and parts[:2] == ["api", "role"]:
                role, raw = p.get_role(GAMEDB, int(parts[2]))
                return self._send(200, role_json(role) | {"backups": backups_json(int(parts[2])),
                                                          "versao": hashlib.sha1(raw).hexdigest()[:16],
                                                          "account": nome_conta(role["base"]["userid"])})
            return self._send(404, {"error": "nao encontrado"})
        except SemPermissao as e:
            return self._send(403, {"error": str(e)})
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
                nome = str(body.get("usuario", "")).strip()[:32] or "admin"
                if not login_admin(nome, str(body.get("password", ""))):
                    time.sleep(1)
                    audit("login_fail", admin=nome, ip=self.client_address[0])
                    return self._send(403, {"error": "usuário ou senha incorretos"})
                token = secrets.token_urlsafe(32)
                SESSIONS[token] = (time.time() + SESSION_TTL, nome)
                audit("login", admin=nome, ip=self.client_address[0])
                return self._send(200, {"ok": True}, headers={
                    "Set-Cookie": f"pwadmin={token}; HttpOnly; SameSite=Strict; Path=/"})
            if not self._session():
                return self._send(401, {"error": "login necessario"})
            if path == "/api/logout":
                SESSIONS.pop(CTX.token, None)
                audit("logout")
                return self._send(200, {"ok": True}, headers={
                    "Set-Cookie": "pwadmin=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0"})
            parts = path.strip("/").split("/")
            if len(parts) == 4 and parts[:2] == ["api", "role"]:
                exigir("admin" if parts[3] == "restore" else "gm")
            if path == "/api/broadcast" or path == "/api/varrer":
                exigir("gm")
            if path in ("/api/cash", "/api/admins", "/api/contas", "/api/servidor/reiniciar", "/api/servidor/cancelar") \
                    or parts[:2] == ["api", "contas"]:
                exigir("admin")
            if path == "/api/varrer":
                _varrer_agora.set()
                return self._send(200, {"ok": True})
            if path == "/api/admins":
                return self._send(200, mudar_admin(body))
            if path == "/api/contas":
                return self._send(200, criar_conta(body))
            if len(parts) == 4 and parts[:2] == ["api", "contas"] and parts[3] == "senha":
                return self._send(200, senha_conta(int(parts[2]), body))
            if path == "/api/servidor/reiniciar":
                return self._send(200, reiniciar_servidor(body))
            if path == "/api/servidor/cancelar":
                return self._send(200, cancelar_reinicio())
            if len(parts) == 4 and parts[:2] == ["api", "role"] and parts[3] == "edit":
                return self._send(200, edit_role(int(parts[2]), body.get("changes", {})))
            if len(parts) == 4 and parts[:2] == ["api", "role"] and parts[3] == "additem":
                return self._send(200, add_item(int(parts[2]), body))
            if len(parts) == 4 and parts[:2] == ["api", "role"] and parts[3] == "removeitem":
                return self._send(200, remove_item(int(parts[2]), body))
            if len(parts) == 4 and parts[:2] == ["api", "role"] and parts[3] == "snapshot":
                return self._send(200, snapshot_role(int(parts[2]), body))
            if len(parts) == 4 and parts[:2] == ["api", "role"] and parts[3] == "restore":
                return self._send(200, restore_role(int(parts[2]), str(body.get("backup", ""))))
            if len(parts) == 4 and parts[:2] == ["api", "role"] and parts[3] in LIVE_ROLE_ACTIONS:
                return self._send(200, LIVE_ROLE_ACTIONS[parts[3]](int(parts[2]), body))
            if path == "/api/broadcast":
                return self._send(200, live_broadcast(body))
            if path == "/api/cash":
                return self._send(200, add_cash(body))
            return self._send(404, {"error": "nao encontrado"})
        except SemPermissao as e:
            return self._send(403, {"error": str(e)})
        except (ValueError, p.RpcError) as e:
            return self._send(400, {"error": str(e)})
        except Exception as e:  # noqa: BLE001
            return self._send(500, {"error": str(e)})


def comando_admin(args):
    """python3 -I server.py admin NOME [suporte|gm|admin]  cria o admin (ou troca a senha) e mostra a senha nova
       python3 -I server.py desativar NOME  bloqueia o acesso (sai em ate 60 s)
       python3 -I server.py admins          lista os admins"""
    preparar_banco()
    if args[0] == "admins":
        for a in db_exec("SELECT nome, nivel, ativo, criado, ultimo_login FROM pwadmin_admins ORDER BY nome"):
            print(f"{a['nome']:<20} {a['nivel']:<8} {'ativo' if a['ativo'] else 'DESATIVADO':<11} criado {a['criado']}  "
                  f"último login {a['ultimo_login'] or '-'}")
        return
    nome = args[1].strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]{2,32}", nome):
        raise SystemExit("nome: 2 a 32 letras, numeros, _ . -")
    if args[0] == "admin":
        nivel = args[2] if len(args) > 2 else "admin"
        if nivel not in NIVEIS:
            raise SystemExit("nível: suporte, gm ou admin")
        senha = secrets.token_urlsafe(12)
        db_exec("INSERT INTO pwadmin_admins (nome, senha, nivel) VALUES (%s, %s, %s) "
                "ON DUPLICATE KEY UPDATE senha=VALUES(senha), nivel=VALUES(nivel), ativo=1",
                (nome, hash_senha(senha), nivel))
        audit("admin_senha", admin="console", alvo=nome, nivel=nivel)
        print(f"Admin {nome} ({nivel}) · senha: {senha}")
    elif args[0] == "desativar":
        db_exec("UPDATE pwadmin_admins SET ativo=0 WHERE nome=%s", (nome,))
        audit("admin_desativado", admin="console", alvo=nome)
        print(f"Admin {nome} desativado.")


def main():
    if len(sys.argv) > 1:
        return comando_admin(sys.argv[1:])
    preparar_banco()
    threading.Thread(target=_manter_indice, daemon=True).start()
    threading.Thread(target=_manter_varredura, daemon=True).start()
    srv = http.server.ThreadingHTTPServer((CFG["bind"], CFG["port"]), Handler)
    print(f"PW Admin em http://{CFG['bind']}:{CFG['port']}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
