"""Delete, restore, purge and tombstones (pendant_direct.trash), and the app star."""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request

import numpy as np
import pytest

from pendant_direct import feed, speakers, trash, voices
from test_process import (BASE, at, button_msg, daily_for, fake_embedder, fake_whisper, make_msg,  # noqa: F401
                          opus, pages, proc, run, write_segment)

DEV = "DEVA"
A, B = at(0), at(20)
TABLES = ("conversations", "speaker_clusters", "speaker_turns", "stars", "paragraph_samples", "task_requests", "task_scans",
          "strangers", "noise_exemplars", "voice_offers", "spans", "transcript_relabels")


def a_pages():
    return pages(DEV, A, 3) + [button_msg(page_index=20, ts_ms=A, presses=[(1, A + 1000)])]


@pytest.fixture
def two(proc, fake_whisper, monkeypatch):
    """Conversations A (10:00, starred, notable) and B (10:20) on one pendant, with
    voiceprint samples from A's voice and one of A's lines, a pending task, a
    Voice Scoring offer and a stranger line."""
    vdir = proc.DATA / "voiceprints"
    speakers.enroll(vdir, "Noah", np.array([1, 0, 0, 0], np.float32), ["Noah"])   # legacy sample
    write_segment(proc.SPOOL, "DEVA_20260105T150000Z.pmsg", a_pages() + pages(DEV, B, 3, first_index=3))
    con, n = run(proc)
    assert n == 2
    ca = con.execute("SELECT id FROM speaker_clusters WHERE start_ms=?", (A,)).fetchone()[0]
    cb = con.execute("SELECT id FROM speaker_clusters WHERE start_ms=?", (B,)).fetchone()[0]
    sid = f"para:{DEV}:{A}:0"
    key = speakers.sample_key(sid)
    speakers.add(vdir, "Noah", ca, np.array([0.9, 0.1, 0, 0], np.float32))
    speakers.add(vdir, "Noah", cb, np.array([0.8, 0.2, 0, 0], np.float32))
    speakers.add(vdir, "Noah", key, np.array([0.7, 0.3, 0, 0], np.float32))
    con.execute("UPDATE speaker_clusters SET enrolled_in='Noah', labeled_by='user', label='Noah' WHERE id IN (?,?)",
                (ca, cb))
    con.execute("INSERT INTO paragraph_samples VALUES (?,?,?,?,?)",
                (sid, key, "Noah", np.array([0.7, 0.3, 0, 0], np.float32).tobytes(), 1.0))
    skey = speakers.sample_key(f"para:{DEV}:{A}:1")
    con.execute("INSERT INTO strangers (cluster_id, embedding, created_at) VALUES (?,?,?)",
                (skey, np.array([0, 1, 0, 0], np.float32).tobytes(), 2.0))
    con.execute("INSERT INTO paragraph_samples VALUES (?,?,?,?,?)",
                (f"para:{DEV}:{A}:1", skey, voices.STRANGER_HOLDER, np.array([0, 1, 0, 0], np.float32).tobytes(), 2.0))
    con.execute("INSERT INTO task_requests (device, start_ms, item, source, created_at) VALUES (?,?,?,?,?)",
                (DEV, A, "call the dentist", "app", 3.0))
    voices.mark(con, "2026-01-05", ca, "skipped")
    con.execute("UPDATE conversations SET kind='media', importance=0, kind_reasons=? WHERE start_ms=?",
                (json.dumps(["no wearer voice", "few words"]), A))
    con.commit()
    con.close()
    monkeypatch.setenv("PENDANT_DATA", str(proc.DATA))
    monkeypatch.setenv("PENDANT_NOTES_DIR", str(proc.NOTES_DIR))
    return proc


