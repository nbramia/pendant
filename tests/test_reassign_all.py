"""Reassigning a voice across all past conversations (feed.bulk_preview / scope "all"):
matching by voice similarity, every exclusion rule, preview and apply agreeing,
line overrides, voiceprint effects, consent, the sample, and the HTTP preview.
Fake 4-d embeddings."""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import numpy as np
import pytest
from test_voices import DAILY, DEV, E, env, unit  # noqa: F401

from pendant_direct import feed, speakers, voices

SIM = [0, 1, 0, 0]                  # the tapped voice


@pytest.fixture()
def ba(env):
    """env + a tapped voice (conversation 1000, "Speaker B") and a builder for past conversations."""
    env.tapped = env.add(SIM, tag="Speaker B")
    env.con.execute("DELETE FROM conversations WHERE start_ms = 2000")
    env.con.commit()
    files: dict[str, list[str]] = {"2026-11-08": [DAILY.split("## 11:00")[0]]}

    def conv(start_ms, day, paras, voices_=(), title="Chat"):
        """A past conversation: `paras` are (tag, text) lines; `voices_` are (tag, emb, kwargs) clusters."""
        heading = f"{9 + start_ms % 10}:00 — {title} {start_ms} (1m00s) · old"
        body = "\n\n".join(f"[09:{i:02d}] **{t}:** {w}" for i, (t, w) in enumerate(paras))
        files.setdefault(day, ["# day"]).append(f"## {heading}\n\n{body}\n\n---\n")
        (env.notes / f"{day}.md").write_text("\n".join(files[day]))
        env.con.execute("INSERT INTO conversations (device, start_ms, day, raw_heading, title) VALUES (?,?,?,?,?)",
                        (DEV, start_ms, day, heading, title))
        env.con.commit()
        return [env.add(emb, tag=tag, start_ms=start_ms, **kw) for tag, emb, kw in voices_]
    env.conv = conv
    env.preview = lambda name="Noah", **kw: feed.bulk_preview(DEV, 1000, "Speaker B", name, "user", **kw)
    def apply(name="Noah", preview=None, **kw):
        p = preview or env.preview(name)
        return feed.reassign_speaker(DEV, 1000, {"scope": "all", "tag": "Speaker B", "name": name, "source": "user",
                                                 "ids": p["ids"], "digest": p["digest"], **kw})
    env.apply = apply
    env.vp = lambda: speakers.load_voiceprints(env.vdir)
    return env


def populate(ba):
    ids = {}
    ids["match1"] = ba.conv(2000, "2026-11-08", [("Alex", "pass the salt please")],
                            [("Alex", [0.1, 1, 0, 0], {"labeled_by": "match"})])[0]
    a, c = ba.conv(3000, "2026-11-07", [("Speaker A", "one two three"), ("Speaker C", "not him"),
                                        ("Speaker A", "four five")],
                   [("Speaker A", [0, 1, 0.1, 0], {}), ("Speaker C", [1, 0, 0, 0], {})], title="Lunch")
    ids["match2"], ids["other"] = a, c
    ids["user"] = ba.conv(4000, "2026-11-06", [("Speaker A", "answered by hand")],
                          [("Speaker A", SIM, {"labeled_by": "user"})])[0]
    ids["dismissed"] = ba.conv(4100, "2026-11-06", [("Speaker A", "dismissed words")],
                               [("Speaker A", SIM, {"labeled_by": "dismissed"})])[0]
    n = ba.conv(5000, "2026-11-06", [("Speaker A", "noise words")], [("Speaker A", SIM, {})])[0]
    ba.con.execute("UPDATE speaker_clusters SET noise = 1 WHERE id = ?", (n,))
    ids["noise"] = n
    ids["named"] = ba.conv(5100, "2026-11-06", [("Noah", "already him")],
                           [("Noah", SIM, {"labeled_by": "match", "label": "Noah"})])[0]
    ids["shared"] = ba.conv(6000, "2026-11-05", [("Speaker A", "mixed up voices")],
                            [("Speaker A", SIM, {}), ("Speaker A", [1, 0, 0, 0], {})])
    ba.con.commit()
    return ids


