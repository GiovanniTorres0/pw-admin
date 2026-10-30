"""Protocolo GNET do Perfect World 1.5.5 (v156): codificacao, estruturas e cliente RPC.

Formato na rede: CompactUINT(type) + Octets(payload).
Octets = CompactUINT(tamanho) + bytes. Vetor = CompactUINT(quantidade) + itens.
Inteiros e floats em big-endian. RPC: xid (uint32, bit 31 = pedido) + argumento/resultado.
Estruturas copiadas do rpcalls.xml do fonte (cnet_20150831), conferidas com o exportrole da v156.
"""
import socket
import struct
import threading

# ---------------------------------------------------------------- codificacao

def cuint_bytes(n):
    if n < 0x80:
        return struct.pack(">B", n)
    if n < 0x4000:
        return struct.pack(">H", n | 0x8000)
    if n < 0x20000000:
        return struct.pack(">I", n | 0xC0000000)
    return b"\xe0" + struct.pack(">I", n)


class Reader:
    def __init__(self, data, pos=0):
        self.data = data
        self.pos = pos

    def _take(self, fmt):
        v = struct.unpack_from(fmt, self.data, self.pos)[0]
        self.pos += struct.calcsize(fmt)
        return v

    def cuint(self):
        b = self.data[self.pos]
        if b < 0x80:
            self.pos += 1
            return b
        if b < 0xC0:
            return self._take(">H") & 0x3FFF
        if b < 0xE0:
            return self._take(">I") & 0x1FFFFFFF
        self.pos += 1
        return self._take(">I")

    def octets(self):
        n = self.cuint()
        if self.pos + n > len(self.data):
            raise ValueError("octets passa do fim do buffer")
        v = self.data[self.pos:self.pos + n]
        self.pos += n
        return v

    def remaining(self):
        return len(self.data) - self.pos


class Writer:
    def __init__(self):
        self.parts = []

    def put(self, fmt, v):
        self.parts.append(struct.pack(fmt, v))

    def cuint(self, n):
        self.parts.append(cuint_bytes(n))

    def octets(self, b):
        self.cuint(len(b))
        self.parts.append(bytes(b))

    def bytes(self):
        return b"".join(self.parts)


# ---------------------------------------------------------------- estruturas
# Tipos: B/b = (unsigned) char, H/h = (unsigned) short, I/i = (unsigned) int,
# f = float, oct = Octets, ("vec", schema), ("struct", schema).

PRIMS = {"B": ">B", "b": ">b", "H": ">H", "h": ">h", "I": ">I", "i": ">i", "f": ">f"}

GRoleForbid = [("type", "B"), ("time", "i"), ("createtime", "i"), ("reason", "oct")]

GRoleInventory = [
    ("id", "I"), ("pos", "i"), ("count", "i"), ("max_count", "i"), ("data", "oct"),
    ("proctype", "i"), ("expire_date", "i"), ("guid1", "i"), ("guid2", "i"), ("mask", "i"),
]
INV_VEC = ("vec", GRoleInventory)

GRoleBase = [
    ("version", "b"), ("id", "I"), ("name", "oct"), ("race", "i"), ("cls", "i"), ("gender", "B"),
    ("custom_data", "oct"), ("config_data", "oct"), ("custom_stamp", "I"), ("status", "B"),
    ("delete_time", "i"), ("create_time", "i"), ("lastlogin_time", "i"),
    ("forbid", ("vec", GRoleForbid)), ("help_states", "oct"), ("spouse", "I"), ("userid", "I"),
    ("cross_data", "oct"), ("reserved2", "B"), ("reserved3", "B"), ("reserved4", "B"),
]

