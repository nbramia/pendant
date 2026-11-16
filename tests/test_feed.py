"""pendant_direct.feed: daily-file parsing and the authenticated JSON endpoints."""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from pendant_direct import feed

DAILY = """---
date: 2026-11-08
source: pendant
type: transcript
---
# Sunday, November 8, 2026

Intro line.

## 08:30 — DEV_20261108_083055 (0m52s) · A1B2C3D4E5F60718

[08:30] Are you able to hear me?
[08:31] Second paragraph here.

---

## 9:05 — DEV_20261108_090500 (12m03s) · 0F1E2D3C4B5A6978

Meeting text.

---
"""


def test_parse_daily_sections():
    convs = feed.parse_daily(DAILY)
    assert [c["time"] for c in convs] == ["08:30", "9:05"]
    assert convs[0]["device"] == "A1B2C3D4E5F60718"  # old files: hex ID after the dot
    assert convs[0]["duration_s"] == 52
    assert convs[0]["text"] == "[08:30] Are you able to hear me?\n[08:31] Second paragraph here."
    assert convs[1]["duration_s"] == 12 * 60 + 3
    assert convs[1]["words"] == 2
    assert convs[0]["words"] == 9, "[HH:MM] stamps are not words"


def test_parse_daily_ignores_preamble():
    assert feed.parse_daily("# Title\n\nno sections\n") == []


@pytest.fixture()
def server(tmp_path, monkeypatch):
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "2026-11-08.md").write_text(DAILY)
    monkeypatch.setenv("PENDANT_NOTES_DIR", str(notes))
    monkeypatch.setenv("PENDANT_DATA", str(tmp_path / "data"))
    srv = feed.make_server(0, "tok")
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def get(url, token="tok"):
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req) as r:
        return r.status, json.loads(r.read())


def test_requires_token(server):
    with pytest.raises(urllib.error.HTTPError) as e:
        get(server + "/pendant/feed/days", token="wrong")
    assert e.value.code == 401


def test_days_and_day(server):
    code, days = get(server + "/pendant/feed/days")
    assert code == 200
    assert days["days"][0]["date"] == "2026-11-08"
    assert days["days"][0]["conversations"] == 2
    code, day = get(server + "/pendant/feed/day/2026-11-08")
    assert len(day["conversations"]) == 2
    assert len(day["hourly_minutes"]) == 24
    assert day["talk_minutes"] == pytest.approx((52 + 723) / 60, abs=0.1)


def test_missing_day_is_empty_and_bad_date_rejected(server):
    _, day = get(server + "/pendant/feed/day/2026-01-01")
    assert day["conversations"] == []
    with pytest.raises(urllib.error.HTTPError) as e:
        get(server + "/pendant/feed/day/2026-13-01")
    assert e.value.code == 400


ZERO_LLM = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0,
            "cached_tokens": 0, "usd": 0.0}


def test_status_without_db(server):
    _, st = get(server + "/pendant/feed/status")
    assert st == {"pendants": [], "last_upload": None, "untranscribed_minutes": 0.0,
                  "names": {"0F1E2D3C4B5A6978": "old", "A1B2C3D4E5F60718": "new"},
                  "llm_today": ZERO_LLM, "llm_7d": ZERO_LLM}


# ---------------------------------------------------------------------------
# titled headings with pendant names, DB enrichment

NEW_DAILY = """# Monday

## 16:15 — Fix The Garage Door (26m03s) · old

[16:15] **Noah:** we should fix it

[16:16] **Speaker A:** okay then

---

## 17:00 — Untitled Chat (0m30s) · blue spare

[17:00] hi

---
"""


def test_parse_new_headings_with_names_and_speakers():
    convs = feed.parse_daily(NEW_DAILY)
    assert [(c["title"], c["device"]) for c in convs] == [("Fix The Garage Door", "old"),
                                                         ("Untitled Chat", "blue spare")]
    assert convs[0]["heading"] == "16:15 — Fix The Garage Door (26m03s) · old"
    assert convs[0]["words"] == 6, "speaker labels are not words"


def make_db(tmp_path, monkeypatch):
    from pendant_direct import process
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    monkeypatch.setattr(process, "DB", data / "pages.db")
    con = process.db()
    return con


def test_day_payload_joins_conversations(tmp_path, monkeypatch):
    con = make_db(tmp_path, monkeypatch)
    con.execute("INSERT INTO conversations (device, start_ms, end_ms, audio_s, day, pendant, title, overview, "
                "action_items, category, people, notable, raw_heading, event_file, clip) "
                "VALUES ('0F1E2D3C4B5A6978', 1, 2, 1563, '2026-11-09', 'old', 'Fix The Garage Door', "
                "'They planned it.', '[\"Buy hinge\"]', 'home', '[]', 1, "
                "'16:15 — Fix The Garage Door (26m03s) · old', 'E - fix - F00D-1.md', '/x')")
    con.commit()
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "2026-11-09.md").write_text(NEW_DAILY)
    monkeypatch.setenv("PENDANT_NOTES_DIR", str(notes))
    monkeypatch.setenv("PENDANT_DATA", str(tmp_path / "data"))
    day = feed.day_payload(feed.dt.date(2026, 11, 9))
    a, b = day["conversations"]
    assert a["overview"] == "They planned it." and a["action_items"] == ["Buy hinge"]
    assert a["notable"] is True and a["device"] == "old" and a["device_id"] == "0F1E2D3C4B5A6978"
    assert a["event"] == "E - fix - F00D-1"
    assert b["overview"] == "" and b["action_items"] == [] and b["notable"] is False
    assert "heading" not in a


