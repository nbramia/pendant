"""Voice Scoring (pendant_direct.voices + the feed's answer endpoints): candidate
order, daily budget, inclusion rules, confirm/correct voiceprint arithmetic,
transcript relabels, strangers and noise exemplars. Fake 4-d embeddings."""
from __future__ import annotations

import datetime as dt
import fcntl
import json
import sqlite3
import threading
import urllib.request

import numpy as np
import pytest

from pendant_direct import feed, speakers, voices

DEV = "0F1E2D3C4B5A6978"
E = np.eye(4, dtype=np.float32)
NOAH, ALEX, OTHER, NOISE = E[0], E[1], E[2], E[3]
HEADING = "10:00 — Garage Plans (5m00s) · old"
OTHER_HEADING = "11:00 — Lunch (2m00s) · old"
DAILY = f"""# Sunday

## {HEADING}

[10:00] **Alex:** we should fix the door

[10:01] **Speaker B:** sure thing

---

## {OTHER_HEADING}

[11:00] **Alex:** pass the salt

---
"""


def unit(v):
    v = np.asarray(v, dtype=np.float32)
    return v / np.linalg.norm(v)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    from pendant_direct import process
    from synth_audio import write_tone
    data, notes = tmp_path / "data", tmp_path / "notes"
    data.mkdir()
    notes.mkdir()
    (notes / "2026-11-08.md").write_text(DAILY)
    monkeypatch.setenv("PENDANT_DATA", str(data))
    monkeypatch.setenv("PENDANT_NOTES_DIR", str(notes))
    monkeypatch.setattr(process, "DB", data / "pages.db")
    (data / "config.json").write_text(json.dumps({"speakers": {"allowed": ["Noah", "Alex"]}}))
    clip = data / "clips" / "c.opus"
    clip.parent.mkdir()
    write_tone(clip, seconds=3.0)
    con = process.db()
    con.execute("INSERT INTO conversations (device, start_ms, day, raw_heading) VALUES (?, 1000, '2026-11-08', ?)",
                (DEV, HEADING))
    con.execute("INSERT INTO conversations (device, start_ms, day, raw_heading) VALUES (?, 2000, '2026-11-08', ?)",
                (DEV, OTHER_HEADING))
    con.commit()

    class Env:
        pass
    e = Env()
    e.data, e.notes, e.con, e.clip = data, notes, con, clip
    e.vdir = speakers.voiceprint_dir(data)

    def add(emb, tag="Speaker A", label=None, labeled_by=None, start_ms=1000, voiced=1.0, enrolled_in=None,
            excerpt="these are some spoken words"):
        cur = con.execute(
            "INSERT INTO speaker_clusters (device, start_ms, tag, label, embedding, clip, start_s, end_s, speech_s, "
            "excerpt, labeled_by, created_at, voiced, enrolled_in) VALUES (?,?,?,?,?,?,0.5,2.5,10,?,?,0,?,?)",
            (DEV, start_ms, tag, label, unit(emb).tobytes(), str(clip), excerpt, labeled_by, voiced, enrolled_in))
        con.commit()
        return cur.lastrowid
    e.add = add
    e.enroll = lambda name, emb, cid=None: speakers.enroll(e.vdir, name, emb, ["Noah", "Alex"], cluster_id=cid)
    e.ids = lambda **kw: [s["id"] for s in feed.unknown_payload(**kw)["speakers"]]
    e.daily = lambda: (notes / "2026-11-08.md").read_text()
    yield e
    con.close()


# ---------------------------------------------------------------------------
# scoring and selection (pure)

def test_confidence_is_similarity_and_margin():
    vps = {"Noah": (NOAH, 3), "Alex": (ALEX, 2)}
    embs = np.stack([unit([1, 0, 0, 0]), unit([1, 0.9, 0, 0]), unit([0, 0, 1, 0])])
    s = voices.score(embs, vps)
    assert s[0].best_name == "Noah" and s[0].confidence == pytest.approx(1.0)
    # close to both people: similarity is high but the margin is ~0
    assert s[1].similarity == pytest.approx(0.743, abs=1e-3) and s[1].confidence == pytest.approx(0.149, abs=1e-2)
    assert s[2].confidence == 0.0 and s[2].similarity == pytest.approx(0.0)
    # one voiceprint: the margin is over 0
    one = voices.score(embs[:1], {"Noah": (NOAH, 1)})[0]
    assert one.margin == pytest.approx(1.0) and one.confidence == pytest.approx(1.0)
    # no voiceprints: 0
    assert [x.confidence for x in voices.score(embs, {})] == [0.0, 0.0, 0.0]


