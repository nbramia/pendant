"""Ogg Opus writer for the pendant's 20 ms, 16 kHz Opus packets (RFC 7845).

Vendored from MAkcanca/pendant-cli (MIT), src/pendant_client/cli.py.
"""
from __future__ import annotations

import os
import struct
from pathlib import Path

OGG_CAPTURE_PATTERN = b"OggS"


def _crc32_ogg(data: bytes,
                _table: list[int] = []) -> int:  # noqa: B008  (initialised lazily)
    if not _table:
        # Standard CRC-32 polynomial used by Ogg (0x04c11db7), MSB-first,
        # init=0, no XOR, not reflected.
        for i in range(256):
            r = i << 24
            for _ in range(8):
                r = ((r << 1) ^ 0x04C11DB7) if (r & 0x80000000) else (r << 1)
            _table.append(r & 0xFFFFFFFF)
    crc = 0
    for b in data:
        crc = (((crc << 8) & 0xFFFFFFFF) ^ _table[((crc >> 24) ^ b) & 0xFF])
    return crc & 0xFFFFFFFF



class OggOpusWriter:
    """Minimal writer producing a valid .opus file from emitted Opus packets."""

    SAMPLE_RATE = 16_000  # pendant input sample rate (informational, in OpusHead)
    # Per RFC 7845 § 4, Opus granule positions are counted at a fixed
    # 48 kHz rate REGARDLESS of the input sample rate (Opus always
    # decodes to 48 kHz internally). 20 ms @ 48 kHz = 960 samples.
    # If you use 320 here (correct for 16 kHz input), every duration /
    # seek-bar UI reports 1/3 of actual playtime — audio plays at the
    # right speed (decoder doesn't care about granules), but the
    # player thinks the file is 3× shorter than it is.
    SAMPLES_PER_FRAME = 960

    def __init__(self, path: Path):
        self._fp = path.open("wb")
        self._serial = struct.unpack("<I", os.urandom(4))[0] | 1
        self._page_seq = 0
        self._granule = 0
        self._write_headers()

    def _write_page(self, packet: bytes,
                    *, is_first: bool = False, is_last: bool = False,
                    granule: int) -> None:
        # Segment table: each segment is at most 255 bytes; final segment
        # < 255 indicates packet end.
        n = len(packet)
        segs: list[int] = []
        while n >= 255:
            segs.append(255)
            n -= 255
        segs.append(n)

        header_type = 0
        if is_first:
            header_type |= 0x02  # bos
        if is_last:
            header_type |= 0x04  # eos

        head = bytearray()
        head += OGG_CAPTURE_PATTERN
        head += b"\x00"                                      # version
        head += struct.pack("<B", header_type)
        head += struct.pack("<q", granule)
        head += struct.pack("<I", self._serial)
        head += struct.pack("<I", self._page_seq)
        head += struct.pack("<I", 0)                         # crc placeholder
        head += struct.pack("<B", len(segs))
        head += bytes(segs)
        body = head + packet
        crc = _crc32_ogg(body)
        body = body[:22] + struct.pack("<I", crc) + body[26:]
        self._fp.write(body)
        self._page_seq += 1

    def _write_headers(self) -> None:
        # OpusHead (RFC 7845 §5.1)
        opus_head = (
            b"OpusHead"
            + b"\x01"                         # version
            + b"\x01"                         # channels = 1
            + struct.pack("<H", 0)            # pre-skip
            + struct.pack("<I", self.SAMPLE_RATE)  # input sample rate
            + struct.pack("<h", 0)            # output gain Q7.8
            + b"\x00"                         # mapping family
        )
        self._write_page(opus_head, is_first=True, granule=0)

        # OpusTags (RFC 7845 §5.2)
        vendor = b"limitless-pendant-client"
        opus_tags = (
            b"OpusTags"
            + struct.pack("<I", len(vendor)) + vendor
            + struct.pack("<I", 0)  # 0 user comments
        )
        self._write_page(opus_tags, granule=0)

    def add_packet(self, packet: bytes) -> None:
        if not packet:
            return
        self._granule += self.SAMPLES_PER_FRAME
        self._write_page(packet, granule=self._granule)

    def close(self) -> None:
        # Per RFC 3533 § 6, the last Ogg page of a logical stream must
        # have the EOS bit (0x04) set, with its granule position equal
        # to the final sample index. Without it, players compute the
        # duration from a partial parse and then keep playing past the
        # displayed length — the file is technically a truncated/in-
        # progress stream.
        try:
            if self._granule > 0:
                # Empty data page (1 segment of length 0) with EOS flag.
                self._write_page(b"", is_last=True, granule=self._granule)
            self._fp.flush()
            self._fp.close()
        except Exception:
            pass