def raw(ba):
    return {p.name: p.read_text() for p in sorted(ba.notes.glob("*.md"))}


def test_preview_counts_every_exclusion_and_changes_nothing(ba):
    ids = populate(ba)
    before = (raw(ba), ba.con.execute("SELECT * FROM speaker_clusters").fetchall(),
              ba.con.execute("SELECT COUNT(*) FROM transcript_relabels").fetchone(), (ba.data / "config.json").read_text())
    out = ba.preview()
    # matched: Alex in 2000 (4 words) and Speaker A in 3000 (3 + 2 words)
    assert (out["conversations"], out["days"], out["words"]) == (2, 2, 9)
    assert out["skipped"] == 1                       # the shared tag in 6000 (one matching voice)
    after = (raw(ba), ba.con.execute("SELECT * FROM speaker_clusters").fetchall(),
             ba.con.execute("SELECT COUNT(*) FROM transcript_relabels").fetchone(), (ba.data / "config.json").read_text())
    assert before == after
    assert ba.vp() == {}
    assert {s["start_ms"] for s in out["sample"]} == {2000, 3000}
    assert ids["user"] and ids["noise"]


def test_sample_entries_carry_what_the_app_shows(ba):
    populate(ba)
    s = {e["start_ms"]: e for e in ba.preview()["sample"]}
    e = s[3000]
    assert e["date"] == "2026-11-07" and e["time"] == "9:00"
    assert e["title"] == "Lunch" and e["device"] == DEV and e["label"] == "Speaker A"
    assert e["excerpt"] == "one two three" and e["paragraphs"] == [0, 2]
    assert 0.9 < e["similarity"] <= 1.0
    assert s[2000]["label"] == "Alex" and s[2000]["paragraphs"] == [0]


def test_each_exclusion_rule_alone(ba):
    ba.conv(2000, "2026-11-08", [("Speaker A", "hand answered")], [("Speaker A", SIM, {"labeled_by": "user"})])
    ba.conv(2100, "2026-11-08", [("Speaker A", "dismissed")], [("Speaker A", SIM, {"labeled_by": "dismissed"})])
    noisy = ba.conv(2200, "2026-11-08", [("Speaker A", "noise")], [("Speaker A", SIM, {})])[0]
    ba.con.execute("UPDATE speaker_clusters SET label_kind = 'noise' WHERE id = ?", (noisy,))
    ba.con.commit()
    assert ba.preview()["conversations"] == 0        # hand answered, dismissed and noise-kind voices are all left out


def test_below_the_match_threshold_is_not_matched(ba):
    ba.conv(2000, "2026-11-08", [("Speaker A", "close voice")], [("Speaker A", [0.1, 1, 0, 0], {})])   # 0.995
    ba.conv(2100, "2026-11-08", [("Speaker A", "far voice")], [("Speaker A", [1, 1, 0, 0], {})])       # 0.707
    ba.conv(2200, "2026-11-08", [("Speaker A", "borderline")], [("Speaker A", [1, 1.732, 0, 0], {})])  # 0.866
    assert ba.preview()["conversations"] == 3
    (ba.data / "config.json").write_text(json.dumps({"speakers": {"allowed": ["Noah"], "match_threshold": 0.8}}))
    assert ba.preview()["conversations"] == 2
    (ba.data / "config.json").write_text(json.dumps({"speakers": {"allowed": ["Noah"], "match_threshold": 0.99}}))
    assert ba.preview()["conversations"] == 1