def test_stranger_closer_rejects_the_voiceprint_guess():
    vps = {"Alex": (ALEX, 4)}
    voice = unit([0.2, 1, 0.9, 0])
    s = voices.score(voice[None], vps, strangers=np.stack([unit([0, 0.8, 1, 0])]))[0]
    assert s.stranger_closer and s.best_name is None and s.confidence == 0.0
    s = voices.score(voice[None], vps, strangers=np.stack([OTHER * -1]))[0]
    assert not s.stranger_closer and s.best_name == "Alex"
    # the processor's match obeys the same rule
    assert speakers.match(voice[None], vps, 0.5) == ["Alex"]
    assert speakers.match(voice[None], vps, 0.5, strangers=np.stack([unit([0, 0.8, 1, 0])])) == [None]


def cand(i, conf, sim=None, noise=False, when=0):
    return {"id": i, "confidence": conf, "similarity": conf if sim is None else sim,
            "noise_like": noise, "when": when, "suggested": None}


def test_select_orders_by_confidence_and_spends_the_budget_once():
    cands = [cand(i, 0.6 + i / 100) for i in range(12)]          # all above the new-voice threshold
    cands += [cand(100, 0.1), cand(101, 0.2)]                      # probably someone new
    out, new = voices.select(cands, {}, budget=8, new_voice_threshold=0.5)
    assert [c["id"] for c in out] == [100, 101, 0, 1, 2, 3, 4, 5, 6, 7]
    assert sorted(b for _i, b in new) == [0, 0] + [1] * 8
    offered = {i: {"budgeted": b, "skipped": 0} for i, b in new}
    # the same day again: the same cards, nothing new spent
    out2, new2 = voices.select(cands, offered, budget=8, new_voice_threshold=0.5)
    assert [c["id"] for c in out2] == [c["id"] for c in out] and new2 == []
    # more new voices still come through when the budget is gone
    out3, new3 = voices.select(cands + [cand(102, 0.0)], offered, budget=8, new_voice_threshold=0.5)
    assert out3[0]["id"] == 102 and new3 == [(102, 0)]
    # skipped today: left out
    offered[0]["skipped"] = 1
    assert 0 not in [c["id"] for c in voices.select(cands, offered, budget=8, new_voice_threshold=0.5)[0]]


def test_select_puts_noise_like_last_and_never_free():
    cands = [cand(1, 0.0, noise=True), cand(2, 0.9), cand(3, 0.0)]
    out, new = voices.select(cands, {}, budget=1, new_voice_threshold=0.5)
    assert [c["id"] for c in out] == [3, 2]          # noise-like gets no budget slot left
    out, _ = voices.select(cands, {}, budget=5, new_voice_threshold=0.5)
    assert [c["id"] for c in out] == [3, 2, 1]


def test_prior_enrollment_inference():
    assert voices.prior_enrollment("Alex", "user", "Noah") == "Alex"
    assert voices.prior_enrollment("", "calendar", "Alex") is None
    assert voices.prior_enrollment(None, "calendar", "Alex") == "Alex"
    assert voices.prior_enrollment(None, "meeting", None) is None
    assert voices.prior_enrollment(None, "match", "Alex") is None


def test_remove_is_the_exact_inverse_of_add_and_both_are_idempotent(tmp_path):
    rng = np.random.default_rng(0)
    a, b, c = (rng.normal(size=4).astype(np.float32) for _ in range(3))
    v, w = tmp_path / "v", tmp_path / "w"
    for cid, x in ((1, a), (2, b), (3, c)):
        speakers.add(v, "Alex", cid, x)
    assert speakers.add(v, "Alex", 2, b) == 3                    # already a member
    assert speakers.remove(v, "Alex", 2) == 2
    assert speakers.remove(v, "Alex", 2) == 2                    # already gone
    for cid, x in ((1, a), (3, c)):
        speakers.add(w, "Alex", cid, x)
    got, want = speakers.load_voiceprints(v)["Alex"], speakers.load_voiceprints(w)["Alex"]
    assert got[1] == want[1] == 2 and np.allclose(got[0], want[0], atol=1e-6)
    assert np.allclose(got[0], (speakers.normalize(a) + speakers.normalize(c)) / 2, atol=1e-6)
    speakers.remove(v, "Alex", 1)
    assert speakers.remove(v, "Alex", 3) == 0
    assert speakers.load_voiceprints(v) == {} and not (v / "Alex.npz").exists()
    assert speakers.remove(v, "Nobody", 1) == 0