def state(proc) -> dict:
    """Everything a delete touches, for exact comparison."""
    con = proc.db()
    out = {}
    for t in TABLES:
        out[t] = sorted(con.execute(f"SELECT * FROM {t}").fetchall(), key=repr)
    out["chunks"] = con.execute("SELECT device, page, ord, opus_sha, ts_ms, opus, done FROM chunks "
                                "ORDER BY device, ts_ms, page, ord").fetchall()
    con.close()
    vp = speakers._read(proc.DATA / "voiceprints", "Noah")
    out["voiceprint"] = (vp.legacy, vp.legacy_sum.tobytes(), vp.ids, vp.embs.tobytes()) if vp else None
    vault = proc.NOTES_DIR.parent
    out["files"] = {str(p.relative_to(proc.DATA.parent)): p.read_bytes()
                    for root in (vault, proc.DATA / "clips") for p in sorted(root.rglob("*")) if p.is_file()}
    return out


def delete(proc, start=A):
    return trash.delete(proc.DATA, proc.NOTES_DIR, DEV, start)


def restore(proc, start=A):
    return trash.restore(proc.DATA, proc.NOTES_DIR, DEV, start)


def heading(start_hhmm):
    return f"## {start_hhmm} — Hello There (0m03s) · DEVA"


def test_delete_moves_everything_to_trash(two):
    raw_before = daily_for(two).read_text()
    assert heading("10:00") in raw_before
    out = delete(two)
    assert out["deleted"] is True
    raw = daily_for(two).read_text()
    assert heading("10:00") not in raw and heading("10:20") in raw
    assert raw == raw_before.replace(raw_before[raw_before.index(heading("10:00")):raw_before.index(heading("10:20"))], "")
    con = two.db()
    assert con.execute("SELECT start_ms FROM conversations").fetchall() == [(B,)]
    assert con.execute("SELECT COUNT(*) FROM speaker_clusters WHERE start_ms=?", (A,)).fetchone() == (0,)
    assert con.execute("SELECT COUNT(*) FROM chunks WHERE ts_ms < ?", (B,)).fetchone() == (0,)
    assert con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] > 0
    for t in ("stars", "paragraph_samples", "task_requests", "strangers", "voice_offers"):
        assert con.execute(f"SELECT COUNT(*) FROM {t}").fetchone() == (0,), t
    con.close()
    vp = speakers._read(two.DATA / "voiceprints", "Noah")
    cb = state(two)["speaker_clusters"][0][0]
    assert vp.ids == [cb] and vp.legacy == 1
    assert not list((two.NOTES_DIR.parent / "Events").iterdir())
    assert [p.name for p in (two.DATA / "clips").iterdir()] == ["DEVA_20260105_102000.opus"]
    hl = (two.NOTES_DIR.parent / "Highlights" / "2026-01-05 Highlights.md").read_text()
    assert "10:00" not in hl and "*No starred moments.*" in hl
    d = two.DATA / "trash" / f"{DEV}-{A}"
    m = json.loads((d / "manifest.json").read_text())
    assert m["section"]["text"].startswith(heading("10:00")) and m["section"]["next"] == heading("10:20")[3:]
    assert len(m["voiceprints"]) == 2 and m["files"]["clip"] == "DEVA_20260105_100000.opus"
    row = m["rows"]["conversations"][0]
    assert (row["kind"], row["importance"], json.loads(row["kind_reasons"])) == (
        "media", 0, ["no wearer voice", "few words"])
    assert (d / "clip" / m["files"]["clip"]).exists() and (d / "event" / m["files"]["event"]).exists()
    listed = trash.list_trash(two.DATA)
    assert [(t["device_id"], t["start_ms"], t["title"]) for t in listed] == [(DEV, A, "Hello There")]
    assert listed[0]["purge_at"] - listed[0]["deleted_at"] == 7 * 86400
    assert [c["start_ms"] for c in feed.day_payload(BASE.date())["conversations"]] == [B]


