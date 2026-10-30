# PW Admin

Painel web para administrar um servidor de **Perfect World 1.5.5 (v156)**: contas, personagens, inventário, correio e moderação. Python puro (só `pymysql`), uma página HTML, sem build.

> Projeto de fã, sem ligação com a Perfect World Entertainment / Wanmei. **Nenhum arquivo do jogo vem neste repositório**: o catálogo de itens, os ícones e os textos são gerados a partir do *seu* cliente e servidor (ver [Dados do jogo](#dados-do-jogo)).

## O que faz

- **Busca:** por nome, conta ou ID, feita no servidor (escala para milhares de contas), com lista de online e recentes. Atualização automática do personagem aberto.
- **Personagem:** nível, XP, SP, HP, MP, gold, atributos (com recálculo de HP, MP, dano e defesa base pelo nível e regra de pontos do login) e botão "mandar para a cidade".
- **Inventário:** equipado por espaço, bolsa e banco em grade de 8 como no jogo, com a dica (tooltip) do jogo. Colocar (bolsa ou banco) e tirar itens, gold e expansão de espaços.
- **Correio:** gold e itens. Equipamentos, voos, moda, poções e ovos de pet vão com o corpo binário que o servidor espera.
- **Moderação:** kick, mute, ban, backups e restauração.
- **Servidor:** aviso global e cash para a conta.

## Segurança

- Escuta só em `127.0.0.1`. O acesso é por túnel SSH, e o painel nunca fica exposto na internet.
- Só grava personagem **offline**, e só se ele passar no teste de ida e volta (decodificar e codificar dá os mesmos bytes).
- Cada gravação mexe **somente no personagem escolhido**: faz backup antes, confere depois e registra em `audit.log`.
- Tirar item confere ID e posição contra o que está gravado (página desatualizada não apaga o item errado).
- Campos editáveis e limites ficam numa lista fechada (`EDITABLE` em `server.py`); o resto é recusado.
- Um login por GM (senha PBKDF2 com sal). Toda ação vai para a tabela `pwadmin_auditoria` no MariaDB, com GM, IP e backup gerado.
- Backups numa pasta por personagem, apagados após 30 dias (o mais recente de cada um sempre fica).
- POST exige o cabeçalho `X-PWAdmin: 1` (contra CSRF); cookie `HttpOnly; SameSite=Strict`.

## Instalar (no servidor do jogo)

```bash
sudo apt install python3-pymysql
git clone https://github.com/GiovanniTorres0/pw-admin.git ~/pwpanel && cd ~/pwpanel
python3 -I server.py        # 1ª vez: cria config.json e mostra a senha do usuário admin
```

GMs (as tabelas `pwadmin_*` são criadas sozinhas no banco do authd):

```bash
python3 -I server.py admin NOME       # cria o GM ou troca a senha (mostra a senha nova)
python3 -I server.py desativar NOME   # bloqueia o acesso (cai em até 60 s)
python3 -I server.py admins           # lista
```

Depois edite `config.json` (modelo em `config.exemplo.json`): endereço do `gamedbd` (29400) e do `gdeliveryd` (29100), e usuário e senha do MariaDB do authd. Para trocar a senha do painel, apague `config.json` e rode de novo.

Serviço systemd: `sudo cp pwadmin.service /etc/systemd/system/ && sudo systemctl enable --now pwadmin` (ajuste usuário e caminho).

Acesso do seu PC:

```bash
ssh -N -L 8080:localhost:8080 usuario@seu-servidor
# abra http://localhost:8080
```

## Dados do jogo

O painel funciona sem eles, mas sem nomes, ícones e dicas. Gere a partir dos seus arquivos (rodar de `tools/`):

```bash
# catálogo de itens (elements.data do CLIENTE)
python -I -X utf8 gerar_catalogo_itens.py "<cliente>/element/data/elements.data" ../itens.json
# corpo dos itens que o painel cria (elements.data do SERVIDOR: gamed/config/elements.data)
python -I -X utf8 gerar_equipamentos.py "<servidor>/gamed/config/elements.data" ../dados/equipamentos.json
# ícones e textos (dos .pck do cliente)
python -I pw_pck_extract.py get "<cliente>/element/surfaces.pck" "surfaces/iconset/iconlist_ivtrm.dds" ../dados/iconlist_ivtrm.dds
python -I pw_pck_extract.py get "<cliente>/element/surfaces.pck" "surfaces/iconset/iconlist_ivtrm.txt" ../dados/iconlist_ivtrm.txt
python -I pw_pck_extract.py get "<cliente>/element/configs.pck" "configs/item_desc.txt" ../dados/item_desc.txt
python -I pw_pck_extract.py get "<cliente>/element/configs.pck" "configs/item_ext_desc.txt" ../dados/item_ext_desc.txt
python -I pw_pck_extract.py get "<cliente>/element/configs.pck" "configs/item_color.txt" ../dados/item_color.txt
```

Esses arquivos estão no `.gitignore` e nunca devem ser publicados.

## Arquivos

| Arquivo | O que é |
|---|---|
| `server.py` | Servidor HTTP e API |
| `index.html` | Interface |
| `pwproto.py` | Protocolo GNET: estruturas do personagem (`GRoleData`), RPC do `gamedbd`, protocolos do `gdeliveryd` |
| `equipamentos.py` | Monta o corpo binário dos itens criados pelo painel |
| `icones.py`, `textos.py`, `dica.py` | Ícones (atlas DXT) e dica do item no estilo do cliente |
| `dados/classes.json` | Crescimento de HP/MP/dano por classe, usado no recálculo dos atributos |
| `tools/` | Geradores dos dados, leitor do `elements.data` e extrator de `.pck` |
| `roundtrip_test.py` | Lê cada personagem, codifica de novo e compara os bytes (não grava nada) |

## Créditos

Leitor do `elements.data`, extrator de `.pck`, layout `v156.json` e a base de ícones e dicas vêm do [pw-universal-server](https://github.com/mur1ll0/pw-universal-server) (MIT, Murillo Andre Maleski). Licença: MIT (ver `LICENSE`).
