"""Short voices out of the voiceprints: `python -m pendant_direct.sample_hygiene [--data DIR] [--apply]`.

A voice with less speech than `speakers.min_sample_speech_s` doesn't become a voiceprint sample
when it is named (answers, meeting and calendar names), but voiceprints written before may hold
such samples. This lists them per person (a dry run, the default: nothing is written) and with
--apply takes them out under the processor's lock, after saving them to
`<data>/backups/short-samples-<time>.json`; `--restore FILE` puts them back. Only samples keyed by
a voice are considered: readings, single lines and a voiceprint's legacy part stay. A person whose
every sample is short is left without a voiceprint. Prints names and counts only.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

from . import config, speakers


def plan(data_dir: Path) -> dict:
    """{"min_s", "people": {name: {"samples", "short": [voice ids]}}}, read only."""
    data_dir = Path(data_dir)
    min_s = float(config.load(data_dir)["speakers"].get("min_sample_speech_s") or 0)
    vdir = speakers.voiceprint_dir(data_dir)
    people: dict = {}
    if min_s <= 0 or not (data_dir / "pages.db").exists():
        return {"min_s": min_s, "people": people}
    con = sqlite3.connect(f"file:{data_dir / 'pages.db'}?mode=ro", uri=True)
    try:
        for name in speakers._names(vdir):
            vp = speakers._read(vdir, name) if not name.startswith(".") else None
            if vp is None:
                continue
            short = []
            for cid in vp.ids:
                if cid <= 0:
                    continue     # a reading or a single line, not a voice
                row = con.execute("SELECT speech_s FROM speaker_clusters WHERE id = ?", (cid,)).fetchone()
                if row is not None and float(row[0] or 0) < min_s:
                    short.append(cid)
            people[name] = {"samples": vp.count, "short": short}
    finally:
        con.close()
    return {"min_s": min_s, "people": people}


def _enrolled(con: sqlite3.Connection, ids) -> dict:
    return {cid: e for cid, e in con.execute(
        f"SELECT id, enrolled_in FROM speaker_clusters WHERE id IN ({','.join('?' * len(ids))})", list(ids))}


def apply(data_dir: Path, p: dict) -> Path | None:
    """Take the planned short samples out; returns the backup file (None when there was nothing)."""
    from . import trash
    data_dir = Path(data_dir)
    ids = [c for v in p["people"].values() for c in v["short"]]
    if not ids:
        return None
    vdir = speakers.voiceprint_dir(data_dir)
    with trash.processor_lock(data_dir):
        con = sqlite3.connect(data_dir / "pages.db", timeout=30)
        try:
            before = _enrolled(con, ids)
            backup = data_dir / "backups" / f"short-samples-{time.strftime('%Y%m%d-%H%M%S')}.json"
            backup.parent.mkdir(parents=True, exist_ok=True)
            taken = speakers.take(vdir, ids, dry_run=True)
            backup.write_text(json.dumps({"taken": taken, "enrolled_in": {str(k): v for k, v in before.items()}}))
            speakers.take(vdir, ids)
            con.executemany("UPDATE speaker_clusters SET enrolled_in = '' WHERE id = ?", [(c,) for c in ids])
            con.commit()
        finally:
            con.close()
    return backup


def restore(data_dir: Path, backup: Path) -> int:
    """Put back the samples a run of apply() took out (and their enrolled_in). Returns how many."""
    from . import trash
    data_dir = Path(data_dir)
    saved = json.loads(Path(backup).read_text())
    with trash.processor_lock(data_dir):
        n = speakers.put(speakers.voiceprint_dir(data_dir), saved["taken"])
        con = sqlite3.connect(data_dir / "pages.db", timeout=30)
        try:
            con.executemany("UPDATE speaker_clusters SET enrolled_in = ? WHERE id = ?",
                            [(v, int(k)) for k, v in saved["enrolled_in"].items()])
            con.commit()
        finally:
            con.close()
    return n


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--data", type=Path, default=Path(os.environ.get("PENDANT_DATA", Path.home() / "pendant-direct-data")))
    ap.add_argument("--apply", action="store_true", help="take the short samples out (default: dry run)")
    ap.add_argument("--restore", type=Path, metavar="FILE", help="put back the samples a previous --apply took out")
    args = ap.parse_args(argv)
    data = args.data.expanduser()
    if args.restore:
        print(f"restored {restore(data, args.restore)} samples")
        return 0
    p = plan(data)
    if p["min_s"] <= 0:
        print("speakers.min_sample_speech_s is 0: nothing to do")
        return 0
    for name, v in sorted(p["people"].items()):
        print(f"  {name}: {v['samples']} samples, {len(v['short'])} from voices under {p['min_s']:g} s "
              f"-> {v['samples'] - len(v['short'])}")
    total = sum(len(v["short"]) for v in p["people"].values())
    if not args.apply:
        print(f"dry run, nothing changed: {total} short samples; run again with --apply to take them out")
        return 0
    backup = apply(data, p)
    print(f"took out {total} short samples" + (f"; saved to {backup} (--restore puts them back)" if backup else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
