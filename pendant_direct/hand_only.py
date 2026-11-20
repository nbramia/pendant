"""Only the wearer's own answers in the voiceprints: `python -m pendant_direct.hand_only [--data DIR] [--apply]`.

A voiceprint sample should come only from a voice the wearer named himself: an answer in the app (a
name, a one-tap guess, a transcript correction of that voice or line) or a voice-sample reading.
Voiceprints written before that rule may hold other samples:

  meeting    a meeting note's speaker names (`labeled_by = 'meeting'`)
  calendar   a calendar meeting's other attendee (`labeled_by = 'calendar'`)
  sitting    a voice a "this conversation" answer reached in another piece, not the one he tapped
             (`named_via`, else read from the reassignment undo records)
  bulk       a voice an "all past conversations" answer reached (`named_via`, else the undo records)
  other      a voice that is no longer labelled by the wearer (`labeled_by` not 'user')
  legacy     the part of a voiceprint written before samples were kept one by one (provenance unknown)

This lists them per person (a dry run, the default: nothing is written). With --apply it copies the
voiceprint folder to `<data>/backups/hand-only-<time>/voiceprints/`, writes what it took out to
`manifest.json` there, then, under the processor's lock, takes those samples out (a person left with
none loses the voiceprint), empties the legacy parts, clears their voices' `enrolled_in` and marks the
voices sitting and bulk answers reached (`named_via`). `--restore DIR` puts the folder and the rows back.
Prints names and counts only.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path

import numpy as np

from . import speakers

KINDS = ("meeting", "calendar", "sitting", "bulk", "other", "legacy")


def reached_by_answers(data_dir: Path) -> dict[int, str]:
    """{voice id: 'sitting' | 'bulk'}: voices a reassignment named that weren't in the tapped voice's
    own conversation (the reassignment undo records: before/after rows)."""
    out: dict[int, str] = {}
    for f in sorted((Path(data_dir) / "reassign_undo").glob("*/manifest.json")):
        try:
            m = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        before = {r["id"]: r for r in (m.get("before") or {}).get("speaker_clusters", [])}
        after = {r["id"]: r for r in (m.get("after") or {}).get("speaker_clusters", [])}
        via = "bulk" if m.get("scope") == "all" else "sitting"
        for i, r in after.items():
            b = before.get(i) or {}
            named = r.get("labeled_by") == "user" and (b.get("labeled_by") != "user" or b.get("label") != r.get("label"))
            if named and not (r.get("device") == m.get("device") and r.get("start_ms") == m.get("start_ms")):
                out.setdefault(int(i), via)
    return out


def plan(data_dir: Path) -> dict:
    """{"people": {name: {"samples", "legacy", "take": {kind: [voice ids]}}}, "reached": {id: via}}, read only."""
    data_dir = Path(data_dir)
    vdir = speakers.voiceprint_dir(data_dir)
    people: dict = {}
    reached = reached_by_answers(data_dir)
    if not (data_dir / "pages.db").exists():
        return {"people": people, "reached": {}}
    con = sqlite3.connect(f"file:{data_dir / 'pages.db'}?mode=ro", uri=True)
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info(speaker_clusters)")}
        via_col = "named_via" if "named_via" in cols else "NULL"
        for name in speakers._names(vdir):
            vp = speakers._read(vdir, name) if not name.startswith(".") else None
            if vp is None:
                continue
            take: dict[str, list[int]] = {}
            for cid in vp.ids:
                if cid <= 0:
                    continue     # a reading or a single line he reassigned: his own answer
                row = con.execute(f"SELECT labeled_by, {via_col} FROM speaker_clusters WHERE id = ?", (cid,)).fetchone()
                by, via = (row or (None, None))
                kind = (via or reached.get(cid) or ("" if by == "user" else
                                                    by if by in ("meeting", "calendar") else "other"))
                if kind:
                    take.setdefault(kind, []).append(cid)
            people[name] = {"samples": vp.count, "legacy": int(vp.legacy), "take": take}
    finally:
        con.close()
    return {"people": people, "reached": {str(k): v for k, v in reached.items()}}


def apply(data_dir: Path, p: dict) -> Path | None:
    """Take the planned samples and legacy parts out; returns the backup folder (None when nothing to do)."""
    from . import trash
    data_dir = Path(data_dir)
    vdir = speakers.voiceprint_dir(data_dir)
    ids = [c for v in p["people"].values() for cs in v["take"].values() for c in cs]
    legacy = [n for n, v in p["people"].items() if v["legacy"]]
    if not ids and not legacy and not p["reached"]:
        return None
    with trash.processor_lock(data_dir):
        backup = data_dir / "backups" / f"hand-only-{time.strftime('%Y%m%d-%H%M%S')}"
        backup.mkdir(parents=True)
        shutil.copytree(vdir, backup / "voiceprints")
        con = sqlite3.connect(data_dir / "pages.db", timeout=30)
        try:
            from . import voices
            voices.ensure_schema(con)
            rows = {cid: (e, v) for cid, e, v in con.execute(
                "SELECT id, enrolled_in, named_via FROM speaker_clusters WHERE id IN "
                f"({','.join('?' * len(ids + [int(k) for k in p['reached']]))})",
                ids + [int(k) for k in p["reached"]])} if (ids or p["reached"]) else {}
            (backup / "manifest.json").write_text(json.dumps({
                "plan": p, "rows": {str(k): {"enrolled_in": e, "named_via": v} for k, (e, v) in rows.items()}}))
            if ids:
                speakers.take(vdir, ids)
            with speakers.voiceprint_lock(vdir):
                for name in legacy:
                    vp = speakers._read(vdir, name)
                    if vp is None or not vp.legacy:
                        continue
                    vp.legacy_sum = np.zeros_like(vp.legacy_sum)
                    vp.legacy = 0
                    speakers._write(vdir, name, vp if vp.ids else None)
            con.executemany("UPDATE speaker_clusters SET enrolled_in = '' WHERE id = ?", [(c,) for c in ids])
            con.executemany("UPDATE speaker_clusters SET named_via = ? WHERE id = ? AND labeled_by = 'user'",
                            [(v, int(k)) for k, v in p["reached"].items()])
            con.commit()
        finally:
            con.close()
    return backup


def restore(data_dir: Path, backup: Path) -> int:
    """Put back the voiceprint folder and the voices' rows an apply() changed. Returns the rows restored."""
    from . import trash
    data_dir, backup = Path(data_dir), Path(backup)
    saved = json.loads((backup / "manifest.json").read_text())
    vdir = speakers.voiceprint_dir(data_dir)
    with trash.processor_lock(data_dir):
        with speakers.voiceprint_lock(vdir):
            for f in vdir.glob("*.npz"):
                f.unlink()
            for f in (backup / "voiceprints").glob("*"):
                shutil.copy2(f, vdir / f.name)
        con = sqlite3.connect(data_dir / "pages.db", timeout=30)
        try:
            con.executemany("UPDATE speaker_clusters SET enrolled_in = ?, named_via = ? WHERE id = ?",
                            [(r["enrolled_in"], r["named_via"], int(k)) for k, r in saved["rows"].items()])
            con.commit()
        finally:
            con.close()
    return len(saved["rows"])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--data", type=Path, default=Path(os.environ.get("PENDANT_DATA", Path.home() / "pendant-direct-data")))
    ap.add_argument("--apply", action="store_true", help="take the samples out (default: dry run)")
    ap.add_argument("--restore", type=Path, metavar="DIR", help="put back what a previous --apply took out")
    args = ap.parse_args(argv)
    data = args.data.expanduser()
    if args.restore:
        print(f"restored the voiceprints and {restore(data, args.restore)} voices")
        return 0
    p = plan(data)
    total = {k: 0 for k in KINDS}
    for name, v in sorted(p["people"].items()):
        parts = {k: len(c) for k, c in v["take"].items()}
        if v["legacy"]:
            parts["legacy"] = v["legacy"]
        for k, n in parts.items():
            total[k] += n
        left = v["samples"] - sum(parts.values())
        print(f"  {name}: {v['samples']} samples, not from his answers {parts or 'none'} -> {left}")
    print(f"voices reached by sitting or bulk answers: {len(p['reached'])}")
    if not args.apply:
        print(f"dry run, nothing changed: {total}; run again with --apply to take them out")
        return 0
    backup = apply(data, p)
    print(f"took out {total}" + (f"; saved to {backup} (--restore puts it back)" if backup else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
