"""Remove name (feed.reassign_speaker with `remove: true` / `name: null`): both scopes make
the speaker unknown again (a fresh "Speaker X", the sample out of the old person's
voiceprint and voice library, kept as a stranger), the undo restores byte-identical
state, the processor lock and a failed undo cover it, scope all is refused, and older
"Someone" transcripts stay as they are. Fake 4-d embeddings."""
from __future__ import annotations

import fcntl
import json
import threading
import urllib.error
import urllib.request

import pytest
from test_reassign import OFFSETS, ra  # noqa: F401
from test_reassign_undo import plant_library, recorded, state, undo
from test_voices import DEV, HEADING, OTHER_HEADING, env, unit  # noqa: F401

from pendant_direct import feed, reassign_undo, speakers, trash, voices

FOUR = f"""# Sunday

## {HEADING}

[10:00] **Speaker A:** we should fix the door

[10:01] **Alex:** sure thing

[10:02] **Speaker B:** okay then

[10:03] **Speaker A:** right

---

## {OTHER_HEADING}

[11:00] **Alex:** pass the salt

---
"""


def write(e, text: str) -> None:
    (e.notes / "2026-11-08.md").write_text(text)


@pytest.fixture()
def rn(ra):
    """Conversation 1000: Alex's voice (named by hand, enrolled, with a library clip), an
    unnamed "Speaker A" voice, and a line already labelled "Speaker B" with no voice behind it."""
    write(ra, FOUR)
    ra.con.execute("UPDATE speaker_clusters SET tag = 'Alex', label = 'Alex', labeled_by = 'user', "
                   "enrolled_in = 'Alex', label_kind = 'person' WHERE id = ?", (ra.a,))
    ra.con.commit()
    ra.b = ra.add([0, 0, 1, 0], tag="Speaker A")
    ra.enroll("Alex", unit([0, 1, 0, 0]), cid=ra.a)
    ra.enroll("Alex", unit([0, 1, 0.2, 0]), cid=901)              # another Alex sample stays
    plant_library(ra, "Alex", ra.a)
    return ra


def strangers(e) -> list[int]:
    return [r[0] for r in e.con.execute("SELECT cluster_id FROM strangers ORDER BY cluster_id")]


def test_conversation_scope_makes_the_voice_unknown_and_undo_restores_it(rn):
    s0 = state(rn)
    out = recorded(DEV, 1000, {"scope": "conversation", "tag": "Alex", "remove": True})
    assert out["name"] == "Speaker C" and out["removed"] is True and out["relabeled"] is True
    assert len(out["change_id"]) == 32
    text = rn.daily()
    assert "[10:01] **Speaker C:** sure thing" in text
    assert "[10:00] **Speaker A:** we should fix the door" in text and "[10:02] **Speaker B:** okay then" in text
    assert "[11:00] **Alex:** pass the salt" in text                       # another conversation
    assert rn.con.execute("SELECT tag, label, labeled_by, label_kind, enrolled_in FROM speaker_clusters "
                          "WHERE id = ?", (rn.a,)).fetchone() == ("Speaker C", None, "user", "stranger", "")
    assert speakers.members(rn.vdir, "Alex") == [901]                      # its sample left the voiceprint
    assert not (rn.data / "voice_library" / "Alex").exists()               # and the voice library
    assert strangers(rn) == [rn.a]
    tags = [r[0] for r in rn.con.execute("SELECT tag FROM speaker_clusters WHERE device = ? AND start_ms = 1000",
                                         (DEV,))]
    assert len(tags) == len(set(tags))
    assert undo(out["change_id"])["undone"] is True
    assert state(rn) == s0


def test_conversation_scope_leaves_line_overrides_alone(rn):
    rn.line(1, "Noah")                                     # Alex's line reassigned one by one first
    write(rn, rn.daily().replace("[10:03] **Speaker A:** right", "[10:03] **Alex:** right"))
    out = feed.reassign_speaker(DEV, 1000, {"scope": "conversation", "tag": "Alex", "name": None})
    assert out["name"] == "Speaker C"
    text = rn.daily()
    assert "[10:01] **Noah:** sure thing" in text and "[10:03] **Speaker C:** right" in text
    assert rn.con.execute("SELECT holder FROM paragraph_samples").fetchall() == [("Noah",)]


