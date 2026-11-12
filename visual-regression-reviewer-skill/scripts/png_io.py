#!/usr/bin/env python3
"""Stdlib PNG reading and writing for screenshots: decode to RGB rows, crop, encode, diff images.

Decodes 8-bit, non-interlaced greyscale, grey+alpha, RGB, RGBA and palette PNGs (what browsers,
Playwright, BackstopJS and Storybook write) into rows of RGB bytes, alpha composited on white.
The None, Sub and Up filters are decoded with byte-level shortcuts; Average and Paeth row by row.

    from png_io import read, write, crop, Image
    img = read(Path('page.png'))            # Image(width, height, rows) with 3 bytes per pixel
    write(Path('crop.png'), crop(img, x, y, w, h))
"""
from __future__ import annotations
import hashlib, struct, zlib
from dataclasses import dataclass
from itertools import accumulate
from pathlib import Path

SIG = b'\x89PNG\r\n\x1a\n'


@dataclass
class Image:
    width: int
    height: int
    rows: list[bytes]   # RGB, 3 bytes per pixel

    def px(self, x: int, y: int) -> tuple[int, int, int]:
        r = self.rows[y]
        i = 3 * x
        return r[i], r[i + 1], r[i + 2]


def _unfilter(raw: bytes, w: int, h: int, bpp: int) -> list[bytes]:
    stride = w * bpp
    rows: list[bytes] = []
    prev = bytes(stride)
    # masks for the Up filter: even and odd bytes are added as separate big-integer lanes, so a carry out of
    # one byte lands in a masked-off byte and each byte sums mod 256 (several times faster than per byte)
    half = (stride + 1) // 2
    lo = int.from_bytes(b'\xff\x00' * half, 'big') >> (8 * (half * 2 - stride))
    hi = int.from_bytes(b'\x00\xff' * half, 'big') >> (8 * (half * 2 - stride))
    i = 0
    for _ in range(h):
        f = raw[i]
        line = raw[i + 1:i + 1 + stride]
        i += 1 + stride
        if f == 0:
            cur = bytes(line)
        elif f == 1:
            out = bytearray(stride)
            for c in range(bpp):
                out[c::bpp] = bytes(map((255).__and__, accumulate(line[c::bpp])))
            cur = bytes(out)
        elif f == 2:
            a, b = int.from_bytes(line, 'big'), int.from_bytes(prev, 'big')
            cur = ((((a & lo) + (b & lo)) & lo) | (((a & hi) + (b & hi)) & hi)).to_bytes(stride, 'big')
        else:
            out = bytearray(line)
            for x in range(stride):
                a = out[x - bpp] if x >= bpp else 0
                b = prev[x]
                if f == 3:
                    out[x] = (out[x] + ((a + b) >> 1)) & 255
                else:
                    c = prev[x - bpp] if x >= bpp else 0
                    p = a + b - c
                    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                    out[x] = (out[x] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
            cur = bytes(out)
        rows.append(cur)
        prev = cur
    return rows


def decode(data: bytes) -> Image:
    if data[:8] != SIG:
        raise ValueError('not a PNG')
    pos, idat, palette, trns = 8, [], None, None
    w = h = depth = ctype = interlace = 0
    while pos < len(data):
        n, typ = struct.unpack('>I4s', data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + n]
        if typ == b'IHDR':
            w, h, depth, ctype, _, _, interlace = struct.unpack('>IIBBBBB', body)
        elif typ == b'PLTE':
            palette = body
        elif typ == b'tRNS':
            trns = body
        elif typ == b'IDAT':
            idat.append(body)
        pos += 12 + n
    if depth != 8 or interlace or ctype not in (0, 2, 3, 4, 6):
        raise ValueError(f'unsupported PNG (bit depth {depth}, colour type {ctype}, interlace {interlace})')
    bpp = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[ctype]
    rows = _unfilter(zlib.decompress(b''.join(idat)), w, h, bpp)
    if ctype == 2:
        return Image(w, h, rows)
    out = []
    for r in rows:
        if ctype == 6:
            if r[3::4].count(255) == w:
                ba = bytearray(r)
                del ba[3::4]
                out.append(bytes(ba))
                continue
            o = bytearray(3 * w)
            for x in range(w):
                a = r[4 * x + 3]
                for c in range(3):
                    o[3 * x + c] = (r[4 * x + c] * a + 255 * (255 - a)) // 255
            out.append(bytes(o))
        elif ctype == 0:
            out.append(bytes(v for v in r for _ in range(3)))
        elif ctype == 4:
            o = bytearray(3 * w)
            for x in range(w):
                g, a = r[2 * x], r[2 * x + 1]
                o[3 * x:3 * x + 3] = bytes([(g * a + 255 * (255 - a)) // 255] * 3)
            out.append(bytes(o))
        else:
            o = bytearray(3 * w)
            for x, idx in enumerate(r):
                o[3 * x:3 * x + 3] = palette[3 * idx:3 * idx + 3]
            out.append(bytes(o))
    return Image(w, h, out)


def read(path: Path) -> Image:
    return decode(Path(path).read_bytes())


def encode(img: Image) -> bytes:
    def chunk(typ: bytes, body: bytes) -> bytes:
        return struct.pack('>I', len(body)) + typ + body + struct.pack('>I', zlib.crc32(typ + body) & 0xffffffff)
    raw = b''.join(b'\x00' + r for r in img.rows)
    return (SIG + chunk(b'IHDR', struct.pack('>IIBBBBB', img.width, img.height, 8, 2, 0, 0, 0)) +
            chunk(b'IDAT', zlib.compress(raw, 9)) + chunk(b'IEND', b''))


def write(path: Path, img: Image) -> str:
    """Write the image and return its SHA-256."""
    data = encode(img)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def crop(img: Image, x: int, y: int, w: int, h: int) -> Image:
    """Crop to the rectangle, clipped to the image; areas outside it are filled with light grey."""
    rows = []
    for yy in range(y, y + h):
        if 0 <= yy < img.height:
            r = img.rows[yy]
            left = b'\xee' * 3 * max(0, -x)
            mid = r[3 * max(0, x):3 * min(img.width, x + w)]
            right = b'\xee' * 3 * max(0, x + w - img.width)
            rows.append(left + mid + right)
        else:
            rows.append(b'\xee' * 3 * w)
    return Image(w, h, rows)


def blank(w: int, h: int, colour: bytes = b'\xff\xff\xff') -> Image:
    return Image(w, h, [colour * w for _ in range(h)])


def diff_image(base: Image, changed: dict[int, set[int]], aa: dict[int, set[int]], flaky: dict[int, set[int]]) -> Image:
    """A faded copy of the baseline with changed pixels red, anti-aliasing yellow and flaky blue."""
    rows = []
    for y in range(base.height):
        r = bytearray(255 - (255 - v) // 4 for v in base.rows[y])
        for xs, colour in ((flaky.get(y, ()), b'\x40\x80\xff'), (aa.get(y, ()), b'\xff\xd0\x00'), (changed.get(y, ()), b'\xff\x20\x20')):
            for x in xs:
                if x < base.width:
                    r[3 * x:3 * x + 3] = colour
        rows.append(bytes(r))
    return Image(base.width, base.height, rows)


def sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()
