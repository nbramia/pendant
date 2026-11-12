"""Undoing a reassignment (pendant_direct.reassign_undo via feed.reassign_recorded):
every scope puts the note, the database rows, the voiceprints, the voice library and
config.json back exactly; undoing twice is a no-op; a later edit of the conversation
refuses with 409 and changes nothing; a voiceprint changed by something else since
loses only this change's sample; a failed undo leaves no partial restore. Fake 4-d
embeddings."""
from __future__ import annotations

import fcntl
import hashlib
import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from test_reassign import OFFSETS, ra  # noqa: F401
from test_reassign_all import SIM, ba, populate  # noqa: F401
from test_voices import DEV, env, unit  # noqa: F401

from pendant_direct import feed, process, reassign_undo, speakers, trash, voices

SKIP_TABLES = {"reassign_undo", "reassign_undo_sections"}   # the undo record's own bookkeeping


def state(e) -> dict:
    """Everything a reassignment can touch, byte for byte."""
    tables = [r[0] for r in e.con.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]
    rows = {t: sorted(map(repr, e.con.execute(f"SELECT * FROM {t}").fetchall())) for t in tables
            if t not in SKIP_TABLES}
    files = {}
    for p in sorted(e.data.rglob("*")):
        rel = p.relative_to(e.data)
        if p.is_file() and rel.parts[0] != reassign_undo.DIR and not p.name.endswith(".lock") \
                and p.name not in ("pages.db", "pages.db-journal", ".lock"):
            files[str(rel)] = hashlib.sha256(p.read_bytes()).hexdigest()
    notes = {p.name: p.read_bytes() for p in sorted(e.notes.glob("*.md"))}
    dirs = sorted({str(Path(f).parent) for f in files})   # (lock-only folders aside)
    return {"rows": rows, "files": files, "notes": notes, "dirs": dirs}


def recorded(device, start_ms, body):
    return feed.reassign_recorded(device, start_ms, {"source": "user", **body})


def undo(cid, **kw):
    return feed.undo_reassign(cid, **kw) if kw else feed.undo_reassign(cid)


def plant_library(e, name: str, cid: int) -> None:
    """A voice library clip of cluster `cid` filed under `name`."""
    d = e.data / "voice_library" / name
    d.mkdir(parents=True)
    entry = {"id": f"cluster-{cid}", "src": f"cluster:{cid}", "kind": "hand", "rank": 1, "device": DEV,
             "start_ms": 1000, "day": "2026-11-08", "start_s": 0.5, "end_s": 6.0, "added_at": 1.0}
    (d / f"cluster-{cid}.opus").write_bytes(b"OggS fake clip")
    (d / "index.json").write_text(json.dumps([entry], indent=1) + "\n")


def test_conversation_scope_undo_restores_everything_exactly(ra):
    ra.enroll("Alex", unit([0, 1, 0, 0]), cid=ra.a)               # its sample sits in Alex's voiceprint
    ra.con.execute("UPDATE speaker_clusters SET label = 'Alex', enrolled_in = 'Alex', labeled_by = 'match' "
                   "WHERE id = ?", (ra.a,))
    ra.con.commit()
    ra.enroll("Noah", unit([1, 0, 0, 0]), cid=900)
    plant_library(ra, "Alex", ra.a)
    s0 = state(ra)
    out = recorded(DEV, 1000, {"scope": "conversation", "tag": "Speaker B", "name": "Noah"})
    assert out["scope"] == "conversation" and len(out["change_id"]) == 32 and out["undo_until"] > 0
    assert "**Noah:** sure thing" in ra.daily()
    assert speakers.members(ra.vdir, "Noah") == [900, ra.a] and not (ra.data / "voice_library" / "Alex").exists()
    assert state(ra) != s0
    res = undo(out["change_id"])
    assert res == {"change_id": out["change_id"], "undone": True, "already": False, "scope": "conversation",
                   "name": "Noah"}
    assert state(ra) == s0
    # twice: a no-op
    again = undo(out["change_id"])
    assert again["already"] is True and state(ra) == s0


def test_conversation_scope_someone_undo(ra):
    s0 = state(ra)
    out = recorded(DEV, 1000, {"scope": "conversation", "tag": "Speaker B", "name": "Someone"})
    assert ra.con.execute("SELECT COUNT(*) FROM strangers").fetchone()[0] == 1
    undo(out["change_id"])
    assert state(ra) == s0


