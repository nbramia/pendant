"""Reassigning speakers from a conversation's transcript (feed.reassign_speaker):
both scopes, voiceprint arithmetic, idempotence, strangers, consent, missing
offsets, the processor lock, and the feed fields. Fake 4-d embeddings."""
from __future__ import annotations

import datetime as dt
import fcntl
import json
import threading
import urllib.error
import urllib.request

import numpy as np
import pytest
from test_voices import DAILY, DEV, E, HEADING, NOAH, OTHER, ALEX, env, unit  # noqa: F401

from pendant_direct import feed, process, speakers, voices

OFFSETS = [[0.0, 1.5], [1.5, 3.0]]


@pytest.fixture()
def ra(env, monkeypatch):
    """env + paragraph offsets for conversation 1000 and a fake span embedder."""
    env.con.execute("UPDATE conversations SET paragraph_offsets = ?, clip = ? WHERE start_ms = 1000",
                    (json.dumps(OFFSETS), str(env.clip)))
    env.con.commit()
    calls = []
    vecs = {0: unit([1, 0.1, 0, 0]), 1: unit([0.1, 1, 0, 0])}

    def fake(clip, s, e):
        calls.append((s, e))
        return vecs[0 if s < 1 else 1]
    monkeypatch.setattr(feed, "_embed_span", fake)
    env.calls, env.vecs = calls, vecs
    env.a = env.add([0, 1, 0, 0], tag="Speaker B")                 # the cluster behind paragraph 1
    env.vp = lambda: speakers.load_voiceprints(env.vdir)
    env.line = lambda i, name, **kw: feed.reassign_speaker(DEV, 1000, {"scope": "line", "paragraph": i,
                                                                       "name": name, "source": "user", **kw})
    return env


def test_line_scope_changes_one_paragraph_and_adds_its_sample(ra):
    out = ra.line(1, "Noah")
    assert out == {"scope": "line", "paragraph": 1, "name": "Noah", "applied": True, "sample": "added"}
    text = ra.daily()
    assert "[10:00] **Alex:** we should fix the door" in text
    assert "[10:01] **Noah:** sure thing" in text
    assert "[11:00] **Alex:** pass the salt" in text
    mean, n = ra.vp()["Noah"]
    assert n == 1 and np.allclose(mean, ra.vecs[1], atol=1e-6)
    assert ra.calls == [(1.5, 3.0)]
    assert "Noah" in json.loads((ra.data / "config.json").read_text())["speakers"]["allowed"]


def test_line_scope_is_idempotent_and_moves_the_sample(ra):
    ra.line(1, "Noah")
    again = ra.line(1, "Noah")
    assert again["sample"] == "unchanged"
    assert ra.vp()["Noah"][1] == 1 and len(ra.calls) == 1        # no second sample, no re-embedding
    text = ra.daily()
    ra.line(1, "Noah")
    assert ra.daily() == text
    moved = ra.line(1, "Alex")
    assert moved["sample"] == "added"
    vps = ra.vp()
    assert "Noah" not in vps and vps["Alex"][1] == 1
    assert "[10:01] **Alex:** sure thing" in ra.daily()
    assert len(ra.calls) == 1
    # two lines are two samples of the same person
    ra.line(0, "Alex")
    assert ra.vp()["Alex"][1] == 2
    assert np.allclose(ra.vp()["Alex"][0], (ra.vecs[0] + ra.vecs[1]) / 2, atol=1e-6)


def test_line_scope_old_someone_removes_the_name(ra):
    ra.line(1, "Noah")
    out = ra.line(1, "someone")                       # what older app versions send
    assert out["name"] == "Speaker A" and out["removed"] is True and out["sample"] == "added"
    assert "Noah" not in ra.vp()
    assert "[10:01] **Speaker A:** sure thing" in ra.daily()
    assert ra.con.execute("SELECT COUNT(*) FROM strangers").fetchone()[0] == 1
    assert ra.line(1, None)["sample"] == "unchanged"
    assert ra.con.execute("SELECT COUNT(*) FROM strangers").fetchone()[0] == 1
    # back to a person: the stranger record goes
    ra.line(1, "Alex")
    assert ra.con.execute("SELECT COUNT(*) FROM strangers").fetchone()[0] == 0
    assert ra.vp()["Alex"][1] == 1


