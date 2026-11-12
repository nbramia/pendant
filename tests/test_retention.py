"""Audio retention (pendant_direct.retention): what goes, what stays, and that a purged page still dedupes."""
from __future__ import annotations

import datetime as dt
import json
import time

import pytest

from pendant_direct import config as cfgmod
from pendant_direct import retention, trash
from test_process import at, fake_embedder, fake_whisper, opus, pages, proc, run, write_segment  # noqa: F401
from test_trash import A, B, DEV, delete, restore, state, two  # noqa: F401

NOW = retention.parse_now("2026-01-20")          # both conversations are 15 days old
SOON = retention.parse_now("2026-01-08")         # both are 3 days old


def chunk_view(proc, lo, hi):
    con = proc.db()
    try:
        return con.execute("SELECT device, page, ord, opus_sha, ts_ms, done, opus IS NULL FROM chunks "
                           "WHERE ts_ms BETWEEN ? AND ? ORDER BY device, ts_ms, page, ord", (lo, hi)).fetchall()
    finally:
        con.close()


def do_run(proc, **kw):
    con = proc.db()
    try:
        return retention.run(con, proc.DATA, now_ms=kw.pop("now_ms", NOW), **kw)
    finally:
        con.close()


def clips(proc):
    return sorted(p.name for p in (proc.DATA / "clips").iterdir())


def test_defaults_ship_disabled():
    r = cfgmod.DEFAULTS["retention"]
    assert r == {"enabled": False, "days": 7, "keep_starred": True}
    assert retention.settings(cfgmod.DEFAULTS)["enabled"] is False
    assert retention.settings({"retention": {"enabled": "yes"}})["enabled"] is False   # only a real true


def test_dry_run_changes_nothing_and_reports(two, capsys):
    before = state(two)
    con = two.db()
    rep = retention.report(retention.plan(con, two.DATA, now_ms=NOW))
    con.close()
    assert state(two) == before
    day = rep["days"]["2026-01-05"]
    assert day["clips"] == 1 and day["chunks"] == 150             # B only: A is starred
    assert day["chunk_bytes"] == 150 * len(opus(DEV, 0, 0)) and day["clip_bytes"] > 0
    assert [(r["start_ms"], r["why"]) for r in rep["starred_kept"]][0][0] == A
    assert "button star" in rep["starred_kept"][0]["why"]
    assert retention.main(["--data", str(two.DATA), "--dry-run", "--now", "2026-01-20"]) == 0
    assert "starred recordings kept: 1" in capsys.readouterr().out
    assert state(two) == before


def test_dry_run_opens_the_database_read_only(two):
    (two.DATA / "pages.db").chmod(0o444)
    try:
        assert retention.main(["--data", str(two.DATA), "--dry-run", "--now", "2026-01-20"]) == 0
    finally:
        (two.DATA / "pages.db").chmod(0o644)


def test_nothing_inside_the_window_is_touched(two):
    before = state(two)
    rep = do_run(two, now_ms=SOON)
    assert rep["total"]["chunks"] == 0 and rep["total"]["clips"] == 0
    assert state(two) == before


def test_pass_nulls_only_opus_of_old_unstarred_and_deletes_their_clip(two):
    before_rows = chunk_view(two, 0, 10**15)
    before = state(two)
    rep = do_run(two)
    assert rep["total"]["chunks"] == 150 and rep["total"]["clips"] == 1
    after_rows = chunk_view(two, 0, 10**15)
    assert len(after_rows) == len(before_rows)
    # same keys, sha, ts_ms, done; opus NULL exactly for B's chunks
    assert [r[:6] for r in after_rows] == [r[:6] for r in before_rows]
    a_view = chunk_view(two, A - 1000, A + 5000)
    b_view = chunk_view(two, B - 1000, B + 5000)
    assert all(not r[6] for r in a_view) and all(r[6] for r in b_view) and len(b_view) == 150
    assert len(clips(two)) == 1 and clips(two)[0] != ""
    # transcripts, summaries, speakers, vault and voiceprints are untouched
    after = state(two)
    for k in before:
        if k not in ("chunks", "files"):
            assert after[k] == before[k], k
    assert {k: v for k, v in after["files"].items() if "clips" not in k} == \
        {k: v for k, v in before["files"].items() if "clips" not in k}
    # a second pass has nothing left
    assert do_run(two)["total"]["chunks"] == 0


def test_starred_recordings_are_kept_forever(two):
    do_run(two, now_ms=retention.parse_now("2027-06-01"))
    assert all(not r[6] for r in chunk_view(two, A - 1000, A + 5000))