def test_apply_relabels_exactly_what_the_preview_reported(ba):
    populate(ba)
    out = ba.preview()
    done = ba.apply()
    assert (done["conversations"], done["days"], done["words"]) == (out["conversations"], out["days"], out["words"])
    assert done["voices"] == 2 and done["skipped"] == out["skipped"] and done["applied"] is True
    text = raw(ba)
    assert "[09:00] **Noah:** pass the salt please" in text["2026-11-08.md"]
    lunch = text["2026-11-07.md"]
    assert "**Noah:** one two three" in lunch and "**Noah:** four five" in lunch
    assert "**Speaker C:** not him" in lunch
    for untouched in ("answered by hand", "dismissed words", "noise words", "mixed up voices"):
        assert any(f"**Speaker A:** {untouched}" in t for t in text.values())
    # the words the files now show under Noah are the previewed ones (plus the tapped conversation's own)
    wearer_words = sum(len(line.split(":** ")[1].split()) for t in text.values() for line in t.split("\n")
                       if line.startswith("[") and "**Noah:**" in line and "sure thing" not in line)
    assert wearer_words == out["words"] + 2        # + "already him", which was Noah before
    again = feed.bulk_preview(DEV, 1000, "Noah", "Noah")        # the tapped voice now carries the name
    assert again["conversations"] == 0                              # nothing left for a second apply


def test_apply_relabels_the_tapped_voice_too_and_only_it_reaches_the_voiceprint(ba):
    populate(ba)
    ba.apply()
    mean, n = ba.vp()["Noah"]
    assert n == 1 and np.allclose(mean, unit(SIM), atol=1e-6)
    assert ba.con.execute("SELECT tag, labeled_by, enrolled_in FROM speaker_clusters WHERE id = ?",
                          (ba.tapped,)).fetchone() == ("Noah", "user", "Noah")
    rows = ba.con.execute("SELECT tag, label, labeled_by, enrolled_in, label_kind, noise FROM speaker_clusters "
                          "WHERE start_ms IN (2000, 3000) AND tag != 'Speaker C'").fetchall()
    assert rows == [("Noah", "Noah", "user", "", "person", 0)] * 2


def test_apply_records_the_answers_and_voice_scoring_stops_asking(ba):
    ids = populate(ba)
    ba.apply()
    answered = {r[0] for r in ba.con.execute("SELECT cluster_id FROM voice_offers WHERE answered = 1")}
    assert {ids["match1"], ids["match2"], ba.tapped} <= answered
    assert ids["match1"] not in ba.ids(limit=500) and ids["match2"] not in ba.ids(limit=500)
    batch = ba.con.execute("SELECT cluster_id, old_tag, old_labeled_by, new_tag FROM bulk_relabels").fetchall()
    assert sorted(batch) == sorted([(ids["match1"], "Alex", "match", "Noah"),
                                    (ids["match2"], "Speaker A", None, "Noah")])
    assert len({r[0] for r in ba.con.execute("SELECT batch FROM bulk_relabels")}) == 1


def test_line_overrides_survive_and_are_not_counted(ba):
    populate(ba)
    feed.reassign_speaker(DEV, 3000, {"scope": "line", "paragraph": 0, "name": "Alex", "source": "user"})
    assert "**Alex:** one two three" in raw(ba)["2026-11-07.md"]
    out = ba.preview()
    assert out["words"] == 9 - 3                      # the overridden line is not counted
    done = ba.apply()
    assert done["words"] == out["words"]
    lunch = raw(ba)["2026-11-07.md"]
    assert "**Alex:** one two three" in lunch and "**Noah:** four five" in lunch


def test_matched_voices_are_not_folded_into_other_voiceprints_they_were_in(ba):
    ids = populate(ba)
    ba.con.execute("UPDATE speaker_clusters SET label = 'Alex', labeled_by = 'calendar', enrolled_in = 'Alex' "
                   "WHERE id = ?", (ids["match1"],))
    ba.con.commit()
    ba.enroll("Alex", unit([0.1, 1, 0, 0]), cid=ids["match1"])
    ba.enroll("Alex", unit([0, 0, 1, 0]), cid=999)       # Alex's voiceprint sits far from this voice
    assert ba.vp()["Alex"][1] == 2
    ba.apply()
    assert ba.vp()["Alex"][1] == 1 and ba.vp()["Noah"][1] == 1


