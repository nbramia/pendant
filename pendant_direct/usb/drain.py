"""Dump the pendant's flash over USB into the upload spool, then wipe it.

The backup path for when the phone hasn't kept up (or for a pendant that
was never paired). Each batch of pages is written as a `.pmsg` segment in
the same format the iPhone app uploads, so `pendant_direct.process` dedupes
and transcribes it like any other upload. Pages go newest-first so recent
audio lands first. Throughput is fixed by the shell's hex dump: about 1.8 s
per 4 KB page, roughly real time.

When every page from oldest to the starting newest is in the spool, the
pendant's capture storage is cleared (`storage clear`), unless --no-wipe.
State lives in ~/pendant-direct-data/usb/<device>.json so an unplug resumes.

  .venv/bin/python -m pendant_direct.usb.drain [--no-wipe] [--limit N]
"""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import os
import re
import struct
import time
from pathlib import Path

from ..proto import server_pb2
from .shell import Shell

DATA = Path.home() / "pendant-direct-data"
SPOOL = DATA / "spool"
STATE_DIR = DATA / "usb"
AREA2_START, AREA2_PAGES = 768, 95232
BATCH = 50
HEXLINE = re.compile(r"^[0-9A-Fa-f]{8}:\s+((?:[0-9A-Fa-f]{2}\s+){1,16})")
EMPTY = 0xFFFF0000  # NEWEST at or above this means freshly wiped


def log(msg: str) -> None:
    print(f"[{dt.datetime.now():%H:%M:%S}] {msg}", flush=True)


def parse_status(text: str) -> dict[str, str]:
    """`storage status` prints `@KEY value` lines."""
    out = {}
    for line in text.splitlines():
        parts = line.strip().split()
        if len(parts) == 2 and parts[0].startswith("@"):
            out[parts[0][1:]] = parts[1]
    return out


def parse_hexdump(text: str) -> bytes:
    data = bytearray()
    for line in text.splitlines():
        m = HEXLINE.match(line.strip())
        if m:
            data.extend(int(h, 16) for h in m.group(1).split())
    return bytes(data)


def flashpage_from_sector(sec: bytes) -> bytes | None:
    """Sector layout: [0x00..0x37] FTL header, u32-LE length at 0x38, FlashPage at 0x3C.
    Erased sectors read 0xFFFFFFFF as the length."""
    if len(sec) < 0x3C:
        return None
    (n,) = struct.unpack_from("<I", sec, 0x38)
    if n == 0 or n > len(sec) - 0x3C:
        return None
    return sec[0x3C:0x3C + n]


def wrap(index: int, flash_page: bytes) -> bytes:
    """Wrap a raw FlashPage the way the pendant sends it over BLE (batch ingest)."""
    msg = server_pb2.PendantAllMsg()
    msg.storage_buffer.ingest_type = 2
    msg.storage_buffer.index = index
    msg.storage_buffer.flash_page = flash_page
    return msg.SerializeToString()


def write_segment(device: str, records: list[bytes], spool: Path = SPOOL) -> Path:
    spool.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    final = spool / f"{device}_{stamp}_usb{time.monotonic_ns() % 10**6:06d}.pmsg"
    tmp = final.with_name("." + final.name + ".part")
    with open(tmp, "wb") as f:
        for r in records:
            f.write(struct.pack("<I", len(r)) + r)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, final)
    return final


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-wipe", action="store_true")
    ap.add_argument("--limit", type=int, help="only the newest N pages; never wipes")
    args = ap.parse_args()

    DATA.mkdir(parents=True, exist_ok=True)
    lock = open(DATA / ".usb.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("another USB drain is running")
        return 1

    sh = Shell()
    st = parse_status(sh.cmd("storage status", wait=1.5))
    device = st.get("DEVICE_HEX_ID", "unknown")
    oldest, newest = int(st.get("OLDEST", "0")), int(st.get("NEWEST", "0"))
    if newest >= EMPTY or newest < oldest:
        log(f"{device}: storage empty")
        return 0
    floor = max(oldest, newest - args.limit + 1) if args.limit else oldest

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    state_file = STATE_DIR / f"{device}.json"
    state = json.loads(state_file.read_text()) if state_file.exists() else {}
    if state.get("instance") != st.get("STORAGE_INSTANCE"):
        state = {"instance": st.get("STORAGE_INSTANCE"), "newest": newest, "cursor": newest}
    # Pages recorded since the last run come first, then resume the older sweep.
    ranges = []
    if newest > state["newest"]:
        ranges.append((newest, state["newest"] + 1))
    ranges.append((state["cursor"], floor))
    state["newest"] = newest
    log(f"{device}: pages {oldest}..{newest}, to dump {ranges}")

    def dump(hi: int, lo_limit: int, track_cursor: bool) -> None:
        cursor = hi
        while cursor >= lo_limit:
            lo = max(lo_limit, cursor - BATCH + 1)
            records = []
            for i in range(cursor, lo - 1, -1):
                sec = parse_hexdump(sh.cmd(f"ftl read_sector {AREA2_START + i % AREA2_PAGES}", wait=1.4))
                fp = flashpage_from_sector(sec)
                if fp:
                    records.append(wrap(i, fp))
            records.reverse()
            if records:
                seg = write_segment(device, records)
                log(f"pages {lo}..{cursor}: {len(records)} -> {seg.name}")
            cursor = lo - 1
            if track_cursor:
                state["cursor"] = cursor
            state_file.write_text(json.dumps(state))

    for i, (hi, lo) in enumerate(ranges):
        dump(hi, lo, track_cursor=(i == len(ranges) - 1))

    if args.limit or args.no_wipe:
        log("done (no wipe)")
        return 0
    sh.cmd("storage clear", wait=4.0)
    state_file.unlink(missing_ok=True)
    log(f"{device}: all pages spooled; storage cleared")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