def test_an_unnamed_voice_keeps_its_own_tag(rn):
    out = feed.reassign_speaker(DEV, 1000, {"scope": "conversation", "tag": "Speaker A", "remove": True})
    assert out["name"] == "Speaker A" and out["relabeled"] is False
    assert rn.daily() == FOUR and strangers(rn) == [rn.b]


def test_line_scope_moves_the_sample_to_strangers_and_undo_restores_it(rn):
    rn.line(1, "Noah")
    d = rn.data / "voice_library" / "Noah"                 # the line's clip in Noah's library
    d.mkdir(parents=True)
    entry = {"id": "para-1", "src": f"para:{DEV}:1000:1", "kind": "hand", "rank": 1, "device": DEV,
             "start_ms": 1000, "day": "2026-11-08", "start_s": 1.5, "end_s": 3.0, "added_at": 1.0}
    (d / "para-1.opus").write_bytes(b"OggS fake clip")
    (d / "index.json").write_text(json.dumps([entry], indent=1) + "\n")
    key = rn.con.execute("SELECT key FROM paragraph_samples").fetchone()[0]
    s0 = state(rn)
    out = recorded(DEV, 1000, {"scope": "line", "paragraph": 1, "remove": True})
    assert out["name"] == "Speaker C" and out["removed"] is True and out["sample"] == "added"
    text = rn.daily()
    assert "[10:01] **Speaker C:** sure thing" in text and "[10:03] **Speaker A:** right" in text
    assert "Noah" not in speakers.load_voiceprints(rn.vdir)
    assert not d.exists()
    assert rn.con.execute("SELECT holder FROM paragraph_samples").fetchall() == [(voices.STRANGER_HOLDER,)]
    assert strangers(rn) == [key]
    assert speakers.members(rn.vdir, "Alex") == [rn.a, 901]                # the voice itself is untouched
    undo(out["change_id"])
    assert state(rn) == s0


def test_line_scope_twice_takes_another_fresh_tag(rn):
    first = feed.reassign_speaker(DEV, 1000, {"scope": "line", "paragraph": 0, "remove": True})
    second = feed.reassign_speaker(DEV, 1000, {"scope": "line", "paragraph": 3, "remove": True})
    assert (first["name"], second["name"]) == ("Speaker C", "Speaker D")
    text = rn.daily()
    assert "[10:00] **Speaker C:** we should" in text and "[10:03] **Speaker D:** right" in text


def test_line_scope_without_a_sample_changes_only_the_text(rn):
    out = recorded(DEV, 2000, {"scope": "line", "paragraph": 0, "remove": True})
    assert out["name"] == "Speaker A" and out["sample"] == "skipped"
    assert "[11:00] **Speaker A:** pass the salt" in rn.daily()
    assert strangers(rn) == [] and speakers.members(rn.vdir, "Alex") == [rn.a, 901]