def test_remove_name_is_not_offered_for_all_past_conversations(ba):
    populate(ba)
    before = raw(ba)
    with pytest.raises(feed.HTTPError) as e:
        ba.preview("someone")
    assert e.value.code == 400
    for body in ({"remove": True}, {"name": None}, {"name": "Someone"}):
        req = {"scope": "all", "tag": "Speaker B", "ids": [], "digest": "", **body}
        with pytest.raises(feed.HTTPError) as e:
            feed.reassign_recorded(DEV, 1000, req)
        assert e.value.code == 400
    assert raw(ba) == before and ba.con.execute("SELECT COUNT(*) FROM strangers").fetchone()[0] == 0
    assert ba.con.execute("SELECT COUNT(*) FROM reassign_undo").fetchone()[0] == 0


def test_consent_for_preview_and_apply(ba):
    populate(ba)
    for call in (lambda: feed.bulk_preview(DEV, 1000, "Speaker B", "Zed Unlisted"),
                 lambda: feed.reassign_speaker(DEV, 1000, {"scope": "all", "tag": "Speaker B", "name": "Zed Unlisted",
                                                           "ids": [], "digest": ""})):
        with pytest.raises(feed.HTTPError) as e:
            call()
        assert e.value.code == 403
    cfg = (ba.data / "config.json").read_text()
    out = feed.bulk_preview(DEV, 1000, "Speaker B", "zed unlisted", "user")        # a typed name previews...
    assert out["name"] == "Zed Unlisted" and (ba.data / "config.json").read_text() == cfg   # ...without joining the list
    with pytest.raises(feed.HTTPError) as e:
        feed.bulk_preview(DEV, 1000, "Speaker B", "R2-D2", "user")
    assert e.value.code == 400
    ba.apply("zed unlisted")
    assert "Zed Unlisted" in json.loads((ba.data / "config.json").read_text())["speakers"]["allowed"]
    assert ba.vp()["Zed Unlisted"][1] == 1


def test_tapped_voice_errors(ba):
    with pytest.raises(feed.HTTPError) as e:
        feed.bulk_preview(DEV, 1000, "Ghost", "Noah")
    assert e.value.code == 404
    with pytest.raises(feed.HTTPError) as e:
        feed.bulk_preview(DEV, 1000, "", "Noah")
    assert e.value.code == 400
    ba.add([0, 0, 1, 0], tag="Speaker B")                                         # two voices behind the tag
    with pytest.raises(feed.HTTPError) as e:
        feed.bulk_preview(DEV, 1000, "Speaker B", "Noah")
    assert e.value.code == 409


def test_sample_is_capped_spread_across_days_and_ordered_by_similarity(ba):
    ms = 10000
    for day, n in (("2026-11-01", 25), ("2026-11-02", 3), ("2026-11-03", 1)):
        for i in range(n):
            ms += 1
            ba.conv(ms, day, [("Speaker A", f"words {i}")], [("Speaker A", [0.05 * (i + 1), 1, 0, 0], {})])
    out = ba.preview()
    assert out["conversations"] == 29 and out["days"] == 3
    sample = out["sample"]
    assert len(sample) == feed.BULK_SAMPLE_MAX
    by_day = {}
    for e in sample:
        by_day.setdefault(e["date"], []).append(e)
    assert len(by_day["2026-11-03"]) == 1 and len(by_day["2026-11-02"]) == 3 and len(by_day["2026-11-01"]) == 16
    assert [e["date"] for e in sample] == sorted((e["date"] for e in sample), reverse=True)
    for es in by_day.values():
        sims = [e["similarity"] for e in es]
        assert sims == sorted(sims, reverse=True)