def test_app_star_and_notable_also_keep(two):
    con = two.db()
    con.execute("DELETE FROM stars")                     # no button star left on A
    con.execute("UPDATE conversations SET notable=0")
    con.commit()
    con.close()
    rep = do_run(two, now_ms=NOW)
    assert rep["total"]["chunks"] == 300                  # nothing starred any more
    # app star: override true protects B only
    proc = two
    (proc.DATA / "notable_overrides.json").write_text(json.dumps({f"{DEV}-{B}": True}))
    con = proc.db()
    p = retention.plan(con, proc.DATA, now_ms=NOW)
    con.close()
    assert [r["start_ms"] for r in p["starred_kept"]] == [B] and p["starred_kept"][0]["why"] == ["app star"]


def test_keep_starred_false_purges_everything_old(two):
    rep = do_run(two, keep_starred=False)
    assert rep["total"]["chunks"] == 300 and rep["total"]["clips"] == 2 and rep["starred_kept"] == []


def test_an_unplaced_button_press_protects_the_audio_around_it(two):
    con = two.db()
    con.execute("INSERT INTO stars (device, ts_ms, conv_device, conv_start_ms, text) VALUES (?,?,NULL,NULL,NULL)",
                (DEV, B + 1500))
    con.commit()
    con.close()
    rep = do_run(two)
    assert rep["total"]["chunks"] == 0                     # B's span is within the grace of the press


def test_chunks_not_done_and_enrollment_windows_are_never_purged(two):
    con = two.db()
    con.execute("UPDATE chunks SET done=0 WHERE ts_ms BETWEEN ? AND ?", (B, B + 1000))
    con.execute("INSERT INTO enrollments (name, device, start_ms, end_ms, lo_ms, hi_ms, status) "
                "VALUES ('Noah', ?, ?, ?, ?, ?, 'waiting')", (DEV, B + 2000, B + 2500, B + 1999, B + 2501))
    con.commit()
    con.close()
    do_run(two, keep_starred=False)
    con = two.db()
    kept = con.execute("SELECT COUNT(*) FROM chunks WHERE opus IS NOT NULL AND ts_ms >= ? AND ts_ms < ?",
                       (B, B + 3000)).fetchone()[0]
    undone = con.execute("SELECT COUNT(*) FROM chunks WHERE done=0 AND opus IS NULL").fetchone()[0]
    in_window = con.execute("SELECT COUNT(*) FROM chunks WHERE ts_ms BETWEEN ? AND ? AND opus IS NULL",
                            (B + 1999, B + 2501)).fetchone()[0]
    con.close()
    assert undone == 0 and in_window == 0 and kept > 0


def test_orphan_clips_older_than_the_window_go_and_new_ones_stay(two):
    d = two.DATA / "clips"
    old = d / "DEVA_20251201_100000.opus"
    new = d / "DEVA_20260119_100000.opus"
    odd = d / "notes.opus"
    for f in (old, new, odd):
        f.write_bytes(b"x" * 10)
    do_run(two)
    assert not old.exists() and new.exists() and odd.exists()


def test_the_trash_is_never_touched_and_restore_still_works(two):
    delete(two, B)
    trash_files = {str(p.relative_to(two.DATA)): p.read_bytes() for p in (two.DATA / "trash").rglob("*") if p.is_file()}
    assert trash_files
    do_run(two, keep_starred=False)
    assert {str(p.relative_to(two.DATA)): p.read_bytes()
            for p in (two.DATA / "trash").rglob("*") if p.is_file()} == trash_files
    restore(two, B)
    con = two.db()
    n = con.execute("SELECT COUNT(*) FROM chunks WHERE ts_ms BETWEEN ? AND ? AND opus IS NOT NULL",
                    (B - 1000, B + 5000)).fetchone()[0]
    con.close()
    assert n == 150                                         # restored with the trash's own audio


def test_deleting_a_conversation_whose_audio_was_purged_works(two):
    do_run(two)
    delete(two, B)                                          # no clip, NULL opus: still moves and restores
    restore(two, B)
    assert len(chunk_view(two, B - 1000, B + 5000)) == 150


def test_purged_pages_arriving_again_are_deduped_and_never_retranscribed(two):
    do_run(two, keep_starred=False)
    rows = chunk_view(two, 0, 10**15)
    con = two.db()
    convs = con.execute("SELECT * FROM conversations ORDER BY start_ms").fetchall()
    con.close()
    raw = two.NOTES_DIR / "2026-01-05.md"
    daily = raw.read_text()
    # the same pages again: a USB backup copy and a live copy
    write_segment(two.SPOOL, "DEVA_20260106T020000Z_usb.pmsg", pages(DEV, A, 3, first_index=0))
    write_segment(two.SPOOL, "DEVA_20260106T030000Z.pmsg", pages(DEV, B, 3, first_index=3))
    con, n = run(two)
    con2 = two.db()
    assert n == 0
    assert chunk_view(two, 0, 10**15) == rows                 # no new rows, opus still NULL
    assert con2.execute("SELECT * FROM conversations ORDER BY start_ms").fetchall() == convs
    assert con2.execute("SELECT COUNT(*) FROM chunks WHERE opus IS NOT NULL").fetchone() == (0,)
    assert raw.read_text() == daily
    assert not (two.DATA / "clips").exists() or clips(two) == []
    con.close()
    con2.close()