# ---------------------------------------------------------------------------
# candidates from the feed

def test_candidates_include_auto_labels_and_exclude_answered(env):
    for _ in range(2):   # two samples each: nobody here is thinly sampled (speakers.min_samples)
        env.enroll("Noah", NOAH)
        env.enroll("Alex", ALEX)
    unl = env.add([0.3, 0.3, 1, 0])
    match = env.add([1, 0.8, 0, 0], tag="Noah", label="Noah", labeled_by="match")
    meet = env.add([0, 1, 0.1, 0], tag="Alex", label="Alex", labeled_by="meeting", enrolled_in="Alex")
    cal = env.add([0.4, 1, 0, 0], tag="Alex", label="Alex Smith", labeled_by="calendar")
    env.add(NOAH, tag="Noah", label="Noah", labeled_by="user")
    env.add(ALEX, labeled_by="dismissed")
    noisy = env.add(OTHER)
    env.con.execute("UPDATE speaker_clusters SET noise = 1 WHERE id = ?", (noisy,))
    env.add(OTHER, voiced=0.1)                                     # fails the voice check
    env.add(OTHER, excerpt="two words")                            # too few words
    env.con.commit()
    sp = feed.unknown_payload()["speakers"]
    assert [s["id"] for s in sp] == [unl, match, cal, meet]       # least confident first
    by = {s["id"]: s for s in sp}
    assert by[unl]["current_label"] is None and by[unl]["new_voice"] is True
    assert by[match]["current_label"] == "Noah" and by[cal]["current_label"] == "Alex Smith"
    assert all(set(s) >= {"current_label", "confidence", "excerpt", "when", "clip_id"} for s in sp)
    assert [s["confidence"] for s in sp] == sorted(s["confidence"] for s in sp)


def test_unlabeled_voice_near_a_voiceprint_is_asked_with_a_guess(env):
    env.enroll("Alex", ALEX)
    cid = env.add([0.1, 1, 0, 0])
    s = feed.unknown_payload()["speakers"][0]
    assert s["id"] == cid and s["current_label"] == "Alex" and s["new_voice"] is False


def test_daily_budget_across_requests_and_days(env):
    env.enroll("Noah", NOAH)
    known = [env.add([1, 0.05 * i, 0, 0]) for i in range(12)]     # all clearly Noah
    new = [env.add([0, 0, 1, 0.1 * i]) for i in range(3)]         # nobody we know
    day1 = dt.datetime(2026, 11, 8, 9, tzinfo=voices.TZ)
    first = env.ids(now=day1)
    assert set(new) <= set(first) and len(set(first) & set(known)) == 8
    assert env.ids(now=day1.replace(hour=20)) == first             # same day: same cards, no new budget
    more = env.add([0, 0.1, 1, 0])                                 # a new voice later that day still comes
    assert more in env.ids(now=day1.replace(hour=21))
    day2 = env.ids(now=day1 + dt.timedelta(days=1))
    assert len(set(day2) & set(known)) == 8
    offers = env.con.execute("SELECT day, SUM(budgeted) FROM voice_offers GROUP BY day").fetchall()
    assert offers == [("2026-11-08", 8), ("2026-11-09", 8)]


def test_skip_hides_a_card_for_good_and_changes_nothing_else(env):
    cid = env.add(OTHER)
    other = env.add(OTHER, start_ms=2000)
    assert set(env.ids()) == {cid, other}
    assert feed.skip_cluster(cid) == {"id": cid, "skipped": True}
    assert env.ids() == [other]
    later = dt.datetime.now(voices.TZ)
    assert env.ids(now=later + dt.timedelta(days=1)) == [other]      # not offered again the next day
    assert env.ids(now=later + dt.timedelta(days=30)) == [other]
    row = env.con.execute("SELECT label, labeled_by, label_kind, COALESCE(mixed, 0) FROM speaker_clusters "
                          "WHERE id = ?", (cid,)).fetchone()
    assert row == (None, None, None, 0)
    assert env.con.execute("SELECT COUNT(*) FROM strangers").fetchone()[0] == 0


