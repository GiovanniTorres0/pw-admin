"""Le cada personagem, decodifica, codifica de novo e compara os bytes.

Uso: python3 -I roundtrip_test.py <host> <porta_gamedbd> <userid> [<userid> ...]
Saida 0 = todos identicos. Nada e gravado no servidor.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pwproto as p  # noqa: E402


def main():
    host, port = sys.argv[1], int(sys.argv[2])
    db = p.RpcClient(host, port)
    falhas = total = 0
    for userid in map(int, sys.argv[3:]):
        roles = p.get_user_roles(db, userid)
        print(f"conta {userid}: {roles or 'sem personagens'}")
        for roleid, _ in roles:
            retcode, _ = p.get_role_raw(db, roleid)
            if retcode != 0:
                print(f"  {roleid}: retcode={retcode}, pulado")
                continue
            total += 1
            role, raw = p.get_role(db, roleid)
            again = p.encode(p.GRoleData, role, p.Writer()).bytes()
            ok = again == raw
            falhas += not ok
            st = role["status"]
            print(f"  {roleid} {p.role_name(role)!r} cls={role['base']['cls']} nivel={st['level']} "
                  f"gold={role['pocket']['money']} itens={len(role['pocket']['items'])} "
                  f"equip={len(role['equipment']['inv'])} bytes={len(raw)} -> {'IDENTICO' if ok else 'DIFERENTE'}")
    print(f"{total} personagens, {falhas} diferentes")
    sys.exit(1 if falhas or not total else 0)


if __name__ == "__main__":
    main()