def test_day_payload_lists_each_conversations_stars(tmp_path, monkeypatch):
    con = make_db(tmp_path, monkeypatch)
    start = int(feed.dt.datetime(2026, 11, 9, 16, 15, tzinfo=feed.TZ).timestamp() * 1000)
    con.execute("INSERT INTO conversations (device, start_ms, end_ms, audio_s, day, pendant, title, notable, "
                "raw_heading) VALUES ('0F1E2D3C4B5A6978', ?, ?, 1563, '2026-11-09', 'old', "
                "'Fix The Garage Door', 1, '16:15 — Fix The Garage Door (26m03s) · old')",
                (start, start + 1_563_000))
    con.executemany("INSERT INTO stars VALUES (?, ?, '0F1E2D3C4B5A6978', ?, 't')",
                    [("0F1E2D3C4B5A6978", start + 125_000, start), ("A1B2C3D4E5F60718", start + 60_000, start)])
    con.execute("INSERT INTO stars (device, ts_ms) VALUES ('0F1E2D3C4B5A6978', ?)", (start + 9_000_000,))  # pending
    con.commit()
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "2026-11-09.md").write_text(NEW_DAILY.replace("[16:15] ", "[16:15] ⭐ ", 1))
    monkeypatch.setenv("PENDANT_NOTES_DIR", str(notes))
    monkeypatch.setenv("PENDANT_DATA", str(tmp_path / "data"))
    a, b = feed.day_payload(feed.dt.date(2026, 11, 9))["conversations"]
    assert a["stars"] == ["16:16", "16:17"] and b["stars"] == []
    assert a["words"] == feed.parse_daily(NEW_DAILY)[0]["words"], "the star is not a word"


def test_day_payload_old_db_without_conversations_table(tmp_path, monkeypatch):
    import sqlite3
    data = tmp_path / "data"
    data.mkdir()
    sqlite3.connect(data / "pages.db").execute("CREATE TABLE chunks (ts_ms INT)").connection.commit()
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "2026-11-08.md").write_text(DAILY)
    monkeypatch.setenv("PENDANT_NOTES_DIR", str(notes))
    monkeypatch.setenv("PENDANT_DATA", str(data))
    convs = feed.day_payload(feed.dt.date(2026, 11, 8))["conversations"]
    assert [c["device"] for c in convs] == ["A1B2C3D4E5F60718", "0F1E2D3C4B5A6978"]
    assert all(c["overview"] == "" and c["notable"] is False for c in convs)


# ---------------------------------------------------------------------------
# speaker endpoints

def post(url, body, token="tok"):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req) as r:
        return r.status, json.loads(r.read())


@pytest.fixture()
def vad(monkeypatch):
    """Mocked Silero VAD: by default the whole span reads as speech. Tests set
    vad["spans"] = fn(chunk, sr) -> [(start_s, end_s)] and read vad["calls"]."""
    from pendant_direct import speakers
    state = {"calls": 0, "spans": lambda chunk, sr: [(0.0, len(chunk) / sr)]}

    def fake(chunk, sr):
        state["calls"] += 1
        return state["spans"](chunk, sr)
    monkeypatch.setattr(speakers, "speech_timestamps", fake)
    return state


@pytest.fixture()
def spk(tmp_path, monkeypatch, server, vad):
    """Server with three conversations' speaker clusters and a real (synthetic) clip."""
    import numpy as np
    from synth_audio import write_tone
    con = make_db(tmp_path, monkeypatch)
    clip = tmp_path / "data" / "clips" / "c.opus"
    clip.parent.mkdir(parents=True)
    write_tone(clip, seconds=3.0)
    me = np.array([1, 0, 0, 0], dtype=np.float32)
    rows = []
    for k in range(3):   # the wearer talks most in each conversation
        rows.append(("0F1E2D3C4B5A6978", 1000 * k, "Speaker A", (me + 0.05 * k).astype(np.float32), 20.0))
        rows.append(("0F1E2D3C4B5A6978", 1000 * k, "Speaker B", np.array([0, 1, 0, 0], np.float32) + k, 4.0))
    for dev, start, tag, emb, speech in rows:
        con.execute("INSERT INTO speaker_clusters (device, start_ms, tag, label, embedding, clip, start_s, end_s, "
                    "speech_s, excerpt, labeled_by, created_at) VALUES (?,?,?,NULL,?,?,0.5,2.5,?,?,NULL,0)",
                    (dev, start, tag, emb.tobytes(), str(clip), speech, f"{tag} has some words"))
    con.commit()
    return server


def test_speakers_roster_and_unknown_with_suggestion(spk):
    _, roster = get(spk + "/pendant/feed/speakers")
    assert roster == {"enrolled": [], "allowed": ["Noah"]}
    _, first = get(spk + "/pendant/feed/speakers/unknown")
    assert len(first["speakers"]) == 5          # at most 5 new clusters are voice-checked per request
    _, unk = get(spk + "/pendant/feed/speakers/unknown")
    sp = unk["speakers"]
    assert len(sp) == 6
    assert [s["suggested"] for s in sp[:3]] == ["Noah"] * 3
    assert all(s["suggested"] is None for s in sp[3:])
    assert {s["tag"] for s in sp[:3]} == {"Speaker A"}
    s = sp[0]
    assert s["duration_s"] == 2.0 and s["device"] == "old" and s["excerpt"] == "Speaker A has some words"
    assert s["when"] == 2000 + 500


def test_clip_endpoint_serves_wav(spk):
    req = urllib.request.Request(spk + "/pendant/feed/speakers/clip/1", headers={"Authorization": "Bearer tok"})
    with urllib.request.urlopen(req) as r:
        assert r.headers["Content-Type"] == "audio/wav"
        data = r.read()
    assert data[:4] == b"RIFF" and data[8:12] == b"WAVE"
    assert abs((len(data) - 44) / 2 - 2.0 * 16000) < 1600
    with pytest.raises(urllib.error.HTTPError) as e:
        get(spk + "/pendant/feed/speakers/clip/999")
    assert e.value.code == 404


