import struct
import zlib

from langfuse_lab.attachments import PHOTO_SIZE, damage_photo_png, receipt_pdf


def test_photo_is_a_valid_deterministic_png():
    data = damage_photo_png()
    assert data == damage_photo_png()
    assert data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR"
    w, h = struct.unpack(">II", data[16:24])
    assert (w, h) == PHOTO_SIZE
    idat_len = struct.unpack(">I", data[33:37])[0]
    raw = zlib.decompress(data[41:41 + idat_len])
    assert len(raw) == h * (1 + 3 * w)
    assert data.endswith(b"IEND\xaeB`\x82")


def test_receipt_is_a_valid_pdf_with_a_correct_xref():
    data = receipt_pdf("8115")
    assert data.startswith(b"%PDF-1.4") and data.rstrip().endswith(b"%%EOF") and b"order 8115" in data
    xref_at = int(data.rsplit(b"startxref\n", 1)[1].split(b"\n", 1)[0])
    assert data[xref_at:xref_at + 4] == b"xref"
    first_obj = int(data[xref_at:].split(b"\n")[3][:10])
    assert data[first_obj:first_obj + 7] == b"1 0 obj"
    assert receipt_pdf("8115") == data and receipt_pdf("9999") != data