def test_a_null_chunk_never_reaches_transcription(two):
    con = two.db()
    con.execute("UPDATE chunks SET done=0, opus=NULL WHERE ts_ms BETWEEN ? AND ?", (B, B + 5000))
    con.commit()
    assert [r for conv in two.conversations(con, DEV) for r in conv if r[2] is None] == []
    con.close()


def test_maybe_run_is_off_by_default_and_runs_once_a_day(two):
    con = two.db()
    logs = []
    assert retention.maybe_run(con, two.DATA, cfgmod.load(two.DATA), now_ms=NOW, log=logs.append) is None
    assert con.execute("SELECT COUNT(*) FROM chunks WHERE opus IS NULL").fetchone() == (0,)
    cfg = {"retention": {"enabled": True, "days": 7, "keep_starred": True}}
    rep = retention.maybe_run(con, two.DATA, cfg, now_ms=NOW, log=logs.append)
    assert rep["total"]["chunks"] == 150
    assert any("vacuumed" in m for m in logs)
    assert con.execute("SELECT value FROM meta WHERE key='retention_last'").fetchone() == ("2026-01-20",)
    assert retention.maybe_run(con, two.DATA, cfg, now_ms=NOW + 3600_000, log=logs.append) is None
    # vacuum at most weekly
    con.execute("UPDATE meta SET value='2026-01-19' WHERE key='retention_last'")
    con.execute("UPDATE chunks SET opus = x'b8', done=1 WHERE rowid IN (SELECT rowid FROM chunks LIMIT 5)")
    con.commit()
    logs.clear()
    retention.maybe_run(con, two.DATA, cfg, now_ms=NOW, log=logs.append)
    assert not any("vacuumed" in m for m in logs)
    con.close()


def test_cli_real_pass_runs_only_when_enabled_and_there_is_no_force(two, capsys):
    before = state(two)
    assert retention.main(["--data", str(two.DATA), "--now", "2026-01-20"]) == 2
    with pytest.raises(SystemExit):
        retention.main(["--data", str(two.DATA), "--now", "2026-01-20", "--force"])   # the flag is gone
    assert state(two) == before
    assert retention.main(["--data", str(two.DATA), "--dry-run", "--now", "2026-01-20"]) == 0   # always allowed
    (two.DATA / "config.json").write_text(json.dumps({"retention": {"enabled": True}}))
    assert retention.main(["--data", str(two.DATA), "--now", "2026-01-20"]) == 0
    assert len(chunk_view(two, B - 1000, B + 5000)) == 150


def test_playback_cache_follows_the_clip_and_the_same_rule(two):
    cache = two.DATA / "audio_cache"
    cache.mkdir()
    stems = sorted(p.stem for p in (two.DATA / "clips").iterdir())
    a_stem, b_stem = stems                                   # 10:00 (starred) and 10:20
    files = {n: cache / f"{n}.m4a" for n in (a_stem, b_stem, "DEVA_20251201_100000", "DEVA_20260119_100000")}
    for f in files.values():
        f.write_bytes(b"m" * 7)
    con = two.db()
    rep = retention.report(retention.plan(con, two.DATA, now_ms=NOW))
    con.close()
    assert rep["total"]["cache"] == 2 and rep["total"]["cache_bytes"] == 14        # B's and the old orphan
    assert all(f.exists() for f in files.values())                                 # dry run removes nothing
    do_run(two)
    assert files[a_stem].exists() and files["DEVA_20260119_100000"].exists()
    assert not files[b_stem].exists() and not files["DEVA_20251201_100000"].exists()
    # starred recordings' playback files stay forever
    do_run(two, now_ms=retention.parse_now("2027-06-01"))
    assert files[a_stem].exists() and not files["DEVA_20260119_100000"].exists()
    assert "cache" in retention.format_report(rep, dry=True)


# ---------------------------------------------------------------------------
# the raw spool

def spool_names(proc):
    return sorted(p.name for p in proc.SPOOL.glob("*.pmsg"))


@pytest.fixture
def three(two):
    """`two` plus an old unstarred segment C, ingested and transcribed."""
    write_segment(two.SPOOL, "DEVA_20260105T170000Z.pmsg", pages(DEV, at(200), 3, first_index=10))
    con, n = run(two)
    con.close()
    assert n == 1
    return two