def test_delete_then_restore_round_trips_exactly(two):
    before = state(two)
    delete(two)
    deleted = state(two)
    assert deleted != before
    assert delete(two)["deleted"] is True and state(two) == deleted, "deleting twice changes nothing"
    assert restore(two) == {"id": f"{DEV}-{A}", "restored": True}
    assert state(two) == before
    con = two.db()
    assert con.execute("SELECT kind, importance, kind_reasons FROM conversations WHERE start_ms=?", (A,)).fetchone() \
        == ("media", 0, json.dumps(["no wearer voice", "few words"]))
    con.close()
    assert restore(two)["restored"] is True and state(two) == before, "restoring twice changes nothing"
    assert not (two.DATA / "trash" / f"{DEV}-{A}").exists()
    assert trash.list_trash(two.DATA) == []


def test_restore_puts_the_section_back_in_time_order_after_later_appends(two, fake_whisper):
    delete(two)
    restore_b = daily_for(two).read_text()
    delete(two, B)           # now neither A nor its next section is there
    write_segment(two.SPOOL, "DEVA_20260105T160000Z.pmsg", pages(DEV, at(40), 3, first_index=10))
    run(two)
    restore(two)              # A: before the 10:40 section (B is gone)
    text = daily_for(two).read_text()
    assert text.index(heading("10:00")) < text.index(heading("10:40"))
    restore(two, B)           # B: right after A, where it was
    text = daily_for(two).read_text()
    assert text.index(heading("10:00")) < text.index(heading("10:20")) < text.index(heading("10:40"))
    assert restore_b != text


def test_reused_cluster_ids_are_remapped_on_restore(two, fake_whisper):
    con = two.db()
    top = con.execute("SELECT MAX(id) FROM speaker_clusters").fetchone()[0]
    con.close()
    delete(two, B)            # B holds the highest cluster id
    write_segment(two.SPOOL, "DEVA_20260105T160000Z.pmsg", pages(DEV, at(40), 3, first_index=10))
    run(two)                  # the new conversation's cluster reuses that id
    con = two.db()
    assert con.execute("SELECT start_ms FROM speaker_clusters WHERE id=?", (top,)).fetchone() == (at(40),)
    con.close()
    restore(two, B)
    con = two.db()
    new_id = con.execute("SELECT id FROM speaker_clusters WHERE start_ms=?", (B,)).fetchone()[0]
    con.close()
    assert new_id != top
    assert new_id in speakers.members(two.DATA / "voiceprints", "Noah")


def test_pages_arriving_again_never_recreate_a_deleted_recording(two):
    delete(two)
    after = state(two)
    # a USB backup drains the same pages (and the press) again, and a copy whose page clock
    # was invalid (the segment's own time, far from the recording) arrives too
    write_segment(two.SPOOL, "DEVA_20260106T020000Z_usb.pmsg", a_pages())
    bad_clock = [make_msg(page_index=k, ts_ms=5, packets=[opus(DEV, k, j) for j in range(50)]) for k in range(3)]
    write_segment(two.SPOOL, "DEVA_20260106T030000Z.pmsg", bad_clock)
    con, n = run(two)
    con.close()
    assert n == 0
    assert state(two) == after


def test_purge_after_seven_days_keeps_tombstones(two):
    delete(two)
    con = two.db()
    assert trash.purge(con, two.DATA, two.NOTES_DIR, now=time.time() + 6 * 86400) == []
    assert trash.purge(con, two.DATA, two.NOTES_DIR, now=time.time() + 8 * 86400) == [f"{DEV}-{A}"]
    assert con.execute("SELECT COUNT(*) FROM trash").fetchone() == (0,)
    assert con.execute("SELECT COUNT(*) FROM tombstones").fetchone()[0] > 0
    con.close()
    assert not any((two.DATA / "trash").iterdir())
    assert trash.list_trash(two.DATA) == []
    with pytest.raises(trash.TrashError) as e:
        restore(two)
    assert e.value.code == 404
    after = state(two)
    write_segment(two.SPOOL, "DEVA_20260110T020000Z_usb.pmsg", a_pages())
    con, n = run(two)
    con.close()
    assert n == 0 and state(two) == after