def test_two_people_takes_the_clip_out_of_its_voiceprint_and_keeps_the_label(env):
    others = [unit([0.1, 1, 0, 0]), unit([0, 1, 0.2, 0])]
    for x in others:
        env.enroll("Alex", x)
    emb = unit([1, 0.4, 0, 0.3])
    cid = env.add(emb, tag="Alex", label="Alex", labeled_by="meeting", enrolled_in="Alex")
    env.enroll("Alex", emb, cid)
    assert cid in env.ids()
    before = env.daily()
    assert feed.mark_mixed(cid) == {"id": cid, "mixed": True}
    mean, n = speakers.load_voiceprints(env.vdir)["Alex"]
    assert n == 2 and np.allclose(mean, (others[0] + others[1]) / 2, atol=1e-6)
    row = env.con.execute("SELECT label, labeled_by, label_kind, mixed, enrolled_in FROM speaker_clusters "
                          "WHERE id = ?", (cid,)).fetchone()
    assert row == ("Alex", "dismissed", "mixed", 1, "")
    assert env.con.execute("SELECT COUNT(*) FROM strangers").fetchone()[0] == 0
    assert env.con.execute("SELECT COUNT(*) FROM noise_exemplars").fetchone()[0] == 0
    assert env.daily() == before
    assert cid not in env.ids()
    assert cid not in env.ids(now=dt.datetime.now(voices.TZ) + dt.timedelta(days=1))
    assert feed.mark_mixed(cid) == {"id": cid, "mixed": True}       # a retry changes nothing
    assert speakers.load_voiceprints(env.vdir)["Alex"][1] == 2


def test_two_people_on_a_stranger_drops_its_stranger_record(env):
    cid = env.add(OTHER)
    feed.stranger_cluster(cid)
    assert env.con.execute("SELECT COUNT(*) FROM strangers").fetchone()[0] == 1
    feed.mark_mixed(cid)
    assert env.con.execute("SELECT COUNT(*) FROM strangers").fetchone()[0] == 0


# ---------------------------------------------------------------------------
# answers

def test_confirm_a_match_reinforces_that_voiceprint(env):
    env.enroll("Alex", ALEX)
    cid = env.add([0.2, 1, 0, 0], tag="Alex", label="Alex", labeled_by="match", enrolled_in="")
    out = feed.label_cluster(cid, "Alex")
    assert out == {"id": cid, "name": "Alex", "count": 2, "action": "confirm", "relabeled": False}
    mean, n = speakers.load_voiceprints(env.vdir)["Alex"]
    assert n == 2 and np.allclose(mean, (ALEX + unit([0.2, 1, 0, 0])) / 2, atol=1e-6)
    row = env.con.execute("SELECT labeled_by, label_kind, enrolled_in FROM speaker_clusters WHERE id=?",
                          (cid,)).fetchone()
    assert row == ("user", "person", "Alex")
    assert cid not in env.ids()


def test_confirm_an_already_enrolled_sample_does_not_double_count(env):
    emb = unit([0.1, 1, 0, 0])
    env.enroll("Alex", ALEX)
    cid = env.add(emb, tag="Alex", label="Alex", labeled_by="calendar", enrolled_in="Alex")
    env.enroll("Alex", emb, cid)       # the calendar pass folded this cluster in
    assert feed.label_cluster(cid, "Alex")["count"] == 2
    assert speakers.load_voiceprints(env.vdir)["Alex"][1] == 2


def test_correct_moves_the_sample_exactly_and_relabels_the_transcript(env):
    others = [unit([0.1, 1, 0, 0]), unit([0, 1, 0.2, 0])]
    for x in others:
        env.enroll("Alex", x)
    wrong = unit([1, 0.4, 0, 0])
    cid = env.add(wrong, tag="Alex", label="Alex", labeled_by="meeting", enrolled_in="Alex")
    env.enroll("Alex", wrong, cid)     # the meeting pass put Noah's voice in Alex's voiceprint
    out = feed.label_cluster(cid, "Noah", "user")
    assert out["action"] == "correct" and out["relabeled"] is True and out["count"] == 1
    vps = speakers.load_voiceprints(env.vdir)
    assert vps["Alex"][1] == 2 and np.allclose(vps["Alex"][0], (others[0] + others[1]) / 2, atol=1e-6)
    assert vps["Noah"][1] == 1 and np.allclose(vps["Noah"][0], wrong, atol=1e-6)
    text = env.daily()
    assert "[10:00] **Noah:** we should fix the door" in text
    assert "[11:00] **Alex:** pass the salt" in text            # another conversation is untouched
    # applying the queue again changes nothing
    assert voices.apply_relabels(env.con, env.notes) == set()
    env.con.execute("UPDATE transcript_relabels SET done_at = NULL")
    voices.apply_relabels(env.con, env.notes)
    assert env.daily() == text