def test_label_consent_gate_and_enrollment(spk, tmp_path):
    with pytest.raises(urllib.error.HTTPError) as e:
        post(spk + "/pendant/feed/speakers/2/label", {"name": "Alex"})
    assert e.value.code == 403
    assert not (tmp_path / "data" / "voiceprints").exists()
    with pytest.raises(urllib.error.HTTPError) as e:
        post(spk + "/pendant/feed/speakers/1/label", {"name": "Noah"}, token="bad")
    assert e.value.code == 401
    code, out = post(spk + "/pendant/feed/speakers/1/label", {"name": "noah"})
    assert code == 200 and out == {"id": 1, "name": "Noah", "count": 1, "action": "correct", "relabeled": True}
    assert (tmp_path / "data" / "voiceprints" / "Noah.npz").exists()
    with pytest.raises(urllib.error.HTTPError) as e:
        post(spk + "/pendant/feed/speakers/1/label", {"name": "Noah"})
    assert e.value.code == 409
    _, roster = get(spk + "/pendant/feed/speakers")
    assert roster["enrolled"] == [{"name": "Noah", "count": 1, "recognized": 1}]
    # the wearer's other clusters now match the voiceprint: still asked about, as "Is this Noah?"
    # (most confident last); no more wearer suggestions
    _, unk = get(spk + "/pendant/feed/speakers/unknown")
    sp = unk["speakers"]
    assert 1 not in [s["id"] for s in sp]
    assert all(s["suggested"] is None for s in sp)
    assert {s["id"]: s["current_label"] for s in sp if s["tag"] == "Speaker A"} == {3: "Noah", 5: "Noah"}
    assert all(s["current_label"] is None for s in sp if s["tag"] == "Speaker B")
    assert [s["confidence"] for s in sp] == sorted(s["confidence"] for s in sp)
    assert sp[-1]["tag"] == "Speaker A" and sp[-1]["confidence"] > 0.9


def test_label_allowed_after_config_change(spk, tmp_path):
    (tmp_path / "data" / "config.json").write_text(json.dumps({"speakers": {"allowed": ["Noah", "Alex"]}}))
    code, out = post(spk + "/pendant/feed/speakers/2/label", {"name": "Alex"})
    assert code == 200 and out["name"] == "Alex"


def test_dismiss_and_unknown_ids(spk):
    code, _ = post(spk + "/pendant/feed/speakers/2/dismiss", {})
    assert code == 200
    _, unk = get(spk + "/pendant/feed/speakers/unknown")
    assert 2 not in [s["id"] for s in unk["speakers"]]
    with pytest.raises(urllib.error.HTTPError) as e:
        post(spk + "/pendant/feed/speakers/999/label", {"name": "Noah"})
    assert e.value.code == 404


def _unknown_ids(spk):
    get(spk + "/pendant/feed/speakers/unknown")      # checks up to 5 new clusters per call
    _, unk = get(spk + "/pendant/feed/speakers/unknown")
    return [s["id"] for s in unk["speakers"]]


def test_voice_check_filters_noise_and_short_text_and_caches(spk, vad, tmp_path):
    import sqlite3
    con = sqlite3.connect(tmp_path / "data" / "pages.db")
    con.execute("UPDATE speaker_clusters SET excerpt = 'too few words' WHERE id = 6")   # 3 words
    con.commit()
    # checks run newest first (ids 5, 3, 4, 1, 2): the first two (5 and 3) read as 30% speech
    n = {"i": 0}

    def spans(chunk, sr):
        n["i"] += 1
        return [(0.0, 0.6)] if n["i"] in (1, 2) else [(0.0, len(chunk) / sr)]
    vad["spans"] = spans
    ids = _unknown_ids(spk)
    assert sorted(ids) == [1, 2, 4]
    rows = dict((r[0], r[1]) for r in con.execute("SELECT id, voiced FROM speaker_clusters"))
    assert rows[5] == pytest.approx(0.3, abs=0.05) and rows[3] == pytest.approx(0.3, abs=0.05)
    assert rows[4] == pytest.approx(1.0)
    assert rows[6] is None                          # too few words: never even audio-checked
    calls = vad["calls"]
    assert calls == 5
    assert sorted(_unknown_ids(spk)) == [1, 2, 4]   # stored results are not recomputed
    assert vad["calls"] == calls


def test_voice_window_is_the_densest_stretch(tmp_path, monkeypatch):
    import numpy as np
    from pendant_direct import speakers
    audio = np.zeros(20 * 16000, dtype=np.float32)
    # speech only between 14 and 20 s of a 20 s span
    s, e, v = speakers.best_voice_window(audio, 0.0, 20.0, vad=lambda c, sr: [(14.0, 20.0)])
    assert 3.0 <= e - s <= 12.0 and s >= 13.5 and e == pytest.approx(20.0, abs=0.2) and v >= 0.9
    s, e, v = speakers.best_voice_window(audio, 0.0, 20.0, vad=lambda c, sr: [])
    assert v == 0.0 and e - s <= 12.0


def test_stored_window_is_served(spk, vad, tmp_path):
    import sqlite3
    vad["spans"] = lambda chunk, sr: [(0.5, 1.5)]
    _unknown_ids(spk)
    con = sqlite3.connect(tmp_path / "data" / "pages.db")
    s, e = con.execute("SELECT start_s, end_s FROM speaker_clusters WHERE id = 1").fetchone()
    assert e - s <= 2.0 + 1e-6
    req = urllib.request.Request(spk + "/pendant/feed/speakers/clip/1", headers={"Authorization": "Bearer tok"})
    with urllib.request.urlopen(req) as r:
        data = r.read()
    assert abs((len(data) - 44) / 2 - (e - s) * 16000) < 1600


