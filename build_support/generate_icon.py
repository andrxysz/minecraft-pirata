"""Gera o ícone ICO do bloco usando somente a biblioteca padrão."""

from pathlib import Path
import struct
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from launcher import ICON_COLORS, ICON_ROWS


def generate(destination: Path):
    images = []
    for size in (16, 32, 48, 64):
        pixels = bytearray()
        for y in reversed(range(size)):
            for x in range(size):
                color = ICON_COLORS[ICON_ROWS[y * 8 // size][x * 8 // size]]
                red, green, blue = (int(color[i:i + 2], 16) for i in (1, 3, 5))
                pixels.extend((blue, green, red, 255))
        mask = bytes(((size + 31) // 32) * 4 * size)
        header = struct.pack('<IiiHHIIiiII', 40, size, size * 2, 1, 32, 0, len(pixels), 0, 0, 0, 0)
        images.append((size, header + pixels + mask))
    offset = 6 + 16 * len(images)
    entries = []
    for size, data in images:
        entries.append(struct.pack('<BBBBHHII', size, size, 0, 0, 1, 32, len(data), offset))
        offset += len(data)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(struct.pack('<HHH', 0, 1, len(images)) + b''.join(entries) +
                            b''.join(data for _, data in images))


if __name__ == '__main__':
    generate(Path(__file__).with_name('MinePirata.ico'))