GRoleStatus = [
    ("version", "b"), ("level", "i"), ("level2", "i"), ("exp", "i"), ("sp", "i"), ("pp", "i"),
    ("hp", "i"), ("mp", "i"), ("posx", "f"), ("posy", "f"), ("posz", "f"), ("worldtag", "i"),
    ("invader_state", "i"), ("invader_time", "i"), ("pariah_time", "i"), ("reputation", "i"),
    ("custom_status", "oct"), ("filter_data", "oct"), ("charactermode", "oct"),
    ("instancekeylist", "oct"), ("dbltime_expire", "i"), ("dbltime_mode", "i"),
    ("dbltime_begin", "i"), ("dbltime_used", "i"), ("dbltime_max", "i"), ("time_used", "i"),
    ("dbltime_data", "oct"), ("storesize", "H"), ("petcorral", "oct"), ("property", "oct"),
    ("var_data", "oct"), ("skills", "oct"), ("storehousepasswd", "oct"), ("waypointlist", "oct"),
    ("coolingtime", "oct"), ("npc_relation", "oct"), ("multi_exp_ctrl", "oct"),
    ("storage_task", "oct"), ("faction_contrib", "oct"), ("force_data", "oct"),
    ("online_award", "oct"), ("profit_time_data", "oct"), ("country_data", "oct"),
    ("king_data", "oct"), ("meridian_data", "oct"), ("extraprop", "oct"), ("title_data", "oct"),
    ("reincarnation_data", "oct"), ("realm_data", "oct"), ("reserved2", "b"), ("reserved3", "b"),
]

GRolePocket = [
    ("capacity", "I"), ("timestamp", "I"), ("money", "I"), ("items", INV_VEC),
    ("reserved1", "i"), ("reserved2", "i"),
]

GRoleEquipment = [("inv", INV_VEC)]

GRoleStorehouse = [
    ("capacity", "I"), ("money", "I"), ("items", INV_VEC), ("size1", "B"), ("size2", "B"),
    ("dress", INV_VEC), ("material", INV_VEC), ("size3", "B"), ("generalcard", INV_VEC),
    ("reserved", "h"),
]

GRoleTask = [
    ("task_data", "oct"), ("task_complete", "oct"), ("task_finishtime", "oct"),
    ("task_inventory", INV_VEC),
]

GRoleData = [
    ("base", ("struct", GRoleBase)), ("status", ("struct", GRoleStatus)),
    ("pocket", ("struct", GRolePocket)), ("equipment", ("struct", GRoleEquipment)),
    ("storehouse", ("struct", GRoleStorehouse)), ("task", ("struct", GRoleTask)),
]


def decode(schema, r):
    out = {}
    for name, typ in schema:
        out[name] = _decode_value(typ, r)
    return out


def _decode_value(typ, r):
    if typ == "oct":
        return r.octets()
    if isinstance(typ, tuple):
        kind, sub = typ
        if kind == "struct":
            return decode(sub, r)
        return [decode(sub, r) for _ in range(r.cuint())]
    return r._take(PRIMS[typ])


def encode(schema, value, w):
    for name, typ in schema:
        _encode_value(typ, value[name], w)
    return w


def _encode_value(typ, v, w):
    if typ == "oct":
        w.octets(v)
    elif isinstance(typ, tuple):
        kind, sub = typ
        if kind == "struct":
            encode(sub, v, w)
        else:
            w.cuint(len(v))
            for item in v:
                encode(sub, item, w)
    else:
        w.put(PRIMS[typ], v)


# ---------------------------------------------------------------- cliente RPC

RPC_GETROLEDATA = 8003
RPC_PUTROLEDATA = 8002
RPC_GETUSERROLES = 3401


class RpcError(Exception):
    pass


class RpcClient:
    """Uma conexao por chamada: simples e sem estado compartilhado com o servidor."""

    _xid = 1
    _lock = threading.Lock()

    def __init__(self, host, port, timeout=15):
        self.host, self.port, self.timeout = host, port, timeout

    @classmethod
    def _next_xid(cls):
        with cls._lock:
            cls._xid = (cls._xid + 1) & 0x7FFFFFFF
            return cls._xid

    def call(self, ptype, arg_bytes):
        xid = self._next_xid()
        payload = struct.pack(">I", 0x80000000 | xid) + arg_bytes
        packet = cuint_bytes(ptype) + cuint_bytes(len(payload)) + payload
        with socket.create_connection((self.host, self.port), timeout=self.timeout) as s:
            s.sendall(packet)
            buf = b""
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    raise RpcError(f"conexao fechada sem resposta (type={ptype})")
                buf += chunk
                while True:
                    try:
                        r = Reader(buf)
                        rtype = r.cuint()
                        size = r.cuint()
                    except IndexError:
                        break
                    if r.remaining() < size:
                        break
                    body = buf[r.pos:r.pos + size]
                    buf = buf[r.pos + size:]
                    if rtype == ptype and struct.unpack_from(">I", body)[0] == xid:
                        return body[4:]


# ---------------------------------------------------------------- operacoes do gamedbd