def test_processor_run_purges_old_trash(two, monkeypatch):
    delete(two)
    con = two.db()
    con.execute("UPDATE trash SET deleted_at = deleted_at - 8 * 86400")
    con.commit()
    con.close()
    monkeypatch.setattr(two, "transcribe_ready", lambda con: 0)
    two.main()
    assert trash.list_trash(two.DATA) == []


@pytest.mark.parametrize("step", ["rows", "voiceprints", "section"])
def test_a_failure_mid_delete_changes_nothing(two, monkeypatch, step):
    before = state(two)

    def boom(s):
        if s == step:
            raise RuntimeError(s)
    monkeypatch.setattr(trash, "_checkpoint", boom)
    with pytest.raises(RuntimeError):
        delete(two)
    assert state(two) == before
    assert not any((two.DATA / "trash").iterdir())


@pytest.mark.parametrize("step", ["rows", "voiceprints", "section"])
def test_a_failure_mid_restore_leaves_it_deleted(two, monkeypatch, step):
    delete(two)
    deleted = state(two)

    def boom(s):
        if s == step:
            raise RuntimeError(s)
    monkeypatch.setattr(trash, "_checkpoint", boom)
    with pytest.raises(RuntimeError):
        restore(two)
    assert state(two) == deleted
    assert (two.DATA / "trash" / f"{DEV}-{A}" / "manifest.json").exists()
    monkeypatch.setattr(trash, "_checkpoint", lambda s: None)
    before_restore = state(two)
    restore(two)
    assert state(two) != before_restore


def test_a_crash_before_the_commit_is_undone_by_recover(two, monkeypatch):
    before = state(two)

    def crash(s):
        if s == "section":
            raise KeyboardInterrupt
    monkeypatch.setattr(trash, "_checkpoint", crash)
    monkeypatch.setattr(trash, "_undo_delete", lambda *a: None)   # killed: nothing cleans up
    with pytest.raises(KeyboardInterrupt):
        delete(two)
    assert heading("10:00") not in daily_for(two).read_text()   # half done
    monkeypatch.setattr(trash, "_checkpoint", lambda s: None)
    con = two.db()
    assert trash.recover(con, two.DATA, two.NOTES_DIR) == [f"undid deleting {DEV}-{A}"]
    con.close()
    assert state(two) == before


def test_a_crash_after_the_commit_is_finished_by_recover(two, monkeypatch):
    delete(two)
    deleted = state(two)
    restore(two)

    def crash(s):
        if s == "committed":
            raise KeyboardInterrupt
    monkeypatch.setattr(trash, "_checkpoint", crash)
    with pytest.raises(KeyboardInterrupt):
        delete(two)
    monkeypatch.setattr(trash, "_checkpoint", lambda s: None)
    con = two.db()
    assert trash.recover(con, two.DATA, two.NOTES_DIR) == [f"finished deleting {DEV}-{A}"]
    con.close()
    assert state(two) == deleted


def test_a_crash_mid_restore_is_undone_by_recover(two, monkeypatch):
    delete(two)
    deleted = state(two)

    def crash(s):
        if s == "section":
            raise KeyboardInterrupt
    monkeypatch.setattr(trash, "_checkpoint", crash)
    monkeypatch.setattr(trash, "_undo_restore", lambda *a: None)
    with pytest.raises(KeyboardInterrupt):
        restore(two)
    monkeypatch.setattr(trash, "_checkpoint", lambda s: None)
    con = two.db()
    assert trash.recover(con, two.DATA, two.NOTES_DIR) == [f"undid restoring {DEV}-{A}"]
    con.close()
    assert state(two) == deleted


def test_two_sections_with_one_heading_are_refused(two):
    text = daily_for(two).read_text()
    section = text[text.index(heading("10:00")):text.index(heading("10:20"))]
    daily_for(two).write_text(text + section)
    before = state(two)
    with pytest.raises(trash.TrashError) as e:
        delete(two)
    assert e.value.code == 409 and state(two) == before


