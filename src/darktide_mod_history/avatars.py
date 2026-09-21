from __future__ import annotations

import struct
import subprocess
import zlib
from pathlib import Path

AVATAR_SIZE = 128
GRID_SIZE = 5
CELL_SIZE = 16
BACKGROUND = bytes((240, 240, 240))


def write_avatar(path: Path, data: bytes) -> None:
    # Gource requires RGB/RGBA textures; GitHub also serves indexed PNGs.
    result = subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-xerror",
        "-i", "pipe:0", "-frames:v", "1", "-pix_fmt", "rgba",
        "-c:v", "png", "-f", "image2pipe", "pipe:1",
    ], input=data, stdout=subprocess.PIPE, check=True)
    path.write_bytes(result.stdout)


def png_chunk(kind: bytes, data: bytes) -> bytes:
    payload = kind + data
    return struct.pack(">I", len(data)) + payload + struct.pack(">I", zlib.crc32(payload))


def write_identicon(path: Path, identity_hash: str) -> None:
    seed = bytes.fromhex(identity_hash)
    foreground = bytes(40 + value // 2 for value in seed[:3])
    half_width = (GRID_SIZE + 1) // 2
    cells = [[bool(seed[3 + row * half_width + col] & 1) for col in range(half_width)] for row in range(GRID_SIZE)]
    cells[GRID_SIZE // 2][half_width - 1] = True
    margin = (AVATAR_SIZE - GRID_SIZE * CELL_SIZE) // 2
    pixels = bytearray()
    for y in range(AVATAR_SIZE):
        pixels.append(0)
        for x in range(AVATAR_SIZE):
            row, col = (y - margin) // CELL_SIZE, (x - margin) // CELL_SIZE
            filled = 0 <= row < GRID_SIZE and 0 <= col < GRID_SIZE
            if filled:
                filled = cells[row][min(col, GRID_SIZE - 1 - col)]
            pixels.extend(foreground if filled else BACKGROUND)
    header = struct.pack(">IIBBBBB", AVATAR_SIZE, AVATAR_SIZE, 8, 2, 0, 0, 0)
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + png_chunk(b"IHDR", header)
        + png_chunk(b"IDAT", zlib.compress(pixels))
        + png_chunk(b"IEND", b"")
    )