def test_new_name_and_link_are_taken_back(ra, monkeypatch):
    monkeypatch.setattr(feed, "check_link", lambda *a: None)
    s0 = state(ra)
    out = recorded(DEV, 1000, {"scope": "conversation", "tag": "Speaker B", "name": "Jordan Smith",
                               "entity_id": "e-1"})
    cfg = json.loads((ra.data / "config.json").read_text())["speakers"]
    assert "Jordan Smith" in cfg["allowed"] and cfg["people"] == {"Jordan Smith": "e-1"}
    undo(out["change_id"])
    assert state(ra) == s0


def test_line_scope_undo_restores_exactly(ra):
    ra.line(0, "Alex")                                             # an earlier, unrelated line change
    ra.enroll("Noah", unit([1, 0, 0, 0]), cid=900)
    s0 = state(ra)
    out = recorded(DEV, 1000, {"scope": "line", "paragraph": 1, "name": "Noah"})
    assert out["sample"] == "added" and "[10:01] **Noah:** sure thing" in ra.daily()
    undo(out["change_id"])
    assert state(ra) == s0
    # a line moved from one person to another, then back
    out = recorded(DEV, 1000, {"scope": "line", "paragraph": 0, "name": "Someone"})
    assert "Alex" not in speakers.load_voiceprints(ra.vdir)
    undo(out["change_id"])
    assert state(ra) == s0


def test_all_scope_undo_restores_exactly(ba):
    populate(ba)
    ba.enroll("Alex", unit([0.1, 1, 0, 0]), cid=ba.con.execute(
        "SELECT id FROM speaker_clusters WHERE start_ms = 2000").fetchone()[0])
    ba.con.execute("UPDATE speaker_clusters SET enrolled_in = 'Alex' WHERE start_ms = 2000")
    ba.con.commit()
    s0 = state(ba)
    p = ba.preview("Noah")
    assert p["ids"]
    out = recorded(DEV, 1000, {"scope": "all", "tag": "Speaker B", "name": "Noah", "ids": p["ids"],
                               "digest": p["digest"]})
    assert out["voices"] == len(p["ids"]) and ba.con.execute("SELECT COUNT(*) FROM bulk_relabels").fetchone()[0]
    assert state(ba) != s0
    undo(out["change_id"])
    assert state(ba) == s0
    assert ba.con.execute("SELECT COUNT(*) FROM bulk_relabels").fetchone()[0] == 0


def test_later_edit_of_the_conversation_refuses_and_changes_nothing(ra):
    s0 = state(ra)
    conv = recorded(DEV, 1000, {"scope": "conversation", "tag": "Speaker B", "name": "Noah"})
    line = recorded(DEV, 1000, {"scope": "line", "paragraph": 0, "name": "Noah"})
    s2 = state(ra)
    with pytest.raises(feed.HTTPError) as e:
        undo(conv["change_id"])
    assert e.value.code == 409 and state(ra) == s2
    undo(line["change_id"])
    undo(conv["change_id"])
    assert state(ra) == s0


def test_hand_edited_section_refuses(ra):
    out = recorded(DEV, 1000, {"scope": "conversation", "tag": "Speaker B", "name": "Noah"})
    p = ra.notes / "2026-11-08.md"
    p.write_text(p.read_text().replace("sure thing", "sure thing!"))
    s1 = state(ra)
    with pytest.raises(feed.HTTPError) as e:
        undo(out["change_id"])
    assert e.value.code == 409 and state(ra) == s1


def test_other_sections_and_other_voiceprint_samples_survive(ra):
    ra.enroll("Noah", unit([1, 0, 0, 0]), cid=900)
    s0 = state(ra)
    out = recorded(DEV, 1000, {"scope": "conversation", "tag": "Speaker B", "name": "Noah"})
    # the processor writes elsewhere meanwhile: another sample, another section
    speakers.add(ra.vdir, "Noah", 901, unit([1, 0.2, 0, 0]))
    p = ra.notes / "2026-11-08.md"
    p.write_text(p.read_text() + "\n## 12:00 — Later (1m00s) · old\n\n[12:00] **Noah:** new words\n")
    undo(out["change_id"])
    assert speakers.members(ra.vdir, "Noah") == [900, 901]
    text = p.read_text()
    assert text.startswith(s0["notes"]["2026-11-08.md"].decode()) and text.endswith("new words\n")


