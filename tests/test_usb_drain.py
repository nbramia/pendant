"""Tests for the USB drain's pure helpers (no serial port needed)."""
from __future__ import annotations

import struct

from pendant_direct.process import records
from pendant_direct.proto import flash_page_pb2, server_pb2
from pendant_direct.usb import drain


def zephyr_hexdump(data: bytes, base: int = 0) -> str:
    """Format bytes like Zephyr's shell_hexdump (what `ftl read_sector` prints):
    `00000000: 0a 0b ... 07  08 ... 0f |ascii...       |`"""
    lines = []
    for off in range(0, len(data), 16):
        row = data[off:off + 16]
        hexpart = ""
        for i in range(16):
            if i and i % 8 == 0:
                hexpart += " "
            hexpart += f"{row[i]:02x} " if i < len(row) else "   "
        ascii_ = "".join(chr(b) if 0x20 <= b < 0x7F else "." for b in row)
        lines.append(f"{base + off:08X}: {hexpart}|{ascii_:<16}|")
    return "\n".join(lines)


def make_sector(flash_page: bytes | None, size: int = 4096) -> bytes:
    sec = bytearray(b"\xAB" * 0x38)  # FTL header (opaque)
    if flash_page is None:
        sec += b"\xFF\xFF\xFF\xFF"
    else:
        sec += struct.pack("<I", len(flash_page)) + flash_page
    sec += b"\xFF" * (size - len(sec))
    return bytes(sec)


def audio_page(opus: bytes, ts_ms: int = 1_762_000_000_000) -> bytes:
    page = flash_page_pb2.FlashPage()
    page.absolute_timestamp_ms = ts_ms
    page.chunks.add().audio_data.codec_beamforming_data = opus
    return page.SerializeToString()


def test_parse_status():
    text = """storage status
@DEVICE_HEX_ID 0F1E2D3C4B5A6978
@OLDEST 120
@NEWEST 4410
@STORAGE_INSTANCE 7
some free text line
@BROKEN
uart:~$"""
    st = drain.parse_status(text)
    assert st == {"DEVICE_HEX_ID": "0F1E2D3C4B5A6978", "OLDEST": "120",
                  "NEWEST": "4410", "STORAGE_INSTANCE": "7"}


def test_parse_hexdump_round_trip_with_ascii_column():
    fp = audio_page(b"\xb8" + b"AB 12 cd |" * 30)  # printable bytes that look hex-ish in the ascii column
    sec = make_sector(fp)
    text = "ftl read_sector 1000\n" + zephyr_hexdump(sec) + "\nuart:~$ "
    assert drain.parse_hexdump(text) == sec


def test_parse_hexdump_partial_last_line():
    data = bytes(range(21))
    assert drain.parse_hexdump(zephyr_hexdump(data)) == data


def test_parse_hexdump_ignores_noise():
    assert drain.parse_hexdump("uart:~$ \nerror: no such sector\n") == b""


def test_flashpage_from_sector_valid():
    fp = audio_page(b"\xb8opus")
    assert drain.flashpage_from_sector(make_sector(fp)) == fp


def test_flashpage_from_sector_erased():
    assert drain.flashpage_from_sector(make_sector(None)) is None


def test_flashpage_from_sector_short_or_zero():
    assert drain.flashpage_from_sector(b"\x00" * 0x3B) is None
    assert drain.flashpage_from_sector(b"") is None
    assert drain.flashpage_from_sector(make_sector(b"")) is None  # zero length
    # length larger than what the sector holds
    sec = bytearray(make_sector(audio_page(b"x"), size=0x40))
    struct.pack_into("<I", sec, 0x38, 0x1000)
    assert drain.flashpage_from_sector(bytes(sec)) is None


def test_wrap_decodes_as_batch_storage_buffer():
    fp = audio_page(b"\xb8opus")
    msg = server_pb2.PendantAllMsg()
    msg.ParseFromString(drain.wrap(4242, fp))
    assert msg.WhichOneof("content") == "storage_buffer"
    assert msg.storage_buffer.index == 4242
    assert msg.storage_buffer.ingest_type == 2
    assert msg.storage_buffer.flash_page == fp


def test_write_segment_is_readable_and_atomic(tmp_path):
    recs = [drain.wrap(i, audio_page(bytes([i]) * 10)) for i in range(5)]
    seg = drain.write_segment("0F1E2D3C4B5A6978", recs, spool=tmp_path)
    assert seg.parent == tmp_path
    assert seg.name.startswith("0F1E2D3C4B5A6978_") and seg.suffix == ".pmsg"
    assert list(records(seg)) == recs
    assert [p.name for p in tmp_path.iterdir()] == [seg.name]  # no .part left
