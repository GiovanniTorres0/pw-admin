"""Icones de itens recortados do atlas do cliente.

Baseado no web-admin/backend/painel/icones.py do pw-universal-server (MIT, Murillo Andre
Maleski). Aqui foi acrescentado o DXT5, que e o formato do atlas do cliente Mayhem.

O cliente guarda os icones de inventario em surfaces.pck: Surfaces\\IconSet\\IconList_IvtrM.dds
com um .txt (largura, altura, linhas, colunas e o nome de cada celula, linha a linha). Um item
acha a celula pelo nome do arquivo do seu file_icon, em minusculas. Os dois arquivos ficam em
dados/ (extrair com tools/pw_pck_extract.py; ver README.md).
"""
import functools
import os
import struct
import zlib

PASTA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dados")
CABECALHO_DDS = 128


class Atlas:
    def __init__(self, nome):
        with open(os.path.join(PASTA, f"iconlist_{nome}.txt"), "rb") as f:
            texto = f.read().split(b"\n")
        self.largura, self.altura, self.linhas, self.colunas = (int(texto[i].strip()) for i in range(4))
        self.indice = {}
        for n, linha in enumerate(texto[4:4 + self.linhas * self.colunas]):
            celula = linha.strip(b"\r").decode("gbk", "replace").lower()
            if celula:
                self.indice.setdefault(celula, n)
        with open(os.path.join(PASTA, f"iconlist_{nome}.dds"), "rb") as f:
            self.dds = f.read()
        altura, largura = struct.unpack_from("<II", self.dds, 12)
        self.formato = self.dds[84:88]
        if self.formato not in DECODIFICADORES or largura < self.colunas * self.largura \
                or altura < self.linhas * self.altura:
            raise ValueError("atlas de icones inesperado (esperado DXT1/3/5 do tamanho do indice)")
        self.bytes_por_bloco = 8 if self.formato == b"DXT1" else 16
        self.blocos_por_linha = largura // 4

    def png(self, titulo):
        n = self.indice.get(titulo.lower())
        if n is None:
            return None
        decodificar = DECODIFICADORES[self.formato]
        x0 = (n % self.colunas) * self.largura
        y0 = (n // self.colunas) * self.altura
        pixels = bytearray(self.largura * self.altura * 4)
        for by in range(self.altura // 4):
            for bx in range(self.largura // 4):
                desloc = CABECALHO_DDS + ((y0 // 4 + by) * self.blocos_por_linha + (x0 // 4 + bx)) * self.bytes_por_bloco
                bloco = decodificar(self.dds[desloc:desloc + self.bytes_por_bloco])
                for py in range(4):
                    for px in range(4):
                        d = ((by * 4 + py) * self.largura + bx * 4 + px) * 4
                        pixels[d:d + 4] = bloco[py * 4 + px]
        return codificar_png(self.largura, self.altura, bytes(pixels))

    def folha_png(self):
        """O atlas inteiro (so a area com celulas) num PNG so. A pagina baixa uma vez e
        recorta cada icone por CSS, em vez de pedir um PNG por item."""
        decodificar = DECODIFICADORES[self.formato]
        largura, altura = self.colunas * self.largura, self.linhas * self.altura
        pixels = bytearray(largura * altura * 4)
        linha_px = largura * 4
        for by in range(altura // 4):
            for bx in range(largura // 4):
                desloc = CABECALHO_DDS + (by * self.blocos_por_linha + bx) * self.bytes_por_bloco
                bloco = decodificar(self.dds[desloc:desloc + self.bytes_por_bloco])
                for py in range(4):
                    d = (by * 4 + py) * linha_px + bx * 16
                    pixels[d:d + 16] = b"".join(bloco[py * 4:py * 4 + 4])
        return codificar_png(largura, altura, bytes(pixels))


def rgb565(c):
    r, g, b = (c >> 11) & 31, (c >> 5) & 63, c & 31
    return (r << 3 | r >> 2, g << 2 | g >> 4, b << 3 | b >> 2)


def _cores_dxt(b, quatro_sempre):
    c0, c1, bits = struct.unpack("<HHI", b)
    a, z = rgb565(c0), rgb565(c1)
    if quatro_sempre or c0 > c1:
        cores = [a, z, tuple((2 * p + q) // 3 for p, q in zip(a, z)), tuple((p + 2 * q) // 3 for p, q in zip(a, z))]
    else:
        cores = [a, z, tuple((p + q) // 2 for p, q in zip(a, z)), None]
    return [cores[(bits >> (2 * i)) & 3] for i in range(16)]


def decodificar_bloco_dxt1(b):
    """Duas cores RGB565 e 2 bits por pixel. c0 > c1: quatro cores opacas; senao tres e o
    indice 3 transparente."""
    return [bytes(c + (255,)) if c else b"\x00\x00\x00\x00" for c in _cores_dxt(b, False)]


def decodificar_bloco_dxt3(b):
    """16 alfas de 4 bits (a x 17) e o bloco de cor do DXT1, sempre com quatro cores."""
    alfas = int.from_bytes(b[:8], "little")
    return [bytes(c + (((alfas >> (4 * i)) & 15) * 17,)) for i, c in enumerate(_cores_dxt(b[8:], True))]


def decodificar_bloco_dxt5(b):
    """Dois alfas de 8 bits e 3 bits por pixel (a0 > a1: 6 interpolados; senao 4 + 0 e 255),
    depois o bloco de cor do DXT1 com quatro cores."""
    a0, a1 = b[0], b[1]
    if a0 > a1:
        alfas = [a0, a1] + [((7 - i) * a0 + i * a1) // 7 for i in range(1, 7)]
    else:
        alfas = [a0, a1] + [((5 - i) * a0 + i * a1) // 5 for i in range(1, 5)] + [0, 255]
    bits = int.from_bytes(b[2:8], "little")
    return [bytes(c + (alfas[(bits >> (3 * i)) & 7],)) for i, c in enumerate(_cores_dxt(b[8:], True))]


DECODIFICADORES = {b"DXT1": decodificar_bloco_dxt1, b"DXT3": decodificar_bloco_dxt3,
                   b"DXT5": decodificar_bloco_dxt5}


def codificar_png(largura, altura, rgba):
    def pedaco(tipo, dados):
        return struct.pack(">I", len(dados)) + tipo + dados + struct.pack(">I", zlib.crc32(tipo + dados))
    linhas = b"".join(b"\x00" + rgba[y * largura * 4:(y + 1) * largura * 4] for y in range(altura))
    return (b"\x89PNG\r\n\x1a\n" + pedaco(b"IHDR", struct.pack(">IIBBBBB", largura, altura, 8, 6, 0, 0, 0))
            + pedaco(b"IDAT", zlib.compress(linhas, 9)) + pedaco(b"IEND", b""))


@functools.lru_cache(maxsize=1)
def atlas():
    return Atlas("ivtrm")


@functools.lru_cache(maxsize=1)
def folha():
    """PNG da folha inteira. Fica salvo em dados/ (montar leva alguns segundos); apagar o
    arquivo depois de trocar o atlas."""
    caminho = os.path.join(PASTA, "iconlist_ivtrm.png")
    if not os.path.exists(caminho):
        with open(caminho + ".tmp", "wb") as f:
            f.write(atlas().folha_png())
        os.replace(caminho + ".tmp", caminho)
    with open(caminho, "rb") as f:
        return f.read()


def celula(nome):
    """Indice da celula do icone na folha, ou None."""
    return atlas().indice.get(nome.lower())


@functools.lru_cache(maxsize=8192)
def png_do_icone(nome):
    """PNG do icone pelo nome do arquivo (ja decodificado de GBK), ou None."""
    return atlas().png(nome)