def test_noise_endpoint(spk, tmp_path):
    code, out = post(spk + "/pendant/feed/speakers/1/noise", {})
    assert code == 200 and out == {"id": 1, "noise": True}
    assert 1 not in _unknown_ids(spk)
    with pytest.raises(urllib.error.HTTPError) as e:
        post(spk + "/pendant/feed/speakers/1/label", {"name": "Noah", "source": "user"})
    assert e.value.code == 409
    assert not (tmp_path / "data" / "voiceprints").exists()
    with pytest.raises(urllib.error.HTTPError) as e:
        post(spk + "/pendant/feed/speakers/999/noise", {})
    assert e.value.code == 404


def test_user_source_adds_name_to_allowed_and_enrolls(spk, tmp_path):
    cfg = tmp_path / "data" / "config.json"
    with pytest.raises(urllib.error.HTTPError) as e:     # no source: gate holds
        post(spk + "/pendant/feed/speakers/2/label", {"name": "Alex"})
    assert e.value.code == 403
    with pytest.raises(urllib.error.HTTPError) as e:     # other sources too
        post(spk + "/pendant/feed/speakers/2/label", {"name": "Alex", "source": "calendar"})
    assert e.value.code == 403
    assert not cfg.exists()
    with pytest.raises(urllib.error.HTTPError) as e:
        post(spk + "/pendant/feed/speakers/2/label", {"name": "Bad1!", "source": "user"})
    assert e.value.code == 400
    code, out = post(spk + "/pendant/feed/speakers/2/label", {"name": "  mary-jane  o'neil ", "source": "user"})
    assert code == 200 and out["name"] == "Mary-Jane O'Neil" and out["count"] == 1
    assert json.loads(cfg.read_text())["speakers"]["allowed"] == ["Noah", "Mary-Jane O'Neil"]
    assert (tmp_path / "data" / "voiceprints" / "Mary-Jane O'Neil.npz").exists()
    _, roster = get(spk + "/pendant/feed/speakers")
    assert roster["allowed"] == ["Noah", "Mary-Jane O'Neil"]


def test_allow_name_unit(tmp_path):
    from pendant_direct import config
    assert config.allow_name(tmp_path, "alex") == "Alex"
    assert config.allow_name(tmp_path, "ALEX") == "Alex"      # already present: no duplicate
    assert json.loads((tmp_path / "config.json").read_text())["speakers"]["allowed"] == ["Noah", "Alex"]
    for bad in ("", "   ", "x" * 41, "A1", "-Bob"):
        with pytest.raises(ValueError):
            config.allow_name(tmp_path, bad)


def test_old_database_is_migrated(tmp_path, monkeypatch):
    import sqlite3
    from pendant_direct import process
    data = tmp_path / "data"
    data.mkdir()
    con = sqlite3.connect(data / "pages.db")
    con.execute("CREATE TABLE speaker_clusters (id INTEGER PRIMARY KEY, device TEXT, start_ms INT, tag TEXT, "
                "label TEXT, embedding BLOB, clip TEXT, start_s REAL, end_s REAL, speech_s REAL, excerpt TEXT, "
                "labeled_by TEXT, created_at REAL)")
    con.execute("INSERT INTO speaker_clusters (device, tag) VALUES ('d', 'Speaker A')")
    con.commit()
    con.close()
    monkeypatch.setattr(process, "DB", data / "pages.db")
    con = process.db()
    assert con.execute("SELECT tag, voiced, noise FROM speaker_clusters").fetchall() == [("Speaker A", None, 0)]


def test_speakers_unknown_without_db(server):
    _, unk = get(server + "/pendant/feed/speakers/unknown")
    assert unk == {"speakers": []}


def test_search_terms_and_filters(server, tmp_path):
    from pendant_direct import feed
    _, r = get(server + "/pendant/feed/search?q=hear+able&from=2026-11-01&to=2026-11-30")
    assert r["count"] == 1 and r["results"][0]["time"] == "08:30"
    assert "able to hear me" in r["results"][0]["snippet"]
    assert "[08:30]" not in r["results"][0]["snippet"]
    _, r = get(server + "/pendant/feed/search?q=&from=2026-11-01&to=2026-11-30&min_minutes=5")
    assert [x["time"] for x in r["results"]] == ["9:05"]
    _, r = get(server + "/pendant/feed/search?q=meeting&from=2026-11-01&to=2026-11-30&device=nope")
    assert r["count"] == 0
    with pytest.raises(urllib.error.HTTPError) as e:
        get(server + "/pendant/feed/search?q=x&from=bad")
    assert e.value.code == 400


def test_snippet_centres_on_match():
    from pendant_direct import feed
    text = "[09:00] " + "word " * 100 + "**Alex:** the needle is here " + "tail " * 50
    s = feed.snippet(text, ["needle"])
    assert "needle" in s and s.startswith("…") and "**Alex:**" in s


def test_snippet_never_cuts_a_speaker_label():
    from pendant_direct import feed
    text = "[09:00] " + "x" * 30 + " **Alex Stone:** " + "y" * 200
    for width in range(20, 80):
        s = feed.snippet(text, ["yyy"], width=width).strip("…")
        assert s.count("**") in (0, 2), (width, s)


def test_allow_name_refuses_to_overwrite_an_unreadable_config(tmp_path):
    from pendant_direct import config
    (tmp_path / "config.json").write_text("{ not json")
    with pytest.raises(ValueError):
        config.allow_name(tmp_path, "Jordan")
    assert (tmp_path / "config.json").read_text() == "{ not json"


