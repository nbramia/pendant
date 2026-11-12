"""Tests for the Ogg Opus writer: page structure, CRCs and granule positions."""
from __future__ import annotations

import struct

from pendant_direct.oggopus import OggOpusWriter, _crc32_ogg


def parse_pages(data: bytes):
    pages = []
    pos = 0
    while pos < len(data):
        assert data[pos:pos + 4] == b"OggS"
        header_type, granule, serial, seq, crc, nsegs = struct.unpack_from("<BqIIIB", data, pos + 5)
        lacing = data[pos + 27:pos + 27 + nsegs]
        body_len = sum(lacing)
        end = pos + 27 + nsegs + body_len
        raw = bytearray(data[pos:end])
        raw[22:26] = b"\0\0\0\0"
        pages.append(dict(type=header_type, granule=granule, serial=serial, seq=seq,
                          crc_ok=_crc32_ogg(bytes(raw)) == crc,
                          body=data[pos + 27 + nsegs:end]))
        pos = end
    return pages


def test_ogg_opus_structure(tmp_path):
    out = tmp_path / "a.opus"
    w = OggOpusWriter(out)
    packets = [bytes([0xB8]) + bytes([i]) * (40 + i) for i in range(5)] + [b"\xb8" + b"z" * 600]
    for p in packets:
        w.add_packet(p)
    w.add_packet(b"")  # ignored
    w.close()

    data = out.read_bytes()
    assert data.startswith(b"OggS")
    pages = parse_pages(data)
    assert all(p["crc_ok"] for p in pages)
    assert len({p["serial"] for p in pages}) == 1
    assert [p["seq"] for p in pages] == list(range(len(pages)))

    head, tags, *audio, eos = pages
    assert head["body"].startswith(b"OpusHead") and head["type"] & 0x02
    assert tags["body"].startswith(b"OpusTags")
    assert [p["body"] for p in audio] == packets  # incl. the >255-byte packet
    assert [p["granule"] for p in audio] == [960 * (i + 1) for i in range(len(packets))]
    assert eos["type"] & 0x04 and eos["granule"] == 960 * len(packets)


def test_crc_known_vector():
    # Ogg CRC-32 (poly 0x04C11DB7, init 0, unreflected) of "123456789" is 0x89A1897F.
    assert _crc32_ogg(b"123456789") == 0x89A1897F