def test_old_ingested_unstarred_segment_is_purged_and_a_starred_overlap_is_kept(three):
    before = state(three)
    con = three.db()
    rep = retention.report(retention.plan(con, three.DATA, now_ms=NOW))
    con.close()
    assert spool_names(three) == ["DEVA_20260105T150000Z.pmsg", "DEVA_20260105T170000Z.pmsg"]   # dry run
    assert rep["days"]["2026-01-05"]["spool"] == 1 and rep["days"]["2026-01-05"]["spool_bytes"] > 0
    assert rep["spool_kept"]["segments"] == 1
    assert "old spool segments kept" in retention.format_report(rep, dry=True)
    assert state(three) == before
    do_run(three)
    # A and B share the first segment and A is starred: it stays; C goes
    assert spool_names(three) == ["DEVA_20260105T150000Z.pmsg"]
    con = three.db()
    assert con.execute("SELECT COUNT(*) FROM segments").fetchone() == (2,)       # still recorded as ingested
    con.close()


def test_a_segment_not_yet_ingested_is_kept(three):
    write_segment(three.SPOOL, "DEVA_20260105T180000Z.pmsg", pages(DEV, at(300), 3, first_index=20))
    do_run(three)
    assert spool_names(three) == ["DEVA_20260105T150000Z.pmsg", "DEVA_20260105T180000Z.pmsg"]


def test_a_recent_segment_and_an_enrollment_window_keep_their_segments(three):
    con = three.db()
    con.execute("INSERT INTO enrollments (name, device, start_ms, end_ms, lo_ms, hi_ms, status) "
                "VALUES ('Noah', ?, ?, ?, ?, ?, 'waiting')", (DEV, at(200), at(200) + 500, at(200) - 1, at(200) + 501))
    con.commit()
    con.close()
    do_run(three)
    assert "DEVA_20260105T170000Z.pmsg" in spool_names(three)
    rep = do_run(three, now_ms=SOON)
    assert rep["total"]["spool"] == 0


def test_chunks_still_waiting_keep_their_segment(three):
    con = three.db()
    con.execute("UPDATE chunks SET done=0 WHERE ts_ms BETWEEN ? AND ?", (at(200), at(200) + 3000))
    con.commit()
    con.close()
    do_run(three)
    assert "DEVA_20260105T170000Z.pmsg" in spool_names(three)


def test_reingest_after_the_spool_is_purged_creates_nothing(three):
    do_run(three, keep_starred=False)
    assert spool_names(three) == []
    con = three.db()
    rows = con.execute("SELECT device, page, ord, opus_sha, ts_ms, done, opus IS NULL FROM chunks ORDER BY 1,2,3,4").fetchall()
    convs = con.execute("SELECT * FROM conversations ORDER BY start_ms").fetchall()
    con.close()
    # the phone uploads the same pages again under new names, after everything was purged
    write_segment(three.SPOOL, "DEVA_20260106T020000Z.pmsg", pages(DEV, A, 3, first_index=0))
    write_segment(three.SPOOL, "DEVA_20260106T030000Z.pmsg", pages(DEV, at(200), 3, first_index=10))
    con, n = run(three)
    con.close()
    assert n == 0
    con = three.db()
    assert con.execute("SELECT device, page, ord, opus_sha, ts_ms, done, opus IS NULL FROM chunks ORDER BY 1,2,3,4"
                       ).fetchall() == rows
    assert con.execute("SELECT * FROM conversations ORDER BY start_ms").fetchall() == convs
    con.close()


def test_spool_is_kept_until_the_star_backfill_has_read_it(three):
    con = three.db()
    con.execute("DELETE FROM meta WHERE key='stars_backfilled'")
    con.commit()
    con.close()
    do_run(three)
    assert len(spool_names(three)) == 2


def test_clips_overlapping_an_unplaced_press_or_an_enrollment_window_survive(two):
    con = two.db()
    con.execute("INSERT INTO stars (device, ts_ms, conv_device, conv_start_ms, text) VALUES (?,?,NULL,NULL,NULL)",
                (DEV, B + 1500))
    con.commit()
    con.close()
    before = clips(two)
    rep = do_run(two)
    assert clips(two) == before and rep["total"]["clips"] == 0               # B's clip stays, like its chunks
    con = two.db()
    con.execute("DELETE FROM stars WHERE conv_device IS NULL")
    con.execute("INSERT INTO enrollments (name, device, start_ms, end_ms, lo_ms, hi_ms, status) "
                "VALUES ('Noah', ?, ?, ?, ?, ?, 'waiting')", (DEV, B + 500, B + 900, B + 400, B + 1000))
    con.commit()
    con.close()
    rep = do_run(two, keep_starred=False)
    assert rep["total"]["clips"] == 1 and len(clips(two)) == 1               # A's clip goes (no longer starred); B's stays
    con = two.db()
    con.execute("UPDATE enrollments SET status='rejected'")
    con.commit()
    con.close()
    assert do_run(two, keep_starred=False)["total"]["clips"] == 1             # released: now it goes too