def test_line_scope_without_offsets_updates_text_and_skips_the_sample(ra, capsys):
    ra.add([0, 0, 1, 0], tag="Speaker B", start_ms=2000)
    out = feed.reassign_speaker(DEV, 2000, {"scope": "line", "paragraph": 0, "name": "Alex", "source": "user"})
    assert out["sample"] == "skipped" and out["reason"] == "no offsets"
    assert "[11:00] **Alex:** pass the salt" in ra.daily()
    assert ra.vp() == {} and ra.calls == []
    assert "no clip offsets" in capsys.readouterr().out


def test_line_scope_with_unusable_audio_keeps_the_text_change(ra, monkeypatch):
    ra.con.execute("UPDATE conversations SET paragraph_offsets = '[[0.0, 0.4], [1.5, 3.0]]' WHERE start_ms = 1000")
    ra.con.commit()
    assert ra.line(0, "Noah")["reason"] == "no audio"
    monkeypatch.setattr(feed, "_embed_span", lambda *a: (_ for _ in ()).throw(RuntimeError("no model")))
    assert ra.line(1, "Noah")["reason"] == "embedding failed"
    text = ra.daily()
    assert "[10:00] **Noah:**" in text and "[10:01] **Noah:**" in text and ra.vp() == {}


def test_line_scope_consent_and_validation(ra):
    with pytest.raises(feed.HTTPError) as e:
        feed.reassign_speaker(DEV, 1000, {"scope": "line", "paragraph": 1, "name": "Stranger Danger"})
    assert e.value.code == 403                                     # unlisted name needs source user
    assert ra.daily() == DAILY and not ra.vdir.exists()
    for body, code in (({"scope": "line", "paragraph": 9, "name": "Alex"}, 404),
                       ({"scope": "line", "paragraph": -1, "name": "Alex"}, 400),
                       ({"scope": "line", "name": "Alex"}, 400),
                       ({"scope": "line", "paragraph": 0, "name": "Bad1"}, 400),
                       ({"scope": "nope", "name": "Alex"}, 400)):
        with pytest.raises(feed.HTTPError) as e:
            feed.reassign_speaker(DEV, 1000, {"source": "user", **body})
        assert e.value.code == code
    with pytest.raises(feed.HTTPError) as e:
        feed.reassign_speaker(DEV, 777, {"scope": "line", "paragraph": 0, "name": "Alex", "source": "user"})
    assert e.value.code == 404


