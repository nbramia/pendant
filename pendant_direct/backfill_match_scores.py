"""One-off: score the clusters the voiceprint pass named before match scores were stored.

    python -m pendant_direct.backfill_match_scores [--dry-run] [--data DIR]

For each cluster with labeled_by 'match', no match_score and a stored embedding, computes the
score the processor would have stored (`process.match_scores`: the scorer for `speakers.scoring`
against the current voiceprints and calibration, excluding the cluster's own conversation) and
writes it to `match_score`. Nothing else changes: labels, tags and every other column stay.
Idempotent: a scored cluster is never touched, so a second run scores nothing. A cluster whose
person has no voiceprint now is skipped (counted, left NULL). Runs under the processor's lock;
`--dry-run` computes and counts without writing. Prints counts only.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

import numpy as np

from . import config, process, speakers, trash

COUNTS = ("scanned", "scored", "skipped_no_embedding", "skipped_no_profile")


def _todo(con) -> dict:
    """{(device, start_ms): [(id, label, embedding blob)]} for the clusters to score."""
    cols = {r[1] for r in con.execute("PRAGMA table_info(speaker_clusters)")}
    if "match_score" not in cols:
        return {}
    out: dict = {}
    for cid, dev, start, label, emb in con.execute(
            "SELECT id, device, start_ms, label, embedding FROM speaker_clusters "
            "WHERE labeled_by = 'match' AND match_score IS NULL AND label IS NOT NULL AND label != '' ORDER BY id"):
        out.setdefault((dev, start), []).append((cid, label, emb))
    return out


def _score(con, data_dir: Path, write: bool, scores: list | None) -> dict:
    sp = config.load(data_dir)["speakers"]
    vdir = speakers.voiceprint_dir(data_dir)
    prints = speakers.load_voiceprints(vdir)
    cal = None
    try:
        from . import calibration
        cal = calibration.load(con, vdir, sp)
    except Exception:  # as the processor: raw scores
        pass
    threshold = float(sp.get("match_threshold", 0.5))
    n = dict.fromkeys(COUNTS, 0)
    for key, rows in _todo(con).items():
        n["scanned"] += len(rows)
        have = [(cid, label, np.frombuffer(emb, dtype=np.float32)) for cid, label, emb in rows if emb]
        n["skipped_no_embedding"] += len(rows) - len(have)
        if not have:
            continue
        if not (prints or cal is not None):
            n["skipped_no_profile"] += len(have)
            continue
        vals = process.match_scores([e for _c, _l, e in have], [l for _c, l, _e in have], prints, cal, threshold, key)
        for (cid, _l, _e), v in zip(have, vals):
            if v is None:
                n["skipped_no_profile"] += 1
                continue
            n["scored"] += 1
            if scores is not None:
                scores.append(v)
            if write:
                con.execute("UPDATE speaker_clusters SET match_score = ? WHERE id = ? AND match_score IS NULL "
                            "AND labeled_by = 'match'", (v, cid))
    if write:
        con.commit()
    return n


def run(data_dir: Path, dry_run: bool = False, out=sys.stdout, scores: list | None = None) -> dict:
    """Counts: scanned, scored, skipped_no_embedding, skipped_no_profile. `scores` collects the values."""
    data_dir = Path(data_dir)
    db = data_dir / "pages.db"
    if not db.exists():
        raise FileNotFoundError("no pages.db in the data folder")
    if dry_run:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=15)
        try:
            n = _score(con, data_dir, False, scores)
        finally:
            con.close()
    else:
        with trash.processor_lock(data_dir, wait_s=30):
            con = process.db(db)
            try:
                n = _score(con, data_dir, True, scores)
            finally:
                con.close()
    print(("dry run, nothing written: " if dry_run else "") + ", ".join(f"{k}: {v}" for k, v in n.items()), file=out)
    return n


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--data", type=Path, default=Path(os.environ.get("PENDANT_DATA", Path.home() / "pendant-direct-data")))
    args = ap.parse_args(argv)
    try:
        run(args.data.expanduser(), args.dry_run)
    except Exception as e:
        print(f"backfill_match_scores failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