def test_cut_and_insert_section_are_inverse():
    text = "---\nx\n---\n# Day\n\n## 10:00 — A\n\n[10:00] a\n\n---\n\n## 10:20 — B\n\n[10:20] b\n\n---\n\n"
    for h in ("10:00 — A", "10:20 — B"):
        rest, section = trash.cut_section(text, h)
        assert trash.insert_section(rest, section) == text
        assert trash.insert_section(text, section) == text   # already there
    assert trash.cut_section(text, "nope") is None


# ---------------------------------------------------------------------------
# the app star

def test_app_star_sets_and_clears_a_notable_override(two):
    overrides = two.DATA / "notable_overrides.json"
    events = two.NOTES_DIR.parent / "Events"
    assert feed.set_star(DEV, B, True) == {"app_star": True, "notable": True, "stars": 0}
    assert json.loads(overrides.read_text()) == {f"{DEV}-{B}": True}
    assert len(list(events.iterdir())) == 2
    day = feed.day_payload(BASE.date())["conversations"]
    assert [(c["start_ms"], c["app_star"], c["notable"]) for c in day] == [(A, False, True), (B, True, True)]
    hl = (two.NOTES_DIR.parent / "Highlights" / "2026-01-05 Highlights.md").read_text()
    assert f"DEVA-{B}]]" in hl
    assert feed.set_star(DEV, B, False) == {"app_star": False, "notable": False, "stars": 0}
    assert json.loads(overrides.read_text()) == {}
    assert len(list(events.iterdir())) == 1
    # a button-press star can't be taken off from the app
    assert feed.set_star(DEV, A, False) == {"app_star": False, "notable": True, "stars": 1}
    # an explicit false override is left alone by un-starring
    overrides.write_text(json.dumps({f"{DEV}-{A}": False}))
    assert feed.set_star(DEV, A, False)["notable"] is False
    assert json.loads(overrides.read_text()) == {f"{DEV}-{A}": False}


# ---------------------------------------------------------------------------
# routes

@pytest.fixture
def server(two):
    srv = feed.make_server(0, "tok")
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/pendant/feed"
    srv.shutdown()


def call(url, body=None):
    req = urllib.request.Request(url, data=None if body is None else json.dumps(body).encode(),
                                 headers={"Authorization": "Bearer tok"}, method="GET" if body is None else "POST")
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_routes(server, two):
    base = f"{server}/conversations/{DEV}/{B}"
    assert call(f"{base}/star", {"on": True})[1]["app_star"] is True
    assert call(f"{base}/star", {})[0] == 400
    assert call(f"{server}/conversations/{DEV}/123/delete", {})[0] == 404
    code, out = call(f"{base}/delete", {})
    assert code == 200 and out["deleted"] is True
    code, out = call(f"{server}/trash")
    assert code == 200 and [t["start_ms"] for t in out["trash"]] == [B]
    assert call(f"{base}/restore", {}) == (200, {"id": f"{DEV}-{B}", "restored": True})
    assert call(f"{server}/trash") == (200, {"trash": []})