def _people_server(tmp_path, monkeypatch, files):
    import datetime as dt
    notes = tmp_path / "pnotes"
    notes.mkdir()
    today = dt.datetime.now(feed.TZ).date()
    for back, body in files.items():
        (notes / f"{today - dt.timedelta(days=back)}.md").write_text(body)
    monkeypatch.setenv("PENDANT_NOTES_DIR", str(notes))
    monkeypatch.setenv("PENDANT_DATA", str(tmp_path / "data"))
    srv = feed.make_server(0, "tok")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}", today


PEOPLE_DAY = """# Day

## 10:00 — Planning (30m00s) · Old

[10:00] **Noah:** Morning.
[10:02] ⭐ **Kai:** Starred paragraph, two minutes.
[10:04] **Speaker A:** Generic tag.
[10:05] **Kai:** Long one, ends at the conversation end so capped at ten.
[10:30] untagged tail

---

## 11:00 — Lunch (10m00s) · New

[11:00] **kai:** Case differs.
[11:05] **Riley:** Five minutes.

---
"""


def test_people_aggregation(tmp_path, monkeypatch):
    srv, url, today = _people_server(tmp_path, monkeypatch, {0: PEOPLE_DAY, 2: PEOPLE_DAY})
    try:
        _, rows = get(url + "/pendant/feed/people?days=1")
        by = {r["name"]: r for r in rows}
        assert "Noah" not in by and not any(n.startswith("Speaker") for n in by)
        # Kai: 2 (starred) + 10 (capped; next stamp is 25 min away) + 5 (lunch, 11:00-11:05)
        assert by["Kai"]["talk_minutes"] == 17.0 and by["Kai"]["conversations"] == 2
        assert by["Riley"]["talk_minutes"] == 5.0
        assert [r["name"] for r in rows] == ["Kai", "Riley"]
        assert abs(sum(r["share"] for r in rows) - 1) < 0.01
        assert by["Riley"]["last_heard"].startswith(f"{today}T11:05")
        _, rows = get(url + "/pendant/feed/people?days=7")  # multi-day
        assert {r["name"]: r for r in rows}["Kai"]["talk_minutes"] == 34.0
        _, rows = get(url + "/pendant/feed/people")  # default 7
        assert rows[0]["conversations"] == 4
        _, rows = get(url + "/pendant/feed/people?days=2")  # day 2 back is outside
        assert rows[0]["talk_minutes"] == 17.0
    finally:
        srv.shutdown()


def test_person_conversations_and_errors(tmp_path, monkeypatch):
    srv, url, today = _people_server(tmp_path, monkeypatch, {0: PEOPLE_DAY, 40: PEOPLE_DAY})
    try:
        _, rows = get(url + "/pendant/feed/people/Kai")
        assert [(r["time"], r["their_minutes"], r["total_minutes"]) for r in rows] == [
            ("11:00", 5.0, 10.0), ("10:00", 12.0, 30.0)]
        assert rows[0]["date"] == str(today) and rows[0]["title"] == "Lunch" and rows[0]["device"] == "New"
        _, rows = get(url + "/pendant/feed/people/Kai?days=90")
        assert len(rows) == 4
        _, rows = get(url + "/pendant/feed/people/riley%20?days=1")  # URL-decoded, case-insensitive
        assert len(rows) == 1
        for bad in ("/pendant/feed/people/Nobody", "/pendant/feed/people/Speaker%20A"):
            with pytest.raises(urllib.error.HTTPError) as e:
                get(url + bad)
            assert e.value.code == 404
        for bad in ("/pendant/feed/people?days=0", "/pendant/feed/people?days=91",
                    "/pendant/feed/people?days=x", "/pendant/feed/people/Kai?days=0"):
            with pytest.raises(urllib.error.HTTPError) as e:
                get(url + bad)
            assert e.value.code == 400
    finally:
        srv.shutdown()


def test_person_turns_midnight_and_no_stamps():
    c = {"time": "23:50", "duration_s": 20 * 60, "text": "[23:55] **Ann:** a\n[00:05] **Bob:** b\nno stamp"}
    t = feed.person_turns(c)
    assert t["ann"]["minutes"] == 10.0 and t["bob"]["minutes"] == 5.0  # Bob: 00:05 to the end at 00:10
    assert feed.person_turns({"time": "09:00", "duration_s": 60, "text": "plain"}) == {}


def test_paragraph_end_is_capped_at_conversation_end():
    c = {"time": "10:00", "duration_s": 5 * 60, "text": "[10:04] **Ann:** a\n[10:10] **Bob:** b"}
    t = feed.person_turns(c)
    assert t["ann"]["minutes"] == 1.0   # not 6: the next stamp is past the 10:05 end
    assert t["bob"]["minutes"] == 0.0


def test_names_merge_case_insensitively(tmp_path, monkeypatch):
    day = """## 10:00 — A (10m00s) · Old

[10:00] **Kai:** x
[10:02] **kai:** y

---

## 11:00 — B (10m00s) · Old

[11:00] **Kai:** z

---
"""
    srv, url, _ = _people_server(tmp_path, monkeypatch, {0: day})
    try:
        _, rows = get(url + "/pendant/feed/people?days=1")
        assert len(rows) == 1 and rows[0]["name"] == "Kai"   # most common spelling
        assert rows[0]["conversations"] == 2 and rows[0]["talk_minutes"] == 20.0
        _, conv = get(url + "/pendant/feed/people/KAI")
        assert [r["their_minutes"] for r in conv] == [10.0, 10.0]
    finally:
        srv.shutdown()