def test_relabel_section_is_idempotent_and_scoped():
    once = voices.relabel_section(DAILY, HEADING, "Alex", "Noah")
    assert once.count("**Noah:**") == 1 and once.count("**Alex:**") == 1
    assert voices.relabel_section(once, HEADING, "Alex", "Noah") == once
    assert voices.relabel_section(DAILY, "nope", "Alex", "Noah") == DAILY


def test_relabel_waits_for_the_processor_lock(env):
    cid = env.add(OTHER, tag="Speaker B")
    with open(env.data / ".process.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        # flock locks are per open file description: a second open in this process
        # contends like another process would
        assert feed.label_cluster(cid, "Alex")["relabeled"] is True
        assert "**Speaker B:**" in env.daily()                    # queued, not applied
    assert voices.apply_relabels(env.con, env.notes) == {"2026-11-08"}
    assert "[10:01] **Alex:** sure thing" in env.daily()


def test_relabel_skipped_when_two_clusters_share_the_tag(env):
    cid = env.add(OTHER, tag="Alex", label="Alex", labeled_by="meeting", enrolled_in="")
    env.add(ALEX, tag="Alex", label="Alex", labeled_by="user")
    assert feed.label_cluster(cid, "Noah", "user")["relabeled"] is False
    assert env.daily() == DAILY


def test_answered_clusters_are_final(env):
    cid = env.add(OTHER)
    feed.label_cluster(cid, "Noah")
    for fn, args in ((feed.label_cluster, ("Alex",)), (feed.stranger_cluster, ())):
        with pytest.raises(feed.HTTPError) as e:
            fn(cid, *args)
        assert e.value.code == 409


# ---------------------------------------------------------------------------
# strangers and noise

def test_stranger_is_stored_not_merged_and_keeps_similar_voices_coming(env):
    env.enroll("Alex", ALEX)
    a = env.add([0, 0.6, 1, 0], tag="Speaker B")
    out = feed.stranger_cluster(a)                 # an unnamed voice keeps its own "Speaker X"
    assert out == {"id": a, "stranger": True, "name": "Speaker B", "relabeled": False}
    assert "[10:01] **Speaker B:** sure thing" in env.daily()
    assert env.con.execute("SELECT tag, label, labeled_by, label_kind FROM speaker_clusters WHERE id=?",
                           (a,)).fetchone() == ("Speaker B", None, "user", "stranger")
    # a similar voice, auto-matched to Alex: still asked about, but not as Alex
    b = env.add([0, 0.75, 1, 0], tag="Alex", label="Alex", labeled_by="match", start_ms=2000)
    c = env.add([0, 0.4, 1, 0.05], start_ms=2000)
    sp = {s["id"]: s for s in feed.unknown_payload()["speakers"]}
    assert set(sp) == {b, c}
    assert sp[b]["current_label"] is None and sp[b]["confidence"] == 0.0
    assert sp[c]["current_label"] is None and sp[c]["new_voice"] is True
    feed.stranger_cluster(c)
    assert env.con.execute("SELECT COUNT(*) FROM strangers").fetchone()[0] == 2   # one row per answer
    assert speakers.load_voiceprints(env.vdir)["Alex"][1] == 1                  # never enrolled anyone


def test_stranger_takes_the_sample_out_of_a_voiceprint(env):
    env.enroll("Alex", ALEX)
    emb = unit([0.3, 1, 0, 0])
    cid = env.add(emb, tag="Alex", label="Alex", labeled_by="calendar", enrolled_in="Alex")
    env.enroll("Alex", emb, cid)
    feed.stranger_cluster(cid)
    mean, n = speakers.load_voiceprints(env.vdir)["Alex"]
    assert n == 1 and np.allclose(mean, ALEX, atol=1e-6)


def test_people_view_ignores_someone():
    assert feed.GENERIC_TAG.match("Someone")


def test_not_speech_stores_an_exemplar_and_lowers_similar_clusters(env):
    env.enroll("Alex", ALEX)
    emb = unit([0, 0.3, 0, 1])
    hum = env.add(emb, tag="Alex", label="Alex", labeled_by="meeting", enrolled_in="Alex")
    env.enroll("Alex", emb, hum)
    assert feed.mark_noise(hum) == {"id": hum, "noise": True}
    assert speakers.load_voiceprints(env.vdir)["Alex"][1] == 1
    assert env.con.execute("SELECT cluster_id FROM noise_exemplars").fetchall() == [(hum,)]
    similar = env.add([0, 0.2, 0, 1])
    voice = env.add([0.2, 1, 0, 0])            # a confident Alex
    sp = feed.unknown_payload()["speakers"]
    assert [s["id"] for s in sp] == [voice, similar]
    assert sp[-1]["noise_like"] is True and hum not in [s["id"] for s in sp]
    # marking noise again is harmless; labeling it is refused
    assert feed.mark_noise(hum) == {"id": hum, "noise": True}
    assert env.con.execute("SELECT COUNT(*) FROM noise_exemplars").fetchone()[0] == 1
    with pytest.raises(feed.HTTPError):
        feed.label_cluster(hum, "Alex")


# ---------------------------------------------------------------------------
# roster and routes

def test_roster_is_sorted_by_recognitions(env):
    env.enroll("Noah", NOAH)
    for x in (ALEX, ALEX):
        env.enroll("Alex", x)
    env.add(NOAH, tag="Noah", label="Noah", labeled_by="match")
    for _ in range(3):
        env.add(ALEX, tag="Alex", label="Alex", labeled_by="match")
    env.add(ALEX, label="Alex", labeled_by="dismissed")
    out = feed.speakers_payload()
    assert out["enrolled"] == [{"name": "Alex", "count": 2, "recognized": 3},
                               {"name": "Noah", "count": 1, "recognized": 1}]


def test_stranger_and_skip_routes(env):
    a, b = env.add(OTHER), env.add(OTHER * -1 + ALEX)
    srv = feed.make_server(0, "tok")
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{srv.server_address[1]}/pendant/feed/speakers"
    try:
        c = env.add(ALEX, start_ms=2000)
        for cid, verb in ((a, "stranger"), (b, "skip"), (c, "mixed")):
            req = urllib.request.Request(f"{base}/{cid}/{verb}", data=b"{}", method="POST",
                                         headers={"Authorization": "Bearer tok"})
            with urllib.request.urlopen(req) as r:
                assert r.status == 200
        assert env.con.execute("SELECT COUNT(*) FROM strangers").fetchone()[0] == 1
        assert env.ids() == []
    finally:
        srv.shutdown()


def test_old_cluster_table_gets_the_new_columns(tmp_path, monkeypatch):
    from pendant_direct import process
    con = sqlite3.connect(tmp_path / "pages.db")
    con.execute("CREATE TABLE speaker_clusters (id INTEGER PRIMARY KEY, device TEXT, start_ms INT, tag TEXT, "
                "label TEXT, embedding BLOB, clip TEXT, start_s REAL, end_s REAL, speech_s REAL, excerpt TEXT, "
                "labeled_by TEXT, created_at REAL)")
    con.commit()
    con.close()
    monkeypatch.setattr(process, "DB", tmp_path / "pages.db")
    con = process.db()
    cols = {r[1] for r in con.execute("PRAGMA table_info(speaker_clusters)")}
    assert {"voiced", "noise", "enrolled_in", "label_kind"} <= cols
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"strangers", "noise_exemplars", "voice_offers", "transcript_relabels"} <= tables