def test_busy_processor_refuses_remove(rn, monkeypatch):
    monkeypatch.setattr(trash, "LOCK_WAIT_S", 0.3)
    s0 = state(rn)
    with open(rn.data / ".process.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        for body in ({"scope": "conversation", "tag": "Alex"}, {"scope": "line", "paragraph": 1}):
            with pytest.raises(feed.HTTPError) as e:
                recorded(DEV, 1000, {**body, "remove": True})
            assert e.value.code == 503
    assert state(rn) == s0


def test_failed_undo_of_a_remove_is_not_partial(rn, monkeypatch):
    s0 = state(rn)
    out = recorded(DEV, 1000, {"scope": "conversation", "tag": "Alex", "remove": True})
    s1 = state(rn)
    real = reassign_undo._put_files

    def flaky(plan):
        real(dict(list(plan.items())[:1]))
        raise OSError("disk full")
    monkeypatch.setattr(reassign_undo, "_put_files", flaky)
    with pytest.raises(OSError):
        undo(out["change_id"])
    assert state(rn) == s1
    monkeypatch.setattr(reassign_undo, "_put_files", real)
    undo(out["change_id"])
    assert state(rn) == s0


def test_scope_all_is_refused(rn):
    s0 = state(rn)
    for body in ({"remove": True}, {"name": None}):
        with pytest.raises(feed.HTTPError) as e:
            recorded(DEV, 1000, {"scope": "all", "tag": "Alex", "ids": [], "digest": "", **body})
        assert e.value.code == 400
    assert state(rn) == s0


def test_http_remove_and_undo(rn):
    s0 = state(rn)
    srv = feed.make_server(0, "tok")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}/pendant/feed"

    def post(path, body):
        req = urllib.request.Request(base + path, data=json.dumps(body).encode(), method="POST",
                                     headers={"Authorization": "Bearer tok", "Content-Type": "application/json"})
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read())
    try:
        out = post(f"/conversations/{DEV}/1000/speaker", {"scope": "line", "paragraph": 1, "name": None,
                                                          "tag": "Alex", "source": "user"})
        assert out["removed"] is True and out["name"] == "Speaker C" and len(out["change_id"]) == 32
        assert post(f"/reassign/{out['change_id']}/undo", {})["undone"] is True
        assert state(rn) == s0
        with pytest.raises(urllib.error.HTTPError) as e:
            post(f"/conversations/{DEV}/1000/speaker", {"scope": "all", "tag": "Alex", "remove": True})
        assert e.value.code == 400
        out = post(f"/speakers/{rn.b}/stranger", {})               # Voice Scoring / Who's this: Remove name
        assert out == {"id": rn.b, "stranger": True, "name": "Speaker A", "relabeled": False}
    finally:
        srv.shutdown()


def test_voice_scoring_remove_gives_a_named_voice_a_fresh_tag(rn):
    cid = rn.add([0.3, 0, 0, 1], tag="Noah", label="Noah", labeled_by="match", start_ms=2000)
    write(rn, FOUR.replace("[11:00] **Alex:** pass the salt", "[11:00] **Noah:** pass the salt"))
    out = feed.stranger_cluster(cid)
    assert out["name"] == "Speaker A" and out["relabeled"] is True
    assert "[11:00] **Speaker A:** pass the salt" in rn.daily()


def test_older_someone_transcripts_stay_and_still_read(rn):
    write(rn, FOUR.replace("[10:02] **Speaker B:** okay then", "[10:02] **Someone:** okay then"))
    rn.con.execute("INSERT INTO speaker_clusters (device, start_ms, tag, labeled_by, label_kind, embedding) "
                   "VALUES (?, 1000, 'Someone', 'user', 'stranger', ?)", (DEV, unit([0, 0, 0, 1]).tobytes()))
    rn.con.commit()
    out = feed.reassign_speaker(DEV, 1000, {"scope": "conversation", "tag": "Alex", "remove": True})
    assert out["name"] == "Speaker B"                       # "Speaker B" is free again; "Someone" is no tag to reuse
    text = rn.daily()
    assert "[10:02] **Someone:** okay then" in text and "[10:01] **Speaker B:** sure thing" in text
    day = feed.day_payload(__import__("datetime").date(2026, 11, 8))
    assert day["conversations"]
    # a "Someone" voice can still be named, or have its name removed
    out = feed.reassign_speaker(DEV, 1000, {"scope": "conversation", "tag": "Someone", "remove": True})
    assert out["name"] == "Speaker C" and "[10:02] **Speaker C:** okay then" in rn.daily()


# ---------------------------------------------------------------------------
# voices.fresh_tag