def test_busy_processor_answers_503(server, two, monkeypatch):
    import fcntl
    monkeypatch.setattr(trash, "LOCK_WAIT_S", 0.3)
    before = state(two)
    with open(two.DATA / ".process.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)   # a processor run in progress
        code, out = call(f"{server}/conversations/{DEV}/{A}/delete", {})
        fcntl.flock(lock, fcntl.LOCK_UN)
    assert code == 503 and state(two) == before


# ---------------------------------------------------------------------------
# review round 1

def test_restore_refuses_when_a_newer_file_took_the_clip_path(two):
    delete(two)
    deleted = state(two)
    clip = two.DATA / "clips" / "DEVA_20260105_100000.opus"
    clip.write_bytes(b"a newer recording")
    with pytest.raises(trash.TrashError) as e:
        restore(two)
    assert e.value.code == 409
    assert clip.read_bytes() == b"a newer recording"
    assert (two.DATA / "trash" / f"{DEV}-{A}" / "clip" / clip.name).exists()
    assert {k: v for k, v in state(two).items() if k != "files"} == {k: v for k, v in deleted.items() if k != "files"}
    clip.unlink()
    assert state(two) == deleted


def test_restore_refuses_when_the_event_note_path_is_taken(two):
    delete(two)
    m = json.loads((two.DATA / "trash" / f"{DEV}-{A}" / "manifest.json").read_text())
    ev = two.NOTES_DIR.parent / "Events" / m["files"]["event"]
    ev.write_text("someone else's note")
    with pytest.raises(trash.TrashError) as e:
        restore(two)
    assert e.value.code == 409 and ev.read_text() == "someone else's note"
    assert trash.list_trash(two.DATA)[0]["start_ms"] == A


def test_restore_proceeds_when_the_file_already_there_is_identical(two):
    before = state(two)
    delete(two)
    d = two.DATA / "trash" / f"{DEV}-{A}"
    clip = two.DATA / "clips" / "DEVA_20260105_100000.opus"
    clip.write_bytes((d / "clip" / clip.name).read_bytes())
    restore(two)
    assert state(two) == before and not d.exists()


@pytest.mark.parametrize("step", ["committed", "files", "highlights", "finished"])
def test_a_crash_while_finishing_a_delete_is_finished_by_recover(two, monkeypatch, step):
    delete(two)
    deleted = state(two)
    restore(two)

    def crash(s):
        if s == step:
            raise KeyboardInterrupt
    monkeypatch.setattr(trash, "_checkpoint", crash)
    with pytest.raises(KeyboardInterrupt):
        delete(two)
    assert trash.unrecovered(two.DATA)
    monkeypatch.setattr(trash, "_checkpoint", lambda s: None)
    con = two.db()
    assert trash.recover(con, two.DATA, two.NOTES_DIR) == [f"finished deleting {DEV}-{A}"]
    con.close()
    assert state(two) == deleted and trash.unrecovered(two.DATA) == []
    m = json.loads((two.DATA / "trash" / f"{DEV}-{A}" / "manifest.json").read_text())
    assert m["state"] == "finished"
    restore(two)


def test_a_final_entry_left_unfinished_is_finished_by_recover(two):
    """An entry already at its final name but never finished (e.g. written by the
    first version, which renamed before cleaning up) is completed too."""
    delete(two)
    deleted = state(two)
    restore(two)
    delete(two)
    d = two.DATA / "trash" / f"{DEV}-{A}"
    m = json.loads((d / "manifest.json").read_text())
    for sub in ("clip", "event"):          # put the live files back as if never removed
        for f in (d / sub).iterdir():
            dest = (two.DATA / "clips" if sub == "clip" else two.NOTES_DIR.parent / "Events") / f.name
            dest.write_bytes(f.read_bytes())
    m.pop("state")
    (d / "manifest.json").write_text(json.dumps(m))
    assert trash.unrecovered(two.DATA) == [f"{DEV}-{A}"]
    con = two.db()
    trash.recover(con, two.DATA, two.NOTES_DIR)
    con.close()
    assert state(two) == deleted and trash.unrecovered(two.DATA) == []


def test_an_identical_packet_at_a_later_time_is_kept(two):
    """Page numbers restart after a wipe: the same page/ord/silence packet a month later
    is new audio, not the deleted recording."""
    delete(two)
    later = A + 30 * 86400 * 1000
    write_segment(two.SPOOL, "DEVA_20260204T150000Z.pmsg", pages(DEV, later, 3))
    con, n = run(two)
    assert n == 1
    assert con.execute("SELECT COUNT(*) FROM chunks WHERE ts_ms >= ?", (later,)).fetchone()[0] == 150
    con.close()


def test_an_upload_timed_copy_is_dropped_only_near_the_deletion(two):
    delete(two)
    bad_clock = [make_msg(page_index=k, ts_ms=5, packets=[opus(DEV, k, j) for j in range(50)]) for k in range(3)]
    write_segment(two.SPOOL, "DEVA_20260107T030000Z.pmsg", bad_clock)      # 2 days later: the same pages
    write_segment(two.SPOOL, "DEVA_20260301T030000Z.pmsg", bad_clock)      # 2 months later: kept
    con = two.db()
    two.ingest(con)
    march = int(dt_utc(2026, 3, 1, 3) * 1000)
    assert con.execute("SELECT COUNT(*) FROM chunks WHERE ts_ms < ?", (B,)).fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM chunks WHERE ts_ms = ?", (march,)).fetchone()[0] == 150
    con.close()


def dt_utc(*a):
    import datetime as dt
    return dt.datetime(*a, tzinfo=dt.timezone.utc).timestamp()


def test_processor_skips_its_run_when_recovery_fails(two, monkeypatch):
    calls = []
    monkeypatch.setattr(two, "ingest", lambda con: calls.append("ingest"))
    monkeypatch.setattr(two, "transcribe_ready", lambda con: calls.append("transcribe") or 0)

    def broken(*a):
        raise OSError("disk")
    monkeypatch.setattr(trash, "recover", broken)
    two.main()
    assert calls == []


def test_unresolved_trash_work_blocks_the_processor_and_the_endpoints(two, monkeypatch):
    calls = []
    monkeypatch.setattr(two, "ingest", lambda con: calls.append("ingest"))
    monkeypatch.setattr(two, "transcribe_ready", lambda con: calls.append("transcribe") or 0)
    stuck = two.DATA / "trash" / f".{DEV}-{B}.restoring"
    stuck.mkdir(parents=True)                       # no readable manifest: recover can't resolve it
    before = state(two)
    two.main()
    assert calls == []
    for fn in (lambda: delete(two), lambda: restore(two), lambda: feed.set_star(DEV, A, True)):
        with pytest.raises((trash.TrashError, feed.HTTPError)) as e:
            fn()
        assert e.value.code == 503
    assert state(two) == before
    stuck.rmdir()
    two.main()
    assert calls == ["ingest", "transcribe"]


def test_an_owned_entry_with_an_unreadable_manifest_stays_put_and_blocks(two, monkeypatch):
    calls = []
    monkeypatch.setattr(two, "ingest", lambda con: calls.append("ingest"))
    monkeypatch.setattr(two, "transcribe_ready", lambda con: calls.append("transcribe") or 0)
    delete(two)
    d = two.DATA / "trash" / f"{DEV}-{A}"
    good = (d / "manifest.json").read_text()
    (d / "manifest.json").write_text("{not json")
    before = state(two)
    two.main()
    assert calls == [] and d.exists(), "never moved aside while a trash row owns it"
    assert trash.unrecovered(two.DATA) == [f"{DEV}-{A}"]
    for fn in (lambda: restore(two), lambda: delete(two, B)):
        with pytest.raises(trash.TrashError) as e:
            fn()
        assert e.value.code == 503
    assert state(two) == before and d.exists()
    (d / "manifest.json").write_text(good)   # repaired by hand: everything works again
    two.main()
    assert calls == ["ingest", "transcribe"]
    restore(two)


def test_an_unowned_entry_is_set_aside(two):
    d = two.DATA / "trash" / f"{DEV}-123"
    d.mkdir(parents=True)
    (d / "manifest.json").write_text("{not json")
    con = two.db()
    assert trash.recover(con, two.DATA, two.NOTES_DIR) == [f"set aside {DEV}-123"]
    con.close()
    assert not d.exists() and trash.unrecovered(two.DATA) == []
    assert len(list((two.DATA / "trash").glob(f".{DEV}-123.stale-*"))) == 1