def test_a_conversation_with_two_matched_voices_is_one_sample_entry(ba):
    ba.conv(2000, "2026-11-08", [("Speaker A", "hello there"), ("Speaker D", "and also this")],
            [("Speaker A", [0.1, 1, 0, 0], {}), ("Speaker D", [0, 1, 0.1, 0], {})])
    out = ba.preview()
    assert out["conversations"] == 1 and len(out["sample"]) == 1
    assert out["sample"][0]["paragraphs"] == [0, 1] and out["words"] == 5
    assert ba.apply()["voices"] == 2


def test_http_preview_and_apply(ba):
    populate(ba)
    srv = feed.make_server(0, "tok")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{srv.server_address[1]}/pendant/feed/conversations/{DEV}/1000/speaker"

        def call(url, body=None, token="tok"):
            req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                                         method="POST" if body is not None else "GET",
                                         headers={"Authorization": f"Bearer {token}"})
            with urllib.request.urlopen(req) as r:
                return json.loads(r.read())
        out = call(f"{base}/preview?tag=Speaker%20B&name=Noah&source=user")
        assert (out["conversations"], out["days"], out["words"]) == (2, 2, 9) and len(out["sample"]) == 2
        with pytest.raises(urllib.error.HTTPError) as e:
            call(f"{base}/preview?tag=Speaker%20B&name=Zed%20Unlisted")
        assert e.value.code == 403
        with pytest.raises(urllib.error.HTTPError) as e:
            call(f"{base}/preview?tag=Speaker%20B&name=Noah", token="bad")
        assert e.value.code == 401
        body = {"scope": "all", "tag": "Speaker B", "name": "Noah", "source": "user"}
        with pytest.raises(urllib.error.HTTPError) as e:
            call(base, {**body, "ids": out["ids"], "digest": "stale"})
        assert e.value.code == 409 and json.loads(e.value.read()) == {"error": "preview changed"}
        done = call(base, {**body, "ids": out["ids"], "digest": out["digest"]})
        assert done["words"] == 9 and done["scope"] == "all"
    finally:
        srv.shutdown()


def snapshot(ba):
    return (raw(ba), ba.con.execute("SELECT * FROM speaker_clusters").fetchall(),
            ba.con.execute("SELECT COUNT(*) FROM transcript_relabels").fetchone(),
            ba.con.execute("SELECT COUNT(*) FROM bulk_relabels").fetchone(), ba.vp())


def test_preview_returns_the_matched_ids_and_a_digest(ba):
    ids = populate(ba)
    out = ba.preview()
    assert out["ids"] == sorted([ids["match1"], ids["match2"]])
    assert len(out["digest"]) == 16 and out["digest"] == ba.preview()["digest"]


def test_a_voice_added_between_preview_and_apply_changes_nothing(ba):
    populate(ba)
    p = ba.preview()
    ba.conv(7000, "2026-11-04", [("Speaker A", "arrived while reviewing")], [("Speaker A", [0.1, 1, 0, 0], {})])
    before = snapshot(ba)
    with pytest.raises(feed.HTTPError) as e:
        ba.apply(preview=p)
    assert e.value.code == 409 and e.value.msg == "preview changed"
    assert snapshot(ba) == before
    again = ba.preview()
    assert again["conversations"] == p["conversations"] + 1
    assert ba.apply(preview=again)["conversations"] == again["conversations"]


def test_a_changed_override_or_threshold_also_stops_the_apply(ba):
    populate(ba)
    p = ba.preview()
    feed.reassign_speaker(DEV, 3000, {"scope": "line", "paragraph": 0, "name": "Alex", "source": "user"})
    with pytest.raises(feed.HTTPError) as e:
        ba.apply(preview=p)
    assert e.value.code == 409
    p = ba.preview()
    (ba.data / "config.json").write_text(json.dumps({"speakers": {"allowed": ["Noah", "Alex"],
                                                                  "match_threshold": 0.999}}))
    with pytest.raises(feed.HTTPError) as e:
        ba.apply(preview=p)
    assert e.value.code == 409