def test_fresh_tag_skips_every_tag_in_use(rn):
    letters = "\n".join(f"[10:00] **Speaker {chr(65 + i)}:** x" for i in range(26))
    assert voices.fresh_tag(rn.con, DEV, 1000, letters) == "Speaker AA"
    assert voices.fresh_tag(rn.con, DEV, 1000, None) == "Speaker B"          # Alex, Speaker A are voices
    rn.con.execute("INSERT INTO transcript_relabels (device, start_ms, old_tag, new_tag, created_at) "
                   "VALUES (?, 1000, 'X', 'Speaker B', 0)", (DEV,))           # queued, not yet applied
    assert voices.fresh_tag(rn.con, DEV, 1000, None) == "Speaker C"
    assert voices.fresh_tag(rn.con, DEV, 1000, None, own="Speaker A", cid=rn.b) == "Speaker A"
    rn.add([1, 0, 0, 0], tag="Speaker A")                                     # now shared: not kept
    assert voices.fresh_tag(rn.con, DEV, 1000, None, own="Speaker A", cid=rn.b) == "Speaker C"


# ---------------------------------------------------------------------------
# what a removal never touches

def test_remove_leaves_allowed_names_links_and_other_voiceprints_alone(rn):
    rn.enroll("Noah", unit([1, 0, 0, 0]), cid=900)
    cfg0 = (rn.data / "config.json").read_bytes()
    noah0 = (rn.vdir / "Noah.npz").read_bytes()
    for body in ({"scope": "conversation", "tag": "Alex"}, {"scope": "line", "paragraph": 0}):
        recorded(DEV, 1000, {**body, "remove": True, "name": "Someone", "entity_id": "e-9", "source": "user"})
    assert (rn.data / "config.json").read_bytes() == cfg0          # no allowed name, no LifeOS link
    assert (rn.vdir / "Noah.npz").read_bytes() == noah0
    assert speakers.members(rn.vdir, "Alex") == [901]            # only the removed voice's sample left


def test_voice_scoring_remove_of_a_shared_tag_keeps_row_and_note_agreeing(rn):
    other = rn.add([0, 0, 0, 1], tag="Alex", label="Alex", labeled_by="match")   # a second "Alex" here
    out = feed.stranger_cluster(other)
    assert out["name"] == "Alex" and out["relabeled"] is False
    assert rn.daily() == FOUR
    assert rn.con.execute("SELECT tag, label, label_kind FROM speaker_clusters WHERE id = ?",
                          (other,)).fetchone() == ("Alex", None, "stranger")
    assert strangers(rn) == [other]
    with pytest.raises(feed.HTTPError) as e:                       # the transcript path refuses, as for a name
        feed.reassign_speaker(DEV, 1000, {"scope": "conversation", "tag": "Alex", "remove": True})
    assert e.value.code == 409


def test_calendar_enrollment_never_takes_a_removed_voice(tmp_path, monkeypatch):
    import test_attendance as ta

    from pendant_direct import attendance, process
    monkeypatch.setattr(process, "DATA", tmp_path)
    monkeypatch.setattr(process, "DB", tmp_path / "pages.db")
    monkeypatch.setattr(process, "NOTES_DIR", tmp_path / "notes")
    monkeypatch.setattr(attendance, "events_on", lambda day, get=None: [attendance.Event(
        "Dana-Noah weekly", ta.ms(13, 0), ta.ms(14, 0), ["Dana Brooks", "noah@work.example"])])
    monkeypatch.setattr(attendance, "resolve_people", lambda a, get=None: ["Dana Brooks", "Noah Park"])
    monkeypatch.setattr(speakers, "enroll", lambda *a, **kw: pytest.fail("must not enroll a removed voice"))
    con = process.db()
    ta.add_conv(con, ta.ms(13, 5), ta.ms(13, 45), [("Noah", "Noah", 600), ("Speaker B", None, 500)])
    con.execute("UPDATE speaker_clusters SET labeled_by = 'user', label_kind = 'stranger' WHERE tag = 'Speaker B'")
    out = process.enroll_from_calendar(con, "DEVA", ta.ms(13, 5), ta.ms(13, 45), "2026-11-09", "13:05 — x", {}, {})
    assert out == "ambiguous (Dana-Noah weekly)"
    assert con.execute("SELECT tag, label FROM speaker_clusters WHERE tag = 'Speaker B'").fetchone() == ("Speaker B", None)