# ---------------------------------------------------------------------------
# voiceprint integrity: legacy migration, concurrent writers, retried answers

def write_legacy(vdir, name, mean, count):
    vdir.mkdir(parents=True, exist_ok=True)
    np.save(vdir / f"{name}.npy", np.asarray(mean, dtype=np.float32))
    (vdir / f"{name}.json").write_text(json.dumps({"count": count}))


def test_legacy_files_are_read_and_migrated_as_opaque_history(tmp_path):
    v = tmp_path / "v"
    old_mean = unit([1, 1, 0, 0]) * 0.9
    write_legacy(v, "Alex", old_mean, 5)
    mean, n = speakers.load_voiceprints(v)["Alex"]        # read before any migration
    assert n == 5 and np.allclose(mean, old_mean)
    assert speakers.migrate_voiceprints(v) == 1
    assert not (v / "Alex.npy").exists() and not (v / "Alex.json").exists() and (v / "Alex.npz").exists()
    assert speakers.migrate_voiceprints(v) == 0
    mean, n = speakers.load_voiceprints(v)["Alex"]
    assert n == 5 and np.allclose(mean, old_mean, atol=1e-6)
    # history is opaque: an id can't take a sample out of it; new members are removable
    assert speakers.remove(v, "Alex", 1) == 5
    assert speakers.add(v, "Alex", 9, ALEX) == 6
    assert np.allclose(speakers.load_voiceprints(v)["Alex"][0], (old_mean * 5 + ALEX) / 6, atol=1e-6)
    assert speakers.remove(v, "Alex", 9) == 5
    assert np.allclose(speakers.load_voiceprints(v)["Alex"][0], old_mean, atol=1e-6)