def test_busy_processor_refuses_change_and_undo(ra, monkeypatch):
    monkeypatch.setattr(trash, "LOCK_WAIT_S", 0.3)
    s0 = state(ra)
    with open(ra.data / ".process.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with pytest.raises(feed.HTTPError) as e:
            recorded(DEV, 1000, {"scope": "conversation", "tag": "Speaker B", "name": "Noah"})
        assert e.value.code == 503 and state(ra) == s0
        assert ra.con.execute("SELECT COUNT(*) FROM reassign_undo").fetchone()[0] == 0
    out = recorded(DEV, 1000, {"scope": "conversation", "tag": "Speaker B", "name": "Noah"})
    assert out["relabeled"] and "**Noah:** sure thing" in ra.daily()  # applied at once, under the lock
    with open(ra.data / ".process.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with pytest.raises(feed.HTTPError) as e:
            undo(out["change_id"])
        assert e.value.code == 503
    undo(out["change_id"])
    assert state(ra) == s0


def test_processor_enrollment_during_a_change_is_not_undone(ra, monkeypatch):
    """The processor enrolls a sample while the change runs: the change holds the
    processor's lock, so the enrollment waits and the undo keeps it."""
    ra.enroll("Noah", unit([1, 0, 0, 0]), cid=900)
    real = reassign_undo.tracked_files
    proc = []

    def processor():
        with open(ra.data / ".process.lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            speakers.add(ra.vdir, "Noah", 901, unit([1, 0.2, 0, 0]))
            fcntl.flock(lock, fcntl.LOCK_UN)

    def snap(data):
        if not proc:                                   # the processor starts right after the first snapshot
            out = real(data)
            t = threading.Thread(target=processor)
            proc.append(t)
            t.start()
            t.join(0.5)
            return out
        return real(data)
    monkeypatch.setattr(reassign_undo, "tracked_files", snap)
    out = recorded(DEV, 1000, {"scope": "conversation", "tag": "Speaker B", "name": "Noah"})
    proc[0].join(5)
    assert 901 in speakers.members(ra.vdir, "Noah")
    undo(out["change_id"])
    assert speakers.members(ra.vdir, "Noah") == [900, 901]


def test_config_changed_since_loses_only_this_changes_entries(ra, monkeypatch):
    monkeypatch.setattr(feed, "check_link", lambda *a: None)
    cfg0 = json.loads((ra.data / "config.json").read_text())
    first = recorded(DEV, 1000, {"scope": "conversation", "tag": "Speaker B", "name": "Jordan Smith",
                                 "entity_id": "e-1"})
    recorded(DEV, 2000, {"scope": "line", "paragraph": 0, "name": "Kim Lee", "entity_id": "e-2"})
    undo(first["change_id"])
    sp = json.loads((ra.data / "config.json").read_text())["speakers"]
    assert sp["allowed"] == cfg0["speakers"]["allowed"] + ["Kim Lee"]
    assert sp["people"] == {"Kim Lee": "e-2"}


def test_merge_config_key_level():
    b = json.dumps({"speakers": {"allowed": ["A"], "people": {"A": "1"}, "x": 1}}).encode()
    a = json.dumps({"speakers": {"allowed": ["A", "J"], "people": {"A": "1", "J": "2"}, "x": 1}}).encode()
    c = json.dumps({"speakers": {"allowed": ["A", "J", "K"], "people": {"A": "1", "J": "2", "K": "3"}, "x": 2}})
    out = json.loads(reassign_undo.merge_config(b, a, c.encode()))
    assert out == {"speakers": {"allowed": ["A", "K"], "people": {"A": "1", "K": "3"}, "x": 2}}
    assert reassign_undo.merge_config(b, a, a) is b or reassign_undo.merge_config(b, a, a) == b


def test_deleted_voiceprint_is_never_rebuilt(ra):
    out = recorded(DEV, 1000, {"scope": "conversation", "tag": "Speaker B", "name": "Noah"})
    (ra.vdir / "Noah.npz").unlink()                  # Noah deleted that voiceprint since
    s1 = state(ra)
    with pytest.raises(feed.HTTPError) as e:
        undo(out["change_id"])
    assert e.value.code == 409 and state(ra) == s1 and not (ra.vdir / "Noah.npz").exists()


def _crashed_undo(ra):
    out = recorded(DEV, 1000, {"scope": "conversation", "tag": "Speaker B", "name": "Noah"})
    s1 = state(ra)
    daily = ra.notes / "2026-11-08.md"
    j = reassign_undo._journal(ra.data, out["change_id"])
    j.write_text(json.dumps({"files": {str(daily): {"pre": reassign_undo._b64(daily.read_bytes()),
                                                    "target": reassign_undo._b64(b"half written")}}}))
    daily.write_bytes(b"half written")
    return s1, j


def test_feed_start_recovers(ra, monkeypatch, tmp_path):
    s1, j = _crashed_undo(ra)
    tok = tmp_path / "token"
    tok.write_text("t")
    monkeypatch.setenv("PENDANT_TOKEN_FILE", str(tok))

    class Srv:
        def serve_forever(self):
            pass
    monkeypatch.setattr(feed, "make_server", lambda *a: Srv())
    feed.main()
    assert state(ra) == s1 and not j.exists()


def test_processor_start_recovers(ra, monkeypatch):
    s1, j = _crashed_undo(ra)
    monkeypatch.setattr(process, "DATA", ra.data)
    monkeypatch.setattr(process, "NOTES_DIR", ra.notes)
    for f in ("ingest", "transcribe_ready"):
        monkeypatch.setattr(process, f, lambda *a, **k: 0)
    process.main()
    assert state(ra) == s1 and not j.exists()


def test_failed_undo_is_not_partial_and_can_be_retried(ra, monkeypatch):
    ra.enroll("Noah", unit([1, 0, 0, 0]), cid=900)
    s0 = state(ra)
    out = recorded(DEV, 1000, {"scope": "conversation", "tag": "Speaker B", "name": "Noah"})
    s1 = state(ra)
    real = reassign_undo._put_files
    calls = []

    def flaky(plan):
        calls.append(1)
        if len(calls) == 1:
            items = list(plan.items())
            real(dict(items[:1]))                                     # one file written, then a failure
            raise OSError("disk full")
        real(plan)
    monkeypatch.setattr(reassign_undo, "_put_files", flaky)
    with pytest.raises(OSError):
        undo(out["change_id"])
    assert state(ra) == s1
    monkeypatch.setattr(reassign_undo, "_put_files", real)
    undo(out["change_id"])
    assert state(ra) == s0


def test_recover_follows_the_database_mark(ra):
    out = recorded(DEV, 1000, {"scope": "conversation", "tag": "Speaker B", "name": "Noah"})
    s1 = state(ra)
    daily = ra.notes / "2026-11-08.md"
    j = reassign_undo._journal(ra.data, out["change_id"])
    j.write_text(json.dumps({"files": {str(daily): {"pre": reassign_undo._b64(daily.read_bytes()),
                                                    "target": reassign_undo._b64(b"half written")}}}))
    daily.write_bytes(b"half written")                                # crashed before the commit
    reassign_undo.recover(ra.con, ra.data)
    assert state(ra) == s1 and not j.exists()


def test_too_old_and_unknown(ra):
    out = recorded(DEV, 1000, {"scope": "conversation", "tag": "Speaker B", "name": "Noah"})
    with pytest.raises(reassign_undo.UndoError) as e:
        reassign_undo.undo(ra.data, ra.notes, out["change_id"], now=out["undo_until"] + 1)
    assert e.value.code == 410
    with pytest.raises(feed.HTTPError) as e:
        undo("0" * 32)
    assert e.value.code == 404


def test_refused_change_leaves_no_record(ra):
    with pytest.raises(feed.HTTPError):
        recorded(DEV, 1000, {"scope": "conversation", "tag": "Nobody", "name": "Noah"})
    assert ra.con.execute("SELECT COUNT(*) FROM reassign_undo").fetchone()[0] == 0
    assert not list((ra.data / reassign_undo.DIR).glob("*")) if (ra.data / reassign_undo.DIR).exists() else True


def test_http_reassign_returns_change_id_and_undo_route(ra):
    import threading
    s0 = state(ra)
    srv = feed.make_server(0, "tok")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}/pendant/feed"

    def post(path, body):
        req = urllib.request.Request(base + path, data=json.dumps(body).encode(), method="POST",
                                     headers={"Authorization": "Bearer tok", "Content-Type": "application/json"})
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read())
    try:
        out = post(f"/conversations/{DEV}/1000/speaker", {"scope": "conversation", "tag": "Speaker B",
                                                          "name": "Noah", "source": "user"})
        assert len(out["change_id"]) == 32
        assert post(f"/reassign/{out['change_id']}/undo", {})["undone"] is True
        assert state(ra) == s0
        with pytest.raises(urllib.error.HTTPError) as e:
            post(f"/reassign/{'a' * 32}/undo", {})
        assert e.value.code == 404
    finally:
        srv.shutdown()


def test_merge_index_and_voiceprint_are_exact_when_nothing_else_changed():
    b = b'[\n {"id": "a"}\n]\n'
    a = b'[\n {"id": "a"},\n {"id": "b"}\n]\n'
    assert reassign_undo.merge_index(b, a, a) is b
    assert json.loads(reassign_undo.merge_index(b, a, b'[{"id": "c"}, {"id": "a"}, {"id": "b"}]')) == \
        [{"id": "c"}, {"id": "a"}]
    assert reassign_undo.merge_index(None, a, a) is None