def get_role_raw(db, roleid):
    """Devolve (retcode, bytes do GRoleData)."""
    res = db.call(RPC_GETROLEDATA, struct.pack(">i", roleid))
    return struct.unpack_from(">i", res)[0], res[4:]


def get_role(db, roleid):
    retcode, raw = get_role_raw(db, roleid)
    if retcode != 0:
        raise RpcError(f"GetRoleData({roleid}) retcode={retcode}")
    r = Reader(raw)
    role = decode(GRoleData, r)
    if r.remaining():
        raise RpcError(f"GetRoleData({roleid}): sobraram {r.remaining()} bytes")
    return role, raw


def put_role(db, roleid, role, overwrite=1):
    w = Writer()
    w.put(">i", roleid)
    w.put(">b", overwrite)
    encode(GRoleData, role, w)
    res = db.call(RPC_PUTROLEDATA, w.bytes())
    return struct.unpack_from(">i", res)[0]


def get_user_roles(db, userid):
    """Lista [(roleid, nome)] de uma conta."""
    res = db.call(RPC_GETUSERROLES, struct.pack(">i", userid))
    r = Reader(res)
    retcode = r._take(">i")
    if retcode != 0:
        return []
    return [(r._take(">i"), r.octets().decode("utf-16-le", "replace")) for _ in range(r.cuint())]


def role_name(role):
    return role["base"]["name"].decode("utf-16-le", "replace")


# ---------------------------------------------------------------- gdeliveryd (acoes ao vivo)
# A porta 29100 e a dos glinkd: o gdeliveryd trata a conexao como mais um linkserver e executa os
# protocolos de GM sem checar permissao (quem checa e o glinkd). Nunca abrir essa porta para fora.

PublicChat = [("channel", "B"), ("emotion", "B"), ("roleid", "i"), ("localsid", "I"),
              ("msg", "oct"), ("data", "oct")]
GMKickoutRole = [("gmroleid", "i"), ("localsid", "i"), ("kickroleid", "i"), ("forbid_time", "i"),
                 ("reason", "oct")]
GMShutupRole = [("gmroleid", "i"), ("localsid", "I"), ("dstroleid", "i"), ("forbid_time", "i"),
                ("reason", "oct")]
SysSendMail = [("tid", "I"), ("sysid", "i"), ("sys_type", "B"), ("receiver", "i"), ("title", "oct"),
               ("context", "oct"), ("attach_obj", ("struct", GRoleInventory)), ("attach_money", "I")]

PROTO_PUBLICCHAT = 79
PROTO_GMKICKOUTROLE = 360
PROTO_GMKICKOUTROLE_RE = 361
PROTO_GMSHUTUPROLE = 362
PROTO_SYSSENDMAIL = 4214
PROTO_SYSSENDMAIL_RE = 4215

CHAT_CHANNEL_BROADCAST = 9  # GN_CHAT_CHANNEL_BROADCAST: o gdeliveryd repassa a todos os linkservers


class ProtoClient:
    """Protocolos sem xid: CompactUINT(type) + Octets(corpo). Uma conexao por envio."""

    def __init__(self, host, port, timeout=5):
        self.host, self.port, self.timeout = host, port, timeout

    def send(self, ptype, body, wait_type=None, match=None):
        """Envia e, se wait_type for dado, devolve o corpo da primeira resposta desse tipo
        (que passe em match). Sem wait_type, espera um pouco antes de fechar para o servidor
        processar o pacote antes de ver a conexao cair."""
        packet = cuint_bytes(ptype) + cuint_bytes(len(body)) + body
        with socket.create_connection((self.host, self.port), timeout=self.timeout) as s:
            s.sendall(packet)
            if wait_type is None:
                s.settimeout(0.5)
                try:
                    while s.recv(65536):
                        pass
                except socket.timeout:
                    pass
                return None
            buf = b""
            while True:
                try:
                    chunk = s.recv(65536)
                except socket.timeout:
                    raise RpcError(f"sem resposta do gdeliveryd (type={ptype})") from None
                if not chunk:
                    raise RpcError(f"conexao fechada sem resposta (type={ptype})")
                buf += chunk
                while True:
                    try:
                        r = Reader(buf)
                        rtype = r.cuint()
                        size = r.cuint()
                    except IndexError:
                        break
                    if r.remaining() < size:
                        break
                    rbody = buf[r.pos:r.pos + size]
                    buf = buf[r.pos + size:]
                    if rtype == wait_type and (match is None or match(rbody)):
                        return rbody