def test_snippet_keeps_a_match_inside_a_label():
    from pendant_direct import feed
    assert "alex" in feed.snippet("**Alex Stone:** hello", ["alex"], width=10).lower()
    text = "[09:00] " + "x " * 40 + "**Alex Stone:** " + "y " * 40
    for width in range(5, 90):
        for term in ("alex", "swift", "yy", "x x"):
            s = feed.snippet(text, [term], width=width)
            if term in text.lower():
                assert term in s.lower(), (width, term, s)
            assert s.strip("…").count("**") in (0, 2), (width, term, s)


def test_voices_with_no_audio_leave_the_question_queue(spk, tmp_path):
    get(spk + "/pendant/feed/speakers/unknown")           # first request voice-checks the clips
    _, unk = get(spk + "/pendant/feed/speakers/unknown")
    assert len(unk["speakers"]) == 6
    (tmp_path / "data" / "clips" / "c.opus").unlink()      # retention removed the audio
    _, unk = get(spk + "/pendant/feed/speakers/unknown")
    assert unk["speakers"] == []


# --- conversation audio ------------------------------------------------------

def _audio_get(url, token="tok", rng=None):
    h = {"Authorization": f"Bearer {token}"}
    if rng:
        h["Range"] = rng
    with urllib.request.urlopen(urllib.request.Request(url, headers=h)) as r:
        return r.status, dict(r.headers), r.read()


@pytest.fixture()
def conv_audio(tmp_path, monkeypatch, server):
    from synth_audio import write_tone
    con = make_db(tmp_path, monkeypatch)
    clips = tmp_path / "data" / "clips"
    clips.mkdir(parents=True)
    write_tone(clips / "D_1.opus", seconds=3.0)
    for start, clip in ((1000, str(clips / "D_1.opus")), (2000, str(clips / "gone.opus")), (3000, None)):
        con.execute("INSERT INTO conversations (device, start_ms, clip, day, raw_heading) VALUES (?,?,?,?,?)",
                    ("D", start, clip, "2026-11-08", f"h{start}"))
    con.commit()
    return server + "/pendant/feed/conversations/D/{}/audio"


def test_audio_full_and_ranges(conv_audio):
    url = conv_audio.format(1000)
    code, h, body = _audio_get(url)
    assert code == 200 and h["Content-Type"] == "audio/mp4" and h["Accept-Ranges"] == "bytes"
    assert body[4:8] == b"ftyp" and int(h["Content-Length"]) == len(body)
    code, h, part = _audio_get(url, rng="bytes=100-199")
    assert code == 206 and part == body[100:200]
    assert h["Content-Range"] == f"bytes 100-199/{len(body)}"
    code, h, part = _audio_get(url, rng="bytes=-50")
    assert code == 206 and part == body[-50:]
    code, h, part = _audio_get(url, rng=f"bytes={len(body) - 10}-")
    assert code == 206 and part == body[-10:]
    code, _h, part = _audio_get(url, rng="bytes=0-99999999")
    assert code == 206 and part == body


def test_audio_unsatisfiable_range_and_cache(conv_audio, tmp_path):
    url = conv_audio.format(1000)
    with pytest.raises(urllib.error.HTTPError) as e:
        _audio_get(url, rng="bytes=99999999-")
    assert e.value.code == 416 and e.value.headers["Content-Range"].startswith("bytes */")
    cached = tmp_path / "data" / "audio_cache" / "D_1.m4a"
    assert cached.is_file()
    mtime = cached.stat().st_mtime_ns
    _audio_get(url)
    assert cached.stat().st_mtime_ns == mtime   # served from the cache, not re-encoded


def test_audio_missing_is_404(conv_audio):
    for start in (2000, 3000, 4000):   # clip file gone, no clip recorded, no such conversation
        with pytest.raises(urllib.error.HTTPError) as e:
            _audio_get(conv_audio.format(start))
        assert e.value.code == 404


def test_audio_requires_token(conv_audio):
    with pytest.raises(urllib.error.HTTPError) as e:
        _audio_get(conv_audio.format(1000), token="wrong")
    assert e.value.code == 401


def test_parse_range():
    from pendant_direct.audio import parse_range
    assert parse_range(None, 100) is None
    assert parse_range("bytes=0-9", 100) == (0, 9)
    assert parse_range("bytes=90-", 100) == (90, 99)
    assert parse_range("bytes=-10", 100) == (90, 99)
    assert parse_range("bytes=-500", 100) == (0, 99)
    assert parse_range("bytes=100-", 100) == "bad"
    assert parse_range("bytes=5-2", 100) == "bad"
    assert parse_range("bytes=0-1,5-6", 100) is None


def test_transcode_survives_trailing_garbage(tmp_path):
    from synth_audio import write_tone
    from pendant_direct import audio
    src = tmp_path / "bad.opus"
    write_tone(src, seconds=3.0)
    with open(src, "ab") as f:
        f.write(b"OggS" + b"\xff" * 60)   # a page header with a bad body, like the real clips' tails
    dst = tmp_path / "out.m4a"
    audio.transcode(src, dst)
    import av
    with av.open(str(dst)) as c:
        assert c.streams.audio[0].codec_context.name == "aac"
        assert 2.5 < c.duration / 1e6 < 3.5
    assert not (tmp_path / "out.m4a.tmp").exists()


def test_only_one_transcode_runs_at_a_time(tmp_path, monkeypatch):
    import time
    from pendant_direct import audio
    clips = tmp_path / "clips"
    clips.mkdir()
    for n in ("a", "b", "c"):
        (clips / f"{n}.opus").write_bytes(b"x")
    state = {"now": 0, "max": 0}
    lock = threading.Lock()

    def slow(src, dst):
        with lock:
            state["now"] += 1
            state["max"] = max(state["max"], state["now"])
        time.sleep(0.15)
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(b"m4a")
        with lock:
            state["now"] -= 1

    monkeypatch.setattr(audio, "transcode", slow)
    out = []
    ts = [threading.Thread(target=lambda n=n: out.append(audio.playable(tmp_path, f"{n}.opus"))) for n in "abc"]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(out) == 3 and all(out) and state["max"] == 1