def test_a_write_migrates_legacy_files_too(tmp_path):
    v = tmp_path / "v"
    write_legacy(v, "Noah", NOAH, 3)
    assert speakers.add(v, "Noah", 4, NOAH) == 4
    assert sorted(p.name for p in v.iterdir() if not p.name.startswith(".")) == ["Noah.npz"]


def _hammer(v, name, ids):
    for i in ids:
        speakers.add(v, name, i, unit([1, 0.01 * i, 0, 0]))


def test_concurrent_threads_do_not_lose_updates(tmp_path):
    v = tmp_path / "v"
    threads = [threading.Thread(target=_hammer, args=(v, "Alex", range(k * 25, k * 25 + 25))) for k in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(speakers.members(v, "Alex")) == list(range(100))
    assert speakers.load_voiceprints(v)["Alex"][1] == 100
    assert not [p for p in v.iterdir() if p.name.endswith(".tmp")]


def test_concurrent_processes_do_not_lose_updates(tmp_path):
    import multiprocessing as mp
    v = tmp_path / "v"
    ctx = mp.get_context("fork")
    procs = [ctx.Process(target=_hammer, args=(v, "Noah", range(k * 20, k * 20 + 20))) for k in range(3)]
    for p in procs:
        p.start()
    _hammer(v, "Noah", range(60, 80))          # the feed, while the "processor" writes
    for p in procs:
        p.join()
        assert p.exitcode == 0
    assert sorted(speakers.members(v, "Noah")) == list(range(80))


class Boom(Exception):
    pass


@pytest.mark.parametrize("answer", ["correct", "confirm", "stranger", "noise", "mixed"])
def test_retry_after_a_failure_between_files_and_commit(env, monkeypatch, answer):
    others = [unit([0.1, 1, 0, 0]), unit([0, 1, 0.2, 0])]
    for x in others:
        env.enroll("Alex", x)
    emb = unit([1, 0.4, 0, 0.3])
    cid = env.add(emb, tag="Alex", label="Alex", labeled_by="meeting", enrolled_in="Alex")
    env.enroll("Alex", emb, cid)
    env.enroll("Noah", NOAH)
    call = {
        "correct": lambda: feed.label_cluster(cid, "Noah", "user"),
        "confirm": lambda: feed.label_cluster(cid, "Alex"),
        "stranger": lambda: feed.stranger_cluster(cid),
        "noise": lambda: feed.mark_noise(cid),
        "mixed": lambda: feed.mark_mixed(cid),
    }[answer]
    real = feed._finish

    def fail(*a, **k):
        raise Boom()
    monkeypatch.setattr(feed, "_finish", fail)        # files are written, the commit never happens
    with pytest.raises(Boom):
        call()
    monkeypatch.setattr(feed, "_finish", real)
    call()                                            # the app retries
    vps = speakers.load_voiceprints(env.vdir)
    want_alex = {"correct": 2, "confirm": 3, "stranger": 2, "noise": 2, "mixed": 2}[answer]
    want_wearer = {"correct": 2, "confirm": 1, "stranger": 1, "noise": 1, "mixed": 1}[answer]
    assert vps["Alex"][1] == want_alex and vps["Noah"][1] == want_wearer
    if answer != "confirm":
        assert np.allclose(vps["Alex"][0], (others[0] + others[1]) / 2, atol=1e-6)
    assert cid not in env.ids()