def test_line_edit_waits_for_the_processor_lock(ra):
    with open(ra.data / ".process.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        out = ra.line(1, "Alex")
        assert out["applied"] is False and ra.daily() == DAILY     # queued, not written
    assert voices.apply_relabels(ra.con, ra.notes) == {"2026-11-08"}
    assert "[10:01] **Alex:** sure thing" in ra.daily()


def test_line_then_conversation_relabels_apply_in_order(ra):
    with open(ra.data / ".process.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        ra.line(0, "Noah")
        feed.reassign_speaker(DEV, 1000, {"scope": "conversation", "tag": "Speaker B", "name": "Alex",
                                          "source": "user"})
    voices.apply_relabels(ra.con, ra.notes)
    text = ra.daily()
    assert "[10:00] **Noah:** we should fix the door" in text
    assert "[10:01] **Alex:** sure thing" in text


def test_conversation_scope_relabels_the_cluster_like_voice_scoring(ra):
    out = feed.reassign_speaker(DEV, 1000, {"scope": "conversation", "tag": "Speaker B", "name": "Alex",
                                            "source": "user"})
    assert out == {"scope": "conversation", "cluster": ra.a, "name": "Alex", "relabeled": True}
    assert "[10:01] **Alex:** sure thing" in ra.daily()
    mean, n = ra.vp()["Alex"]
    assert n == 1 and np.allclose(mean, [0, 1, 0, 0], atol=1e-6)
    assert ra.con.execute("SELECT tag, label, labeled_by, enrolled_in FROM speaker_clusters WHERE id=?",
                          (ra.a,)).fetchone() == ("Alex", "Alex", "user", "Alex")


def test_conversation_scope_can_reassign_an_answered_cluster_and_remove_its_name(ra):
    feed.reassign_speaker(DEV, 1000, {"scope": "conversation", "tag": "Speaker B", "name": "Alex", "source": "user"})
    out = feed.reassign_speaker(DEV, 1000, {"scope": "conversation", "tag": "Alex", "name": "Noah",
                                            "source": "user"})
    assert out["relabeled"] is True
    vps = ra.vp()
    assert "Alex" not in vps and vps["Noah"][1] == 1           # the sample moved exactly
    out = feed.reassign_speaker(DEV, 1000, {"scope": "conversation", "tag": "Noah", "remove": True})
    assert out["name"] == "Speaker A" and out["removed"] is True
    assert ra.vp() == {}
    assert ra.con.execute("SELECT COUNT(*) FROM strangers WHERE cluster_id=?", (ra.a,)).fetchone()[0] == 1
    assert "[10:01] **Speaker A:** sure thing" in ra.daily()
    assert ra.con.execute("SELECT label, labeled_by, label_kind FROM speaker_clusters WHERE id=?",
                          (ra.a,)).fetchone() == (None, "user", "stranger")
    # and back to a person: the stranger record goes, voices like it are matchable again
    feed.reassign_speaker(DEV, 1000, {"scope": "conversation", "tag": "Speaker A", "name": "Alex", "source": "user"})
    assert ra.con.execute("SELECT COUNT(*) FROM strangers").fetchone()[0] == 0
    assert ra.vp()["Alex"][1] == 1


def test_conversation_scope_consent_and_ambiguity(ra):
    with pytest.raises(feed.HTTPError) as e:
        feed.reassign_speaker(DEV, 1000, {"scope": "conversation", "tag": "Speaker B", "name": "Nobody Known"})
    assert e.value.code == 403
    with pytest.raises(feed.HTTPError) as e:
        feed.reassign_speaker(DEV, 1000, {"scope": "conversation", "tag": "Ghost", "name": "Alex"})
    assert e.value.code == 404
    ra.add(OTHER, tag="Speaker B")                                 # two voices behind one tag
    with pytest.raises(feed.HTTPError) as e:
        feed.reassign_speaker(DEV, 1000, {"scope": "conversation", "tag": "Speaker B", "name": "Alex"})
    assert e.value.code == 409


def test_day_feed_has_speaker_tags_and_offsets(ra, monkeypatch):
    ra.add(OTHER, tag="Alex")
    ra.add(NOAH, tag="Speaker C")
    ra.add(ALEX, tag="Speaker C")                                # shared tag: no single cluster
    day = feed.day_payload(dt.date(2026, 11, 8))
    first, second = day["conversations"]
    assert first["speaker_tags"] == {"Speaker B": ra.a, "Alex": ra.a + 1}
    assert first["paragraph_offsets"] == OFFSETS
    assert second["speaker_tags"] == {} and second["paragraph_offsets"] is None


def test_http_endpoint(ra):
    srv = feed.make_server(0, "tok")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}/pendant/feed/conversations/{DEV}/1000/speaker"

        def post(body, token="tok"):
            req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                         headers={"Authorization": f"Bearer {token}"})
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        code, out = post({"tag": "Speaker B", "name": "Alex", "scope": "conversation", "source": "user"})
        assert code == 200 and out["cluster"] == ra.a
        with pytest.raises(urllib.error.HTTPError) as e:
            post({"scope": "line", "paragraph": 0, "name": "Alex"}, token="bad")
        assert e.value.code == 401
        with pytest.raises(urllib.error.HTTPError) as e:
            post({"scope": "line", "paragraph": 0, "name": "Zed Unlisted"})
        assert e.value.code == 403
    finally:
        srv.shutdown()