MORGAN_EVENT = json.dumps({"id": "ev", "title": "Morgan-Noah week-ahead prep", "start_ms": 0, "end_ms": 1800000,
                         "attendees": [{"name": "Noah", "email": "noah@work.example", "entity_id": None,
                                        "wearer": True},
                                       {"name": "Morgan Hale", "email": "morgan@work.example",
                                        "entity_id": "e-morgan", "wearer": False}]})


def _calendar_conv(tmp_path, start_ms=0):
    from pendant_direct import process
    con = process.db(tmp_path / "data" / "pages.db")
    for start, ev in ((start_ms, MORGAN_EVENT), (1000, "")):    # 1000: no event matched
        con.execute("INSERT INTO conversations (device, start_ms, end_ms, day, raw_heading, calendar_event) "
                    "VALUES ('0F1E2D3C4B5A6978', ?, ?, '2026-11-09', 'x', ?)", (start, start + 1, ev))
    con.commit()


def test_speaker_suggest_offers_the_one_attendee_left(spk, tmp_path):
    _calendar_conv(tmp_path)
    _, out = get(spk + "/pendant/feed/conversations/0F1E2D3C4B5A6978/0/speaker/Speaker%20B/suggest")
    assert out["likely"]["name"] == "Morgan Hale" and out["likely"]["entity_id"] == "e-morgan"
    assert out["likely"]["source"] == "calendar" and out["likely"]["event"] == "Morgan-Noah week-ahead prep"
    assert out["candidates"] == [] and out["event"]["title"] == "Morgan-Noah week-ahead prep"
    # no calendar match for that conversation: nothing suggested, the picker is as before
    _, none = get(spk + "/pendant/feed/conversations/0F1E2D3C4B5A6978/1000/speaker/Speaker%20B/suggest")
    assert none["likely"] is None and none["candidates"] == [] and none["event"] is None
    with pytest.raises(urllib.error.HTTPError) as e:
        get(spk + "/pendant/feed/conversations/0F1E2D3C4B5A6978/5/speaker/Speaker%20B/suggest")
    assert e.value.code == 404


def test_voice_cards_carry_the_likely_attendee_and_confirming_links_it(spk, tmp_path):
    _calendar_conv(tmp_path)
    get(spk + "/pendant/feed/speakers/unknown")                 # first request voice-checks 5 of 6
    _, unk = get(spk + "/pendant/feed/speakers/unknown")
    by = {(s["conversation_start"], s["tag"]): s for s in unk["speakers"]}
    card = by[(0, "Speaker B")]
    assert card["likely"]["name"] == "Morgan Hale"
    assert by[(1000, "Speaker B")]["likely"] is None and by[(1000, "Speaker B")]["candidates"] == []
    # nothing was named by the suggestion
    from pendant_direct import process
    con = process.db(tmp_path / "data" / "pages.db")
    assert con.execute("SELECT label FROM speaker_clusters WHERE id=?", (card["id"],)).fetchone() == (None,)
    # confirming is an ordinary hand label linked to the LifeOS person
    lk = card["likely"]
    code, out = post(spk + f"/pendant/feed/speakers/{card['id']}/label",
                     {"name": lk["label"], "source": "user", "entity_id": lk["entity_id"]})
    assert code == 200 and out["name"] == "Morgan Hale"
    sp = json.loads((tmp_path / "data" / "config.json").read_text())["speakers"]
    assert "Morgan Hale" in sp["allowed"] and sp["people"]["Morgan Hale"] == "e-morgan"
    assert con.execute("SELECT label, labeled_by FROM speaker_clusters WHERE id=?", (card["id"],)).fetchone() == \
        ("Morgan Hale", "user")


def _likely_present(monkeypatch, answer):
    """Fakes LifeOS's likely-present endpoint; returns the list of calls made."""
    from pendant_direct import attendance
    calls = []

    def fake(path, params, timeout=10.0):
        calls.append((path, params))
        if path != "/api/crm/people/likely-present":
            raise RuntimeError("live LifeOS calls are blocked in tests")
        if isinstance(answer, BaseException):
            raise answer
        return answer
    monkeypatch.setattr(attendance, "_get", fake)
    return calls


ALEX_MORGAN = {"start": "s", "end": "e", "people": [
    {"person_id": "e-lex", "name": "Alex Rivera", "score": 0.8, "reasons": ["texted on my way"]},
    {"person_id": "e-morgan", "name": "Morgan Hale", "score": 0.6, "reasons": ["lives with you"]}]}


def test_speaker_suggest_adds_lifeos_maybe_people_in_order(spk, tmp_path, monkeypatch):
    _calendar_conv(tmp_path)
    calls = _likely_present(monkeypatch, ALEX_MORGAN)
    # no calendar event: the maybe people are the only hint, in LifeOS's order with its reasons
    _, out = get(spk + "/pendant/feed/conversations/0F1E2D3C4B5A6978/1000/speaker/Speaker%20B/suggest")
    assert out["likely"] is None and out["candidates"] == [] and out["event"] is None
    assert [(m["name"], m["entity_id"], m["reasons"]) for m in out["maybe"]] == \
        [("Alex Rivera", "e-lex", ["texted on my way"]), ("Morgan Hale", "e-morgan", ["lives with you"])]
    assert all(m["source"] == "lifeos" for m in out["maybe"])
    assert calls[0][1]["limit"] == 5
    # with a calendar event, its likely person keeps first place and isn't repeated as a maybe
    _, cal = get(spk + "/pendant/feed/conversations/0F1E2D3C4B5A6978/0/speaker/Speaker%20B/suggest")
    assert cal["likely"]["entity_id"] == "e-morgan"
    assert [m["name"] for m in cal["maybe"]] == ["Alex Rivera"]
    # someone named by hand in the same conversation is left out
    from pendant_direct import process
    con = process.db(tmp_path / "data" / "pages.db")
    con.execute("UPDATE speaker_clusters SET label='Alex Rivera', labeled_by='user' "
                "WHERE start_ms=1000 AND tag='Speaker A'")
    con.commit()
    _, named = get(spk + "/pendant/feed/conversations/0F1E2D3C4B5A6978/1000/speaker/Speaker%20B/suggest")
    assert [m["name"] for m in named["maybe"]] == ["Morgan Hale"]


