"""Draws desktop/gexdash.ico (16 / 32 / 48 / 256 px): GEX bars, green above and red below a
cyan spot line, on the dashboard's dark background. Pure Python (no Pillow).

Run:  desktop\\.venv\\Scripts\\python.exe desktop\\make_icon.py
"""
import os
import struct
import zlib

BG, GREEN, RED, CYAN = (10, 10, 15), (0, 255, 136), (255, 68, 68), (0, 212, 255)
# bars top to bottom: (side, length as a share of the width); the spot line sits after the 3rd
BARS = [(1, 0.45), (1, 0.80), (1, 0.30), (-1, 0.95), (-1, 0.55), (-1, 0.70)]


def draw(n: int) -> list:
    px = [[BG + (255,) for _ in range(n)] for _ in range(n)]
    pad = max(1, n // 10)
    rows = len(BARS)
    span = n - 2 * pad
    step = span / (rows + 0.5)              # room for the spot line between bar 3 and 4
    bar_h = max(1, round(step * 0.62))
    for i, (side, share) in enumerate(BARS):
        top = pad + round(step * (i + (0.5 if i >= 3 else 0)))
        length = max(1, round(span * share))
        x0 = n - pad - length                 # bars grow leftward from the right edge, like the charts
        for y in range(top, min(n - pad, top + bar_h)):
            for x in range(x0, n - pad):
                px[y][x] = (GREEN if side > 0 else RED) + (255,)
    spot = pad + round(step * 3.06)           # mid-gap: bar 3 ends at 2.62 steps, bar 4 starts at 3.5
    dash = max(1, n // 12)
    for x in range(pad, n - pad):
        if (x // dash) % 2 == 0:
            for t in range(max(1, n // 32)):
                if spot + t < n:
                    px[spot + t][x] = CYAN + (255,)
    return px


def png(px: list) -> bytes:
    n = len(px)
    raw = b''.join(b'\x00' + bytes(c for p in row for c in p) for row in px)
    chunk = lambda t, d: struct.pack('>I', len(d)) + t + d + struct.pack('>I', zlib.crc32(t + d) & 0xffffffff)
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', n, n, 8, 6, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(raw, 9)) + chunk(b'IEND', b''))


def ico(sizes=(16, 32, 48, 256)) -> bytes:
    images = [png(draw(n)) for n in sizes]
    head = struct.pack('<HHH', 0, 1, len(sizes))
    offset = 6 + 16 * len(sizes)
    entries = b''
    for n, img in zip(sizes, images):
        entries += struct.pack('<BBBBHHII', n % 256, n % 256, 0, 0, 1, 32, len(img), offset)
        offset += len(img)
    return head + entries + b''.join(images)


if __name__ == '__main__':
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'gexdash.ico')
    with open(out, 'wb') as f:
        f.write(ico())
    print(out)