def test_apply_needs_the_previews_ids_and_digest(ba):
    populate(ba)
    p = ba.preview()
    for extra in ({"ids": None, "digest": p["digest"]}, {"ids": p["ids"], "digest": None}, {"ids": ["x"], "digest": "d"}):
        with pytest.raises(feed.HTTPError) as e:
            feed.reassign_speaker(DEV, 1000, {"scope": "all", "tag": "Speaker B", "name": "Noah",
                                              "source": "user", **extra})
        assert e.value.code == 400
    with pytest.raises(feed.HTTPError) as e:
        ba.apply(preview={**p, "ids": p["ids"][:1]})
    assert e.value.code == 409


def test_a_voice_the_voiceprint_names_better_than_the_tapped_voice_is_left_alone(ba):
    ba.enroll("Noah", unit([1, 0, 0, 0]))
    # auto-labelled Noah; similarity to the tapped voice ~0.66, to Noah's voiceprint ~0.75
    mine = ba.conv(2000, "2026-11-08", [("Noah", "my own words")],
                   [("Noah", [0.75, 0.66, 0, 0], {"labeled_by": "match", "label": "Noah"})])[0]
    unnamed = ba.conv(2100, "2026-11-08", [("Speaker X", "unnamed voice")], [("Speaker X", [0.5, 1, 0, 0], {})])[0]
    # labelled Noah, but nearer the tapped voice than the voiceprint: eligible
    nearer = ba.conv(2200, "2026-11-08", [("Noah", "mislabelled")],
                     [("Noah", [0.2, 1, 0, 0], {"labeled_by": "match", "label": "Noah"})])[0]
    out = ba.preview("Alex")
    assert out["ids"] == sorted([unnamed, nearer]) and mine not in out["ids"]
    assert out["skipped"] == 1
    ba.apply("Alex")
    assert "**Noah:** my own words" in raw(ba)["2026-11-08.md"]
    assert "**Alex:** mislabelled" in raw(ba)["2026-11-08.md"]


def test_the_name_being_corrected_stays_eligible(ba):
    ba.enroll("Noah", unit([1, 0, 0, 0]))
    ba.con.execute("UPDATE speaker_clusters SET tag = 'Noah' WHERE id = ?", (ba.tapped,))
    ba.con.commit()
    v = ba.conv(2000, "2026-11-08", [("Noah", "same wrong name")],
                [("Noah", [0.75, 0.66, 0, 0], {"labeled_by": "match", "label": "Noah"})])[0]
    out = feed.bulk_preview(DEV, 1000, "Noah", "Alex", "user")
    assert out["ids"] == [v] and out["skipped"] == 0


def test_a_hand_answered_or_unenrolled_label_is_not_judged_by_voiceprint(ba):
    ba.enroll("Noah", unit([1, 0, 0, 0]))
    # a label with no voiceprint (Alex has none) is not second-guessed
    v = ba.conv(2000, "2026-11-08", [("Alex", "no voiceprint yet")],
                [("Alex", [0.2, 1, 0, 0], {"labeled_by": "match", "label": "Alex"})])[0]
    assert ba.preview("Noah")["ids"] == [v]


def test_digest_changes_when_the_relabelled_lines_move_but_words_do_not():
    from pendant_direct import feed
    a = [{"id": 3, "paragraphs": [1, 3], "words": 10}]
    b = [{"id": 3, "paragraphs": [1, 5], "words": 10}]
    assert feed.bulk_digest(a) != feed.bulk_digest(b)
    assert feed.bulk_digest(a) == feed.bulk_digest([{"id": 3, "paragraphs": [3, 1], "words": 10}])