def test_speaker_suggest_is_calendar_only_when_lifeos_fails(spk, tmp_path, monkeypatch):
    import urllib.error
    _calendar_conv(tmp_path)
    _likely_present(monkeypatch, urllib.error.HTTPError("http://lifeos/x", 404, "Not Found", None, None))
    code, out = get(spk + "/pendant/feed/conversations/0F1E2D3C4B5A6978/0/speaker/Speaker%20B/suggest")
    assert code == 200 and out["maybe"] == []
    assert out["likely"]["name"] == "Morgan Hale" and out["event"]["title"] == "Morgan-Noah week-ahead prep"


def test_speaker_suggest_is_calendar_only_when_lifeos_is_slow(spk, tmp_path, monkeypatch):
    import time
    from pendant_direct import attendance, calendar_hint
    _calendar_conv(tmp_path)
    monkeypatch.setattr(calendar_hint, "PRESENT_BUDGET_S", 0.2)
    monkeypatch.setattr(attendance, "_get", lambda *a, **k: (time.sleep(2), ALEX_MORGAN)[1])
    t0 = time.monotonic()
    code, out = get(spk + "/pendant/feed/conversations/0F1E2D3C4B5A6978/1000/speaker/Speaker%20B/suggest")
    assert code == 200 and out["maybe"] == [] and time.monotonic() - t0 < 1.5


def test_confirming_a_maybe_is_an_ordinary_hand_label(spk, tmp_path, monkeypatch):
    from pendant_direct import process, speakers
    _calendar_conv(tmp_path)
    _likely_present(monkeypatch, ALEX_MORGAN)
    con = process.db(tmp_path / "data" / "pages.db")
    cid, = con.execute("SELECT id FROM speaker_clusters WHERE start_ms=1000 AND tag='Speaker B'").fetchone()
    before = con.execute("SELECT id, label, labeled_by, match_score FROM speaker_clusters ORDER BY id").fetchall()
    _, out = get(spk + "/pendant/feed/conversations/0F1E2D3C4B5A6978/1000/speaker/Speaker%20B/suggest")
    # asking changes nothing: no label, no score, no voiceprint
    assert con.execute("SELECT id, label, labeled_by, match_score FROM speaker_clusters ORDER BY id").fetchall() == before
    assert "Alex Rivera" not in speakers.load_voiceprints(speakers.voiceprint_dir(tmp_path / "data"))
    top = out["maybe"][0]
    code, saved = post(spk + f"/pendant/feed/speakers/{cid}/label",
                       {"name": top["label"], "source": "user", "entity_id": top["entity_id"]})
    assert code == 200 and saved["name"] == "Alex Rivera"
    assert con.execute("SELECT label, labeled_by FROM speaker_clusters WHERE id=?", (cid,)).fetchone() == \
        ("Alex Rivera", "user")
    sp = json.loads((tmp_path / "data" / "config.json").read_text())["speakers"]
    assert "Alex Rivera" in sp["allowed"] and sp["people"]["Alex Rivera"] == "e-lex"
    assert "Alex Rivera" in speakers.load_voiceprints(speakers.voiceprint_dir(tmp_path / "data"))


def test_day_feed_exposes_calendar_and_every_meeting_note(tmp_path, monkeypatch):
    con = make_db(tmp_path, monkeypatch)
    heading = "16:15 — Fix The Garage Door (26m03s) · old"
    con.execute("INSERT INTO conversations (device, start_ms, end_ms, day, raw_heading, meeting_note, meeting_source, "
                "meeting_notes, calendar_event) VALUES ('0F1E2D3C4B5A6978', 1, 2, '2026-11-09', ?, 'A', 'wispr', ?, ?)",
                (heading, json.dumps([{"note": "A", "source": "wispr"}, {"note": "B", "source": "granola",
                                                                         "via": "continuation"}]), MORGAN_EVENT))
    con.commit()
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "2026-11-09.md").write_text(f"## {heading}\n\n*Also recorded in [[A]] (Wispr).*\n\n"
                                         "*Calendar: Morgan-Noah week-ahead prep (12:00–12:30) · Noah, Morgan Hale*\n\n"
                                         "[16:15] **Noah:** words\n\n---\n")
    monkeypatch.setenv("PENDANT_NOTES_DIR", str(notes))
    monkeypatch.setenv("PENDANT_DATA", str(tmp_path / "data"))
    c = feed.day_payload(feed.dt.date(2026, 11, 9))["conversations"][0]
    assert c["meetings"] == [{"note": "A", "source": "wispr", "shared": False},
                             {"note": "B", "source": "granola", "shared": True}]
    assert c["calendar"]["title"] == "Morgan-Noah week-ahead prep" and c["calendar"]["shared"] is False
    assert [a["name"] for a in c["calendar"]["attendees"]] == ["Noah", "Morgan Hale"]
    assert "Calendar" not in c["text"] and c["words"] == 6   # the Also-recorded line stays text
