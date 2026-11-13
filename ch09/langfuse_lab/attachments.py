"""Customer uploads for module 05 (pure python, no Langfuse, no PIL): a damage photo and a PDF receipt.

Both are deterministic, so the same bytes (and so the same Langfuse media id) come back on every call.
"""
from __future__ import annotations

import struct
import zlib

PHOTO_SIZE = (96, 64)


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


def damage_photo_png() -> bytes:
    """A small RGB PNG: a brown parcel on a grey table with a dark diagonal 'crack' across it."""
    w, h = PHOTO_SIZE
    rows = []
    for y in range(h):
        row = bytearray([0])  # filter type 0 (none) per scanline
        for x in range(w):
            pixel = (200, 200, 205)  # table
            if 16 <= x < 80 and 10 <= y < 54:
                pixel = (156, 110, 60)  # parcel
                if abs((x - 16) * 44 // 64 - (y - 10)) <= 1:
                    pixel = (40, 25, 15)  # the damage
            row.extend(pixel)
        rows.append(bytes(row))
    header = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)  # 8-bit RGB
    return (b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", header)
            + _png_chunk(b"IDAT", zlib.compress(b"".join(rows), 9)) + _png_chunk(b"IEND", b""))


def receipt_pdf(order_id: str = "8115") -> bytes:
    """A valid one-page PDF receipt for `order_id` (no PII in it)."""
    text = f"Acme receipt - order {order_id} - paid by card ending 1111"
    stream = f"BT /F1 14 Tf 40 100 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 500 200] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{off:010d} 00000 n \n".encode() for off in offsets)
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)
