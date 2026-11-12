"""One-off: classify conversations written before kind/importance existed.

    .venv/bin/python -m pendant_direct.backfill_kind [--data DIR] [--notes DIR]
        [--all] [--dry-run] [--no-vault] [--day YYYY-MM-DD] [--table]

Two independent steps:

1. Classify. For each conversation without a kind (every one with --all) the stored
   transcript (the day's Raw section) goes to DeepSeek on Fireworks through the
   summarizer, as a new recording would; only its kind and importance are used, so
   titles and Raw headings stay as they are. `classify` combines that with local
   signals (words, audio length, the wearer's labelled share of speech, how unlabelled
   voices score against his voiceprint). Each row is committed as soon as it is
   classified, so a rerun never repeats a DeepSeek call. Whisper's dropped
   low-confidence segments aren't stored, so that signal is 0 here. Without a
   Fireworks key the local rules alone decide. `notable` is not touched here.
2. Vault sync (skipped with --no-vault; no DeepSeek). From the stored importance, with
   the same rules as normal processing (`notable_overrides.json`, no Event for a
   conversation with a meeting note), an importance-2 conversation becomes notable and
   gets its Event note, and the Highlights of every day with an importance-2
   conversation are rewritten. Idempotent: run again to repair a failed write.

Defaults: --data ~/pendant-direct-data, --notes: the configured notes folder (config.notes_dir).
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from . import classify as classifier
from . import config as cfgmod
from . import feed, process, summarize
from . import highlights as hl


def classify_all(con, data: Path, notes: Path, cfg: dict, *, redo: bool, dry_run: bool, day: str | None,
                 post=None, log=print) -> list[dict]:
    wearer = cfgmod.wearer(cfg.get("speakers"))
    where = ["1=1" if redo else "kind IS NULL"] + (["day = ?"] if day else [])
    rows = con.execute(f"SELECT * FROM conversations WHERE {' AND '.join(where)} ORDER BY start_ms",
                       (day,) if day else ()).fetchall()
    out, texts = [], {}
    for r in rows:
        if r["day"] not in texts:
            f = notes / f"{r['day']}.md"
            texts[r["day"]] = {c["heading"]: c for c in feed.parse_daily(f.read_text())} if f.exists() else {}
        sec = texts[r["day"]].get(r["raw_heading"])
        if sec is None:
            log(f"{r['day']} {r['raw_heading'][:5]}: no transcript section; skipped")
            continue
        summary = summarize.summarize(sec["text"], cfg, log=log, post=post, con=con, purpose="backfill_kind")
        clusters = [(c["label"], c["speech_s"], None if c["embedding"] is None else
                     __import__("numpy").frombuffer(c["embedding"], dtype="float32"))
                    for c in con.execute("SELECT label, speech_s, embedding FROM speaker_clusters "
                                         "WHERE device=? AND start_ms=?", (r["device"], r["start_ms"]))]
        share, absent, voices_n = classifier.evidence(
            process.wearer_similarities(clusters, wearer, data), wearer, cfg.get("classify"))
        sig = classifier.Signals(words=sec["words"], audio_s=r["audio_s"], wearer_share=share,
                                 wearer_absent=absent, voices=voices_n)
        kind, imp, reasons = classifier.safe_classify(summary, sig, cfg.get("classify"), log=log)
        out.append({"day": r["day"], "time": r["raw_heading"][:5], "title": r["title"], "kind": kind,
                    "importance": imp, "reasons": reasons})
        if not dry_run:
            con.execute("UPDATE conversations SET kind=?, importance=?, kind_reasons=? "
                        "WHERE device=? AND start_ms=?",
                        (kind, imp, json.dumps(reasons), r["device"], r["start_ms"]))
            con.commit()
    return out


def sync_vault(con, cfg: dict, data: Path, log=print) -> int:
    """Event notes and Highlights for importance-2 conversations, from the database
    alone. Returns the number of writes that failed (rerun to repair them)."""
    overrides = hl.load_overrides(data / "notable_overrides.json")
    rules = cfg.get("notable", {})
    failed = 0
    rows = con.execute("SELECT * FROM conversations WHERE importance >= 2 ORDER BY start_ms").fetchall()
    for r in rows:
        if r["meeting_note"]:
            continue
        have = bool(r["notable"] and r["event_file"]
                    and (process.NOTES_DIR.parent / "Events" / r["event_file"]).exists())
        if have or not hl.is_notable(
                key=hl.event_id(r["device"], r["start_ms"]), audio_s=r["audio_s"],
                action_items=json.loads(r["action_items"] or "[]"), title=r["title"] or "",
                overview=r["overview"] or "", rules=rules, overrides=overrides, importance=r["importance"]):
            continue
        try:
            ok = process.rewrite_event(con, r["device"], r["start_ms"])   # sets notable + event_file on success
            con.commit()
        except Exception as e:
            ok = False
            log(f"{r['day']} {r['raw_heading'][:5]}: event note failed ({type(e).__name__})")
        failed += not ok
    for d in sorted({r["day"] for r in rows}):
        try:
            process.write_highlights(con, d)
        except Exception as e:
            failed += 1
            log(f"highlights for {d} failed ({type(e).__name__})")
    return failed


def run(data: Path, notes: Path, *, redo: bool = False, dry_run: bool = False, vault: bool = True,
        day: str | None = None, post=None, log=print) -> list[dict]:
    """Classify and return one report row per conversation handled."""
    process.DATA, process.DB, process.NOTES_DIR = data, data / "pages.db", notes
    cfg = cfgmod.load(data)
    cfg["jev_action_items"] = False          # titles/items are discarded: skip the extra call
    con = process.db()
    con.row_factory = sqlite3.Row
    try:
        out = classify_all(con, data, notes, cfg, redo=redo, dry_run=dry_run, day=day, post=post, log=log)
        if vault and not dry_run and sync_vault(con, cfg, data, log=log):
            log("some vault writes failed; run again to repair them (no DeepSeek calls are repeated)")
    finally:
        con.close()
    return out


def table(rows: list[dict]) -> str:
    lines = ["| Time | Title | Kind | Importance | Reasons |", "|---|---|---|---|---|"]
    lines += [f"| {r['day']} {r['time']} | {r['title']} | {r['kind']} | {r['importance']} | "
              f"{'; '.join(r['reasons'])} |" for r in rows]
    return "\n".join(lines)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--data", type=Path, default=process.DATA)
    ap.add_argument("--notes", type=Path, default=process.NOTES_DIR)
    ap.add_argument("--all", action="store_true", help="redo conversations that already have a kind")
    ap.add_argument("--dry-run", action="store_true", help="classify and print, write nothing")
    ap.add_argument("--no-vault", action="store_true", help="skip step 2 (Event notes, Highlights)")
    ap.add_argument("--day")
    ap.add_argument("--table", action="store_true", help="print a markdown table")
    a = ap.parse_args(argv)
    rows = run(a.data, a.notes, redo=a.all, dry_run=a.dry_run, vault=not a.no_vault, day=a.day)
    print(table(rows) if a.table else "\n".join(
        f"{r['day']} {r['time']} {r['kind']:<12} {r['importance']}  {r['title']}" for r in rows))
    print(f"{len(rows)} conversations classified", file=sys.stderr)


if __name__ == "__main__":
    main()