def _ucs2(text):
    return text.encode("utf-16-le")


def broadcast(dl, text):
    msg = {"channel": CHAT_CHANNEL_BROADCAST, "emotion": 0, "roleid": 0, "localsid": 0,
           "msg": _ucs2(text), "data": b""}
    dl.send(PROTO_PUBLICCHAT, encode(PublicChat, msg, Writer()).bytes())


def kickout_role(dl, roleid, forbid_time, reason=""):
    """forbid_time > 0: derruba e bloqueia o login por forbid_time segundos.
    forbid_time = 0: so remove o bloqueio (nao derruba)."""
    msg = {"gmroleid": 0, "localsid": 0, "kickroleid": roleid, "forbid_time": forbid_time,
           "reason": _ucs2(reason)}
    res = dl.send(PROTO_GMKICKOUTROLE, encode(GMKickoutRole, msg, Writer()).bytes(),
                  wait_type=PROTO_GMKICKOUTROLE_RE,
                  match=lambda b: struct.unpack_from(">i", b, 12)[0] == roleid)
    return struct.unpack_from(">i", res)[0]


def shutup_role(dl, roleid, forbid_time, reason=""):
    """Mute por forbid_time segundos (0 desfaz). O gdeliveryd nao responde a este protocolo."""
    msg = {"gmroleid": 0, "localsid": 0, "dstroleid": roleid, "forbid_time": forbid_time,
           "reason": _ucs2(reason)}
    dl.send(PROTO_GMSHUTUPROLE, encode(GMShutupRole, msg, Writer()).bytes())


_mail_tid = 0
_mail_lock = threading.Lock()


def send_mail(dl, roleid, title, text, money=0, item_id=0, count=0, max_count=0, proctype=0, objeto=None):
    """Correio do sistema (remetente "GM"). Devolve o retcode do SysSendMail_Re (0 = enviado).
    max_count e proctype vem do elements.data (pile_num_max e proc_type); 0 usa count e 0.
    objeto: item ja montado (GRoleInventory sem pos), usado no lugar do item sem corpo."""
    global _mail_tid
    with _mail_lock:
        _mail_tid = (_mail_tid + 1) & 0x7FFFFFFF
        tid = _mail_tid
    item = {"id": item_id, "pos": -1, "count": count, "max_count": max_count or count, "data": b"",
            "proctype": proctype, "expire_date": 0, "guid1": 0, "guid2": 0, "mask": 0}
    if objeto is not None:
        item = objeto | {"pos": -1}
    msg = {"tid": tid, "sysid": 0, "sys_type": 0, "receiver": roleid, "title": _ucs2(title),
           "context": _ucs2(text), "attach_obj": item, "attach_money": money}
    res = dl.send(PROTO_SYSSENDMAIL, encode(SysSendMail, msg, Writer()).bytes(),
                  wait_type=PROTO_SYSSENDMAIL_RE,
                  match=lambda b: struct.unpack_from(">I", b, 2)[0] == tid)
    return struct.unpack_from(">H", res)[0]


def online_roles(dl):
    """Personagens em jogo agora: {roleid: (userid, nome)}. GMListOnlineUser (352) do gdeliveryd,
    paginado pelo handler (-1 = fim)."""
    out, handler = {}, -1
    for _ in range(1000):
        body = dl.send(352, struct.pack(">iIi", -1, 0, handler) + cuint_bytes(0), wait_type=353)
        r = Reader(body)
        retcode, _gm, _sid, handler = r._take(">i"), r._take(">i"), r._take(">I"), r._take(">i")
        if retcode != 0:
            break
        for _ in range(r.cuint()):
            userid, roleid = r._take(">i"), r._take(">i")
            r._take(">i"); r._take(">I"); r._take(">i"); r._take(">b")  # linkid, localsid, gsid, status
            out[roleid] = (userid, r.octets().decode("utf-16-le", "replace"))
        if handler == -1:
            break
    return out


def add_cash(db, userid, cash):
    """DebugAddCash (521) ao gamedbd; cash em centesimos (100 = 1 no jogo). Devolve o retcode do
    AddCash_Re (516)."""
    body = db.send(521, struct.pack(">ii", userid, cash), wait_type=516,
                   match=lambda b: struct.unpack_from(">ii", b)[1] == userid)
    return struct.unpack_from(">i", body)[0]