def test_paragraph_spans_follow_the_rendered_paragraphs():
    segs = [(0.0, 2.0, "hello there"), (2.5, 4.0, "how are you"), (10.0, 12.0, "after a pause")]
    bp = [(0.0, 1_700_000_000_000)]
    paras, spans = process.paragraphs_with_spans(segs, bp, [9.0], labels=["A", "A", "A"])
    assert [p[1] for p in paras] == ["**A:** hello there how are you", "**A:** after a pause"]
    assert spans == [[0.0, 4.0], [10.0, 12.0]]
    assert process.paragraphs(segs, bp, [9.0], labels=["A", "A", "A"]) == paras


def test_label_paragraph_and_sample_key():
    text = voices.label_paragraph(DAILY, HEADING, 1, "Noah")
    assert text.count("**Noah:**") == 1 and voices.label_paragraph(text, HEADING, 1, "Noah") == text
    starred = "## h\n\n[10:00] ⭐ **A:** hi\n\n[10:01] plain\n"
    assert voices.label_paragraph(starred, "h", 0, "B") == "## h\n\n[10:00] ⭐ **B:** hi\n\n[10:01] plain\n"
    assert voices.label_paragraph(starred, "h", 1, "B").endswith("[10:01] **B:** plain\n")
    assert voices.label_paragraph(starred, "h", 5, "B") == starred
    k = speakers.sample_key(f"para:{DEV}:1000:1")
    assert k < 0 and k == speakers.sample_key(f"para:{DEV}:1000:1") != speakers.sample_key(f"para:{DEV}:1000:2")


def test_concurrent_line_assignments_leave_one_holder(ra, monkeypatch):
    import time as _t
    real = feed._embed_span

    def slow(clip, s, e):
        _t.sleep(0.2)            # both requests are inside the read -> record window together
        return real(clip, s, e)
    monkeypatch.setattr(feed, "_embed_span", slow)
    errors = []

    def go(name):
        try:
            ra.line(1, name)
        except Exception as e:  # noqa: BLE001
            errors.append(e)
    threads = [threading.Thread(target=go, args=(n,)) for n in ("Noah", "Alex")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    holder = speakers.sample_key(f"para:{DEV}:1000:1")
    holders = [n for n in ("Noah", "Alex") if holder in speakers.members(ra.vdir, n)]
    assert len(holders) == 1
    assert ra.con.execute("SELECT holder FROM paragraph_samples").fetchone()[0] == holders[0]
    assert ra.vp()[holders[0]][1] == 1 and len(ra.vp()) == 1


def test_line_override_survives_a_voice_wide_relabel_of_another_cluster(ra):
    alex = ra.add(ALEX, tag="Alex", label="Alex", labeled_by="match", enrolled_in="")
    ra.line(1, "Alex")                                           # paragraph 1 (Speaker B's) -> Alex by hand
    out = feed.reassign_speaker(DEV, 1000, {"scope": "conversation", "tag": "Alex", "name": "Noah",
                                            "source": "user"})
    assert out["cluster"] == alex
    text = ra.daily()
    assert "[10:00] **Noah:** we should fix the door" in text    # the cluster's own line moved
    assert "[10:01] **Alex:** sure thing" in text                # the override stayed
    day = feed.day_payload(dt.date(2026, 11, 8))
    assert day["conversations"][0]["line_overrides"] == [1]


def test_voice_wide_relabel_applies_in_queue_order_around_overrides(ra):
    ra.add(ALEX, tag="Alex", label="Alex", labeled_by="match", enrolled_in="")
    with open(ra.data / ".process.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        ra.line(1, "Alex")
        feed.reassign_speaker(DEV, 1000, {"scope": "conversation", "tag": "Alex", "name": "Noah",
                                          "source": "user"})
        ra.line(0, "Alex")                                       # later override of the renamed line
    voices.apply_relabels(ra.con, ra.notes)
    text = ra.daily()
    assert "[10:00] **Alex:** we should fix the door" in text and "[10:01] **Alex:** sure thing" in text
