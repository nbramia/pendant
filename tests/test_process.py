"""Tests for process.py: spool parsing, dedupe, conversation grouping and
transcription into the daily file. Synthetic pages only; whisper is faked."""
from __future__ import annotations

import datetime as dt
import json
import struct
import time

import pytest

from pendant_direct import process
from pendant_direct import transcribe as tx
from pendant_direct.proto import flash_page_pb2, server_pb2

TZ = process.TZ
BASE = dt.datetime(2026, 1, 5, 10, 0, tzinfo=TZ)
BASE_MS = int(BASE.timestamp() * 1000)
CHUNKS_PER_PAGE = 50   # 50 x 20 ms = 1 s of audio per page
PAGE_MS = 1000


# ---------------------------------------------------------------------------
# helpers (same style as tests/test_decode.py)

def opus(device: str, page: int, j: int) -> bytes:
    return b"\xb8" + device.encode() + struct.pack("<II", page, j)


def make_msg(*, page_index: int, ts_ms: int, packets: list[bytes], ingest_type: int = 2) -> bytes:
    page = flash_page_pb2.FlashPage()
    page.absolute_timestamp_ms = ts_ms
    for p in packets:
        page.chunks.add().audio_data.codec_beamforming_data = p
    msg = server_pb2.PendantAllMsg()
    msg.storage_buffer.ingest_type = ingest_type
    msg.storage_buffer.index = page_index
    msg.storage_buffer.flash_page = page.SerializeToString()
    return msg.SerializeToString()


def pages(device: str, start_ms: int, n: int, *, first_index: int = 0, step_ms: int = PAGE_MS):
    """n consecutive full pages starting at start_ms."""
    return [make_msg(page_index=first_index + k, ts_ms=start_ms + k * step_ms,
                     packets=[opus(device, first_index + k, j) for j in range(CHUNKS_PER_PAGE)])
            for k in range(n)]


def write_segment(spool, name: str, msgs: list[bytes], tail: bytes = b""):
    spool.mkdir(parents=True, exist_ok=True)
    path = spool / name
    path.write_bytes(b"".join(struct.pack("<I", len(m)) + m for m in msgs) + tail)
    return path


def at(minutes: float) -> int:
    return BASE_MS + int(minutes * 60_000)


@pytest.fixture
def proc(tmp_path, monkeypatch):
    monkeypatch.setattr(process, "DATA", tmp_path)
    monkeypatch.setattr(process, "SPOOL", tmp_path / "spool")
    monkeypatch.setattr(process, "DB", tmp_path / "pages.db")
    monkeypatch.setattr(process, "CLIPS", tmp_path / "clips")
    # vault() is NOTES_DIR.parent.parent: keep it inside this test's tmp_path
    monkeypatch.setattr(process, "NOTES_DIR", tmp_path / "vault" / "Pendant" / "Raw")
    monkeypatch.setattr(process, "load_keyset", lambda device: None)
    process.SPOOL.mkdir()
    return process


@pytest.fixture(autouse=True)
def fake_embedder(monkeypatch):
    """Never load ECAPA in tests. Set .fn(clip, spans) -> array to vary voices;
    default is one voice."""
    import numpy as np
    from pendant_direct import speakers

    class Fake:
        fn = None
        calls: list = []

        def __call__(self, clip, spans):
            self.calls.append(list(spans))
            if self.fn:
                return self.fn(clip, spans)
            return np.tile(np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32), (len(spans), 1))
    fake = Fake()
    fake.calls = []
    monkeypatch.setattr(speakers, "get_embedder", lambda data_dir: fake)
    return fake


@pytest.fixture
def fake_whisper(monkeypatch):
    """tx.transcribe replacement. Set .segs (list of (start, end, text)) or
    .fn(path) -> segs; calls are recorded."""
    class Fake:
        segs = [(0.0, 1.5, "hello there")]
        fn = None
        calls: list = []

        def __call__(self, path, language="en"):
            self.calls.append(path)
            return (self.fn(path) if self.fn else list(self.segs)), None
    fake = Fake()
    fake.calls = []
    monkeypatch.setattr(tx, "transcribe", fake)
    return fake


def run(proc):
    con = proc.db()
    proc.ingest(con)
    n = proc.transcribe_ready(con)
    return con, n


# ---------------------------------------------------------------------------
# records / segment_time_ms

def test_records_parses_and_stops_at_truncated_tail(tmp_path):
    msgs = [b"a", b"bb" * 10, b""]
    # a tail record claiming 100 bytes with only 3 present, then a stray 2 bytes
    path = write_segment(tmp_path, "x.pmsg", msgs, tail=struct.pack("<I", 100) + b"abc")
    assert list(process.records(path)) == msgs
    (tmp_path / "y.pmsg").write_bytes(b"\x01\x00")
    assert list(process.records(tmp_path / "y.pmsg")) == []


def test_segment_time_ms():
    want = int(dt.datetime(2026, 1, 5, 15, 4, 5, tzinfo=dt.timezone.utc).timestamp() * 1000)
    assert process.segment_time_ms("0F1E2D3C4B5A6978_20260105T150405Z.pmsg") == want
    assert process.segment_time_ms("F00D_20260105T150405Z_usb000123.pmsg") == want
    before = time.time() * 1000
    assert process.segment_time_ms("garbage.pmsg") >= before - 1


# ---------------------------------------------------------------------------
# ingest

def test_ingest_dedupes_live_and_batch_copies(proc):
    dev = "DEVA"
    full = [opus(dev, 7, j) for j in range(5)]
    live = make_msg(page_index=7, ts_ms=at(0), packets=full[:2], ingest_type=1)
    batch = make_msg(page_index=7, ts_ms=at(0) + 900, packets=full, ingest_type=2)
    write_segment(proc.SPOOL, f"{dev}_20260105T150000Z.pmsg", [live])
    write_segment(proc.SPOOL, f"{dev}_20260105T150100Z.pmsg", [batch])
    write_segment(proc.SPOOL, f"{dev}_20260105T150200Z.pmsg", [batch])  # re-drain
    con = proc.db()
    assert proc.ingest(con) == 3
    rows = con.execute("SELECT opus FROM chunks ORDER BY ts_ms, page, ord").fetchall()
    assert sorted(r[0] for r in rows) == sorted(full)
    assert proc.ingest(con) == 0  # segments are remembered


def test_ingest_uses_segment_time_when_page_clock_invalid(proc):
    write_segment(proc.SPOOL, "DEVA_20260105T150405Z.pmsg",
                  [make_msg(page_index=1, ts_ms=12345, packets=[b"\xb8x"])])
    con = proc.db()
    proc.ingest(con)
    (ts,) = con.execute("SELECT ts_ms FROM chunks").fetchone()
    assert ts == proc.segment_time_ms("DEVA_20260105T150405Z.pmsg")


# ---------------------------------------------------------------------------
# conversations

def test_conversations_split_on_gaps_longer_than_gap_s(proc):
    assert proc.GAP_S == 60
    dev = "DEVA"
    msgs = (pages(dev, at(0), 3, first_index=0)
            # 59 s after the last page: same conversation
            + pages(dev, at(0) + 2 * PAGE_MS + 59_000, 3, first_index=3)
            # 61 s after the last page: new conversation
            + pages(dev, at(0) + 2 * PAGE_MS + 59_000 + 2 * PAGE_MS + 61_000, 2, first_index=6))
    write_segment(proc.SPOOL, f"{dev}_20260105T150000Z.pmsg", msgs)
    con = proc.db()
    proc.ingest(con)
    convs = list(proc.conversations(con, dev))
    assert [len(c) for c in convs] == [6 * CHUNKS_PER_PAGE, 2 * CHUNKS_PER_PAGE]


# ---------------------------------------------------------------------------
# transcribe_ready

def daily_for(proc, day=BASE.date()):
    return proc.NOTES_DIR / f"{day:%Y-%m-%d}.md"


def test_transcribe_ready_writes_daily_file(proc, fake_whisper):
    dev = "DEVA"
    msgs = (pages(dev, at(0), 3, first_index=0)
            + pages(dev, at(10), 1, first_index=3)           # 1 s blip: skipped
            + pages(dev, at(20), 4, first_index=4))
    write_segment(proc.SPOOL, f"{dev}_20260105T150000Z.pmsg", msgs)
    con, n = run(proc)
    assert n == 2
    assert len(fake_whisper.calls) == 2
    text = daily_for(proc).read_text()
    assert text.startswith("---\ndate: 2026-01-05\n")
    assert text.count("---\ndate:") == 1
    assert text.count("\n## ") == 2
    assert "## 10:00 — Hello There (0m03s) · DEVA" in text
    assert "## 10:20 — Hello There (0m04s) · DEVA" in text
    assert "hello there" in text
    assert con.execute("SELECT COUNT(*) FROM chunks WHERE done=0").fetchone()[0] == 0
    assert (proc.CLIPS / "DEVA_20260105_100000.opus").read_bytes().startswith(b"OggS")


def test_transcribe_ready_appends_later_and_is_idempotent(proc, fake_whisper):
    dev = "DEVA"
    write_segment(proc.SPOOL, f"{dev}_20260105T150000Z.pmsg", pages(dev, at(0), 3))
    run(proc)
    first = daily_for(proc).read_text()
    con, n = run(proc)
    assert n == 0
    assert daily_for(proc).read_text() == first

    write_segment(proc.SPOOL, f"{dev}_20260105T160000Z.pmsg", pages(dev, at(60), 3, first_index=100))
    con, n = run(proc)
    assert n == 1
    text = daily_for(proc).read_text()
    assert text.startswith(first)
    assert text.count("---\ndate:") == 1 and text.count("\n## ") == 2


def test_transcribe_ready_waits_for_unsettled_conversation(proc, fake_whisper):
    dev = "DEVA"
    now = int(time.time() * 1000)
    write_segment(proc.SPOOL, f"{dev}_20260105T150000Z.pmsg",
                  pages(dev, at(0), 3) + pages(dev, now - 30_000, 3, first_index=50))
    con, n = run(proc)
    assert n == 1
    pending = con.execute("SELECT COUNT(*) FROM chunks WHERE done=0").fetchone()[0]
    assert pending == 3 * CHUNKS_PER_PAGE
    con, n = run(proc)
    assert n == 0


def test_transcribe_ready_skips_blips_without_writing(proc, fake_whisper):
    dev = "DEVA"
    write_segment(proc.SPOOL, f"{dev}_20260105T150000Z.pmsg", pages(dev, at(0), 1))
    con, n = run(proc)
    assert n == 0
    assert fake_whisper.calls == []
    assert not daily_for(proc).exists()
    assert con.execute("SELECT COUNT(*) FROM chunks WHERE done=0").fetchone()[0] == 0


# ---------------------------------------------------------------------------
# wall-clock [HH:MM] stamps inside a conversation

def section_body(text: str) -> str:
    """The transcript body of the only section in a daily file."""
    section = text.split("\n## ", 1)[1]
    return section.split("\n", 1)[1].strip().removesuffix("---").strip()


def test_wall_ms_maps_offsets_through_breakpoints():
    bps = [(0.0, 1_000), (1.0, 50_000)]
    assert process.wall_ms(bps, 0.0) == 1_000
    assert process.wall_ms(bps, 0.5) == 1_500
    assert process.wall_ms(bps, 1.0) == 50_000
    assert process.wall_ms(bps, 2.25) == 51_250


def test_write_clip_breakpoints_and_pauses(tmp_path):
    conv = [(i, ts, b"\xb8" + bytes([i])) for i, ts in
            enumerate([0, 0, 1000, 1000, 31_000, 31_000, 40_000])]
    bps, pauses = process.write_clip(conv, tmp_path / "c.opus")
    assert bps == [(0.0, 0), (2 * 0.02, 1000), (4 * 0.02, 31_000), (6 * 0.02, 40_000)]
    assert pauses == [4 * 0.02]  # the 30 s pause; the 9 s one isn't
    assert (tmp_path / "c.opus").read_bytes().startswith(b"OggS")


def test_stamps_after_pause_but_not_short_gap(proc, fake_whisper):
    dev = "DEVA"
    t0 = at(0.5)                         # 10:00:30
    t1 = t0 + 2 * PAGE_MS + 50_000       # 50 s pause -> 10:01:22
    t2 = t1 + 2 * PAGE_MS + 10_000       # 10 s gap  -> 10:01:36
    write_segment(proc.SPOOL, f"{dev}_20260105T150000Z.pmsg",
                  pages(dev, t0, 3) + pages(dev, t1, 3, first_index=3) + pages(dev, t2, 3, first_index=6))
    fake_whisper.segs = [(0.2, 1.0, "one"), (1.5, 2.5, "two"), (3.1, 5.0, "three"), (6.2, 8.0, "four")]
    run(proc)
    text = daily_for(proc).read_text()
    assert "## 10:00 — One Two Three Four (0m09s) · DEVA" in text
    assert section_body(text) == "[10:00] one two\n\n[10:01] three four"


def test_stamps_every_five_minutes(proc, fake_whisper):
    dev = "DEVA"
    # 36 pages 10 s apart (9 s gaps, below PAUSE_S): 36 s of audio over 6 minutes
    write_segment(proc.SPOOL, f"{dev}_20260105T150000Z.pmsg", pages(dev, at(0), 36, step_ms=10_000))
    fake_whisper.segs = [(5.0 * j, 5.0 * j + 4, f"s{j}") for j in range(8)]
    run(proc)
    body = section_body(daily_for(proc).read_text())
    assert body == "[10:00] s0 s1 s2 s3 s4 s5\n\n[10:05] s6 s7"


# ---------------------------------------------------------------------------
# two pendants recording the same moment

def test_overlaps():
    assert process.overlaps(0, 10, 2, 12)          # 8 of 10 shared
    assert not process.overlaps(0, 10, 5, 15)      # exactly half: not more than half
    assert process.overlaps(0, 10, 4, 6)           # shorter fully inside
    assert not process.overlaps(0, 10, 20, 30)
    assert process.overlaps(5, 5, 0, 10)           # zero-length span inside
    assert not process.overlaps(15, 15, 0, 10)


def spans(con):
    return con.execute("SELECT device, start_ms, end_ms, audio_s FROM spans ORDER BY start_ms").fetchall()


def test_overlapping_devices_keep_longer(proc, fake_whisper):
    write_segment(proc.SPOOL, "DEVA_20260105T150000Z.pmsg", pages("DEVA", at(0), 5))
    write_segment(proc.SPOOL, "DEVB_20260105T150000Z.pmsg", pages("DEVB", at(0) + 1000, 3))
    con, n = run(proc)
    assert n == 1
    assert [c.name for c in fake_whisper.calls] == ["DEVA_20260105_100000.opus"]
    text = daily_for(proc).read_text()
    assert text.count("\n## ") == 1 and "DEVB" not in text
    assert con.execute("SELECT COUNT(*) FROM chunks WHERE done=0").fetchone()[0] == 0
    assert spans(con) == [("DEVA", at(0), at(0) + 4000, 5.0)]


def test_longer_one_wins_regardless_of_device_order(proc, fake_whisper):
    write_segment(proc.SPOOL, "DEVA_20260105T150000Z.pmsg", pages("DEVA", at(0) + 1000, 3))
    write_segment(proc.SPOOL, "DEVB_20260105T150000Z.pmsg", pages("DEVB", at(0), 5))
    con, n = run(proc)
    assert n == 1
    assert [c.name for c in fake_whisper.calls] == ["DEVB_20260105_100000.opus"]


def test_half_or_less_overlap_writes_both(proc, fake_whisper):
    write_segment(proc.SPOOL, "DEVA_20260105T150000Z.pmsg", pages("DEVA", at(0), 10))
    write_segment(proc.SPOOL, "DEVB_20260105T150000Z.pmsg", pages("DEVB", at(0) + 6000, 10))
    con, n = run(proc)
    assert n == 2
    assert [c.name for c in fake_whisper.calls] == ["DEVA_20260105_100000.opus",
                                                    "DEVB_20260105_100006.opus"]  # time order
    assert len(spans(con)) == 2


def test_overlap_with_already_transcribed(proc, fake_whisper):
    write_segment(proc.SPOOL, "DEVA_20260105T150000Z.pmsg", pages("DEVA", at(0), 5))
    run(proc)
    # a shorter copy from another pendant arrives later: dropped
    write_segment(proc.SPOOL, "DEVB_20260105T160000Z.pmsg", pages("DEVB", at(0) + 1000, 3))
    con, n = run(proc)
    assert n == 0
    assert con.execute("SELECT COUNT(*) FROM chunks WHERE done=0").fetchone()[0] == 0
    # a longer one can't retract what's written, so it is written too
    write_segment(proc.SPOOL, "DEVC_20260105T160000Z.pmsg", pages("DEVC", at(0), 8))
    con, n = run(proc)
    assert n == 1
    assert [d for d, *_ in spans(con)] == ["DEVA", "DEVC"]
    assert daily_for(proc).read_text().count("\n## ") == 2


def test_same_device_is_never_deduped_against_itself(proc, fake_whisper):
    write_segment(proc.SPOOL, "DEVA_20260105T150000Z.pmsg", pages("DEVA", at(0), 5))
    run(proc)
    # a later re-drain of different audio from the same pendant at the same time
    # (e.g. after a wipe, page indices restart) is not treated as a duplicate
    write_segment(proc.SPOOL, "DEVA_20260105T160000Z.pmsg", pages("DEVA", at(0), 3, first_index=500))
    con, n = run(proc)
    assert n == 1


def test_db_migrates_old_primary_key_and_keeps_done(tmp_path, monkeypatch):
    import sqlite3
    from pendant_direct import process
    dbp = tmp_path / "pages.db"
    monkeypatch.setattr(process, "DB", dbp)
    con = sqlite3.connect(dbp)
    con.execute("CREATE TABLE chunks (device TEXT, page INT, ord INT, opus_sha TEXT, ts_ms INT, opus BLOB, "
                "done INT DEFAULT 0, PRIMARY KEY (device, page, opus_sha))")
    con.execute("INSERT INTO chunks VALUES ('D', 1, 0, 'aa', 5, x'00', 1)")
    con.commit()
    con.close()
    con = process.db()
    assert con.execute("SELECT device, page, ord, done FROM chunks").fetchall() == [("D", 1, 0, 1)]
    # identical packet at another position in the same page is now kept
    con.execute("INSERT OR IGNORE INTO chunks (device, page, ord, opus_sha, ts_ms, opus) VALUES ('D', 1, 1, 'aa', 5, x'00')")
    assert con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 2


def test_conversation_without_speech_is_not_written(proc, fake_whisper):
    dev = "DEVA"
    write_segment(proc.SPOOL, f"{dev}_20260105T150000Z.pmsg", pages(dev, at(0), 3))
    fake_whisper.segs = []
    con, n = run(proc)
    assert n == 0
    assert not daily_for(proc).exists()
    assert con.execute("SELECT COUNT(*) FROM chunks WHERE done=0").fetchone()[0] == 0


# ---------------------------------------------------------------------------
# names, speakers, summaries, events, highlights

OLD = "0F1E2D3C4B5A6978"


def test_heading_uses_title_and_pendant_name(proc, fake_whisper):
    write_segment(proc.SPOOL, f"{OLD}_20260105T150000Z.pmsg", pages(OLD, at(0), 3))
    fake_whisper.segs = [(0.0, 2.5, "So, the #garage [door] is broken again. Call Bob.")]
    con, n = run(proc)
    text = daily_for(proc).read_text()
    assert "## 10:00 — So, The Garage Door Is Broken Again. Call (0m03s) · old\n" in text
    row = con.execute("SELECT pendant, title, overview, action_items, notable, raw_heading, day "
                      "FROM conversations").fetchone()
    assert row == ("old", "So, The Garage Door Is Broken Again. Call",
                   "So, the #garage [door] is broken again. Call Bob.", "[]", 0,
                   "10:00 — So, The Garage Door Is Broken Again. Call (0m03s) · old", "2026-01-05")


def test_speaker_labels_render_once_a_voiceprint_exists(proc, fake_whisper, fake_embedder):
    import numpy as np
    from pendant_direct import speakers
    a, b = np.eye(4, dtype=np.float32)[:2]
    for _ in range(2):   # enough samples to name a voice (speakers.min_samples)
        speakers.enroll(proc.DATA / "voiceprints", "Noah", a, ["Noah"])
    write_segment(proc.SPOOL, "DEVA_20260105T150000Z.pmsg", pages("DEVA", at(0), 12))
    fake_whisper.segs = [(0.0, 2.0, "hi there"), (2.5, 4.5, "hello"), (5.0, 7.0, "how are you"),
                         (7.5, 9.5, "good")]
    fake_embedder.fn = lambda clip, spans: np.stack([a if s < 2.2 or 5 <= s < 7 else b for s, _e in spans])
    con, n = run(proc)
    assert n == 1
    body = section_body(daily_for(proc).read_text())
    assert body == ("[10:00] **Noah:** hi there\n\n[10:00] **Speaker A:** hello\n\n"
                    "[10:00] **Noah:** how are you\n\n[10:00] **Speaker A:** good")
    rows = con.execute("SELECT tag, label, labeled_by, speech_s FROM speaker_clusters ORDER BY tag").fetchall()
    assert rows == [("Noah", "Noah", "match", 4.0), ("Speaker A", None, None, 4.0)]
    assert con.execute("SELECT people FROM conversations").fetchone()[0] == '["Noah"]'


def test_no_voiceprints_stays_unlabeled_but_stores_clusters(proc, fake_whisper):
    write_segment(proc.SPOOL, "DEVA_20260105T150000Z.pmsg", pages("DEVA", at(0), 5))
    fake_whisper.segs = [(0.0, 2.0, "hi there"), (2.5, 4.5, "hello")]
    con, n = run(proc)
    assert section_body(daily_for(proc).read_text()) == "[10:00] hi there hello"
    assert con.execute("SELECT tag, label FROM speaker_clusters").fetchall() == [("Speaker A", None)]


def test_speaker_failure_never_blocks(proc, fake_whisper, fake_embedder):
    def boom(clip, spans):
        raise RuntimeError("model exploded")
    fake_embedder.fn = boom
    write_segment(proc.SPOOL, "DEVA_20260105T150000Z.pmsg", pages("DEVA", at(0), 5))
    fake_whisper.segs = [(0.0, 2.0, "hi there")]
    con, n = run(proc)
    assert n == 1 and section_body(daily_for(proc).read_text()) == "[10:00] hi there"
    assert con.execute("SELECT COUNT(*) FROM speaker_clusters").fetchone()[0] == 0


def test_speakers_disabled_in_config(proc, fake_whisper, fake_embedder):
    (proc.DATA / "config.json").write_text('{"speakers": {"enabled": false}}')
    write_segment(proc.SPOOL, "DEVA_20260105T150000Z.pmsg", pages("DEVA", at(0), 5))
    fake_whisper.segs = [(0.0, 2.0, "hi there")]
    run(proc)
    assert fake_embedder.calls == []


def test_deepseek_summarizer_failure_falls_back(proc, fake_whisper, monkeypatch):
    from pendant_direct import summarize
    (proc.DATA / "config.json").write_text('{"summarizer": "claude"}')   # legacy value means DeepSeek
    calls = []

    def fail(text, model, post=None, usage=None, **kw):
        calls.append(model)
        raise RuntimeError("api down")
    monkeypatch.setattr(summarize, "fireworks", fail)
    write_segment(proc.SPOOL, "DEVA_20260105T150000Z.pmsg", pages("DEVA", at(0), 3))
    con, n = run(proc)
    assert n == 1 and calls == [summarize.FIREWORKS_MODEL]
    assert "## 10:00 — Hello There (0m03s) · DEVA" in daily_for(proc).read_text()


def test_notable_conversation_gets_event_and_highlights(proc, fake_whisper, monkeypatch):
    from pendant_direct import summarize
    monkeypatch.setattr(summarize, "heuristic", lambda text: summarize.Summary(
        "Budget Review", "We reviewed the budget.", ["Send the sheet", "Book a call"], "work"))
    write_segment(proc.SPOOL, "DEVA_20260105T150000Z.pmsg", pages("DEVA", at(0), 3))
    write_segment(proc.SPOOL, "DEVA_20260105T160000Z.pmsg", pages("DEVA", at(30), 3, first_index=10))
    con, n = run(proc)
    assert n == 2
    events = sorted((proc.NOTES_DIR.parent / "Events").iterdir())
    assert [e.name for e in events] == [f"2026-01-05T100000 - budget-review - DEVA-{at(0)}.md",
                                        f"2026-01-05T103000 - budget-review - DEVA-{at(30)}.md"]
    ev = events[0].read_text()
    assert "raw_link: '[[2026-01-05#10:00 — Budget Review (0m03s) · DEVA]]'" in ev
    assert "- [ ] Send the sheet\n- [ ] Book a call\n" in ev
    hl_path = proc.NOTES_DIR.parent / "Highlights" / "2026-01-05 Highlights.md"
    text = hl_path.read_text()
    assert text.count("- [ ] Send the sheet") == 2
    assert f"- 10:30 — [[2026-01-05T103000 - budget-review - DEVA-{at(30)}]]" in text
    # regenerating from the DB gives the same file
    before = text
    proc.write_highlights(con, "2026-01-05")
    assert hl_path.read_text() == before


def test_highlights_accumulate_across_runs(proc, fake_whisper):
    write_segment(proc.SPOOL, "DEVA_20260105T150000Z.pmsg", pages("DEVA", at(0), 3))
    run(proc)
    hl_path = proc.NOTES_DIR.parent / "Highlights" / "2026-01-05 Highlights.md"
    assert hl_path.read_text().count("\n- 10:") == 1
    assert not (proc.NOTES_DIR.parent / "Events").exists()
    write_segment(proc.SPOOL, "DEVA_20260105T160000Z.pmsg", pages("DEVA", at(5), 3, first_index=50))
    run(proc)
    text = hl_path.read_text()
    assert text.count("\n- 10:") == 2 and "*No notable events.*" in text


def test_notable_override_file(proc, fake_whisper):
    import json as _json
    (proc.DATA / "notable_overrides.json").write_text(_json.dumps({f"DEVA-{at(0)}": True}))
    write_segment(proc.SPOOL, "DEVA_20260105T150000Z.pmsg", pages("DEVA", at(0), 3))
    con, _ = run(proc)
    assert con.execute("SELECT notable, event_file IS NOT NULL FROM conversations").fetchone() == (1, 1)


def test_db_upgrade_keeps_spans_and_done_marks(tmp_path, monkeypatch):
    import sqlite3
    dbp = tmp_path / "pages.db"
    monkeypatch.setattr(process, "DB", dbp)
    con = sqlite3.connect(dbp)
    con.executescript(f"""{process.CHUNKS_SQL};
        CREATE TABLE segments (name TEXT PRIMARY KEY, ingested_at REAL);
        CREATE TABLE spans (device TEXT, start_ms INT, end_ms INT, audio_s REAL);
        INSERT INTO chunks VALUES ('D', 1, 0, 'aa', 5, x'00', 1);
        INSERT INTO spans VALUES ('D', 1, 2, 3.0);""")
    con.commit()
    con.close()
    con = process.db()
    assert con.execute("SELECT * FROM spans").fetchall() == [("D", 1, 2, 3.0)]
    assert con.execute("SELECT done FROM chunks").fetchall() == [(1,)]
    assert con.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM speaker_clusters").fetchone()[0] == 0
    process.db()  # idempotent


def test_cap_length_cuts_long_stretches_at_a_pause():
    # chunks every 2 s for 40 min, with a 15 s pause 27 min in
    rows, t = [], 0
    while t < 40 * 60_000:
        rows.append((len(rows), t, b"x"))
        t += 15_000 if t == 27 * 60_000 else 2_000
    pieces = list(process.cap_length(rows))
    assert len(pieces) == 2
    assert pieces[0][-1][1] == 27 * 60_000          # cut right before the pause
    assert sum(len(p) for p in pieces) == len(rows)


def test_cap_length_leaves_short_conversations_alone():
    rows = [(i, i * 1000, b"x") for i in range(100)]
    assert list(process.cap_length(rows)) == [rows]


def test_conversation_followed_by_more_audio_is_written_without_waiting(proc, fake_whisper, monkeypatch):
    import time as _t
    dev = "DEVA"
    # two conversations 2 min apart, both "just now": the first is closed by the second
    base_ms = int(_t.time() * 1000) - 4 * 60_000
    write_segment(proc.SPOOL, f"{dev}_20260105T150000Z.pmsg",
                  pages(dev, base_ms, 3) + pages(dev, base_ms + 3 * 60_000, 3, first_index=3))
    con, n = run(proc)
    assert n == 1


def meeting_note(day, start: dt.datetime, end: dt.datetime, title="Weekly sync") -> str:
    return (f"---\ndate: {day}\nsource: granola\ntitle: {title}\n---\n# {title}\n\n## Transcript\n\n"
            f"**Me** *({start:%H:%M:%S})*: Hi.\n\n**Them** *({end:%H:%M:%S})*: Bye.\n")


def test_conversation_during_a_granola_meeting_links_and_drops_followups(proc, fake_whisper, monkeypatch):
    from pendant_direct import summarize as sm
    monkeypatch.setattr(sm, "summarize", lambda text, cfg, log=print, **k: sm.Summary(
        title="Sync", overview="o", action_items=["Email Sam"], category="work"))
    meetings_dir = proc.NOTES_DIR.parent.parent / "Work" / "Meetings"
    meetings_dir.mkdir(parents=True)
    (meetings_dir / "Weekly sync 20260105.md").write_text(
        meeting_note(BASE.date(), BASE - dt.timedelta(minutes=1), BASE + dt.timedelta(minutes=25)))
    write_segment(proc.SPOOL, "DEVA_20260105T150000Z.pmsg", pages("DEVA", at(0), 3))
    con, n = run(proc)
    assert n == 1
    text = daily_for(proc).read_text()
    assert "*Also recorded in [[Weekly sync 20260105]] (Granola).*" in text
    row = con.execute("SELECT meeting_note, action_items, notable FROM conversations").fetchone()
    assert row == ("Weekly sync 20260105", "[]", 0)
    hl_text = (proc.NOTES_DIR.parent / "Highlights" / f"{BASE.date()} Highlights.md").read_text()
    assert "Email Sam" not in hl_text and "also in [[Weekly sync 20260105]]" in hl_text


def test_meeting_note_that_arrives_later_is_linked_by_relink(proc, fake_whisper, monkeypatch):
    import time as _t
    from pendant_direct import summarize as sm
    monkeypatch.setattr(sm, "summarize", lambda text, cfg, log=print, **k: sm.Summary(
        title="Sync", overview="o", action_items=["Email Sam"], category="work"))
    now = dt.datetime.now(process.TZ).replace(microsecond=0) - dt.timedelta(minutes=20)
    start_ms = int(now.timestamp() * 1000)
    write_segment(proc.SPOOL, "DEVA_20260105T150000Z.pmsg", pages("DEVA", start_ms, 3))
    con, n = run(proc)
    assert n == 1
    day = now.date()
    assert "Also recorded" not in daily_for(proc, day).read_text()
    # the Granola note lands afterwards
    d = proc.NOTES_DIR.parent.parent / "Work"
    d.mkdir(parents=True, exist_ok=True)
    (d / "Late sync.md").write_text(meeting_note(day, now - dt.timedelta(minutes=1), now + dt.timedelta(minutes=5),
                                                 title="Late sync"))
    changed = proc.relink_meetings(con)
    assert changed == {day.isoformat()}
    text = daily_for(proc, day).read_text()
    assert text.count("*Also recorded in [[Late sync]] (Granola).*") == 1
    assert proc.relink_meetings(con) == set()          # idempotent
    assert con.execute("SELECT action_items FROM conversations").fetchone()[0] == "[]"


def test_meeting_names_rename_section_and_enroll_only_allowed(proc, monkeypatch, tmp_path):
    import numpy as np
    from pendant_direct import meetings, speakers
    day = "2026-01-05"
    heading = "10:00 — Sync (1m00s) · new"
    daily = proc.NOTES_DIR / f"{day}.md"
    proc.NOTES_DIR.mkdir(parents=True, exist_ok=True)
    daily.write_text(f"# Day\n\n## {heading}\n\n[10:01] **Speaker A:** reviewed precinct captain staffing plan "
                     "budget numbers tight\n\n[10:02] **Speaker B:** recruiting pipeline captains stalled after "
                     "retreat honestly\n\n---\n")
    con = proc.db()
    for tag in ("Speaker A", "Speaker B"):
        con.execute("INSERT INTO speaker_clusters (device, start_ms, tag, embedding, labeled_by) VALUES (?,?,?,?,NULL)",
                    ("DEVA", 1, tag, np.ones(4, dtype=np.float32).tobytes()))
    note = ("---\ndate: 2026-01-05\nsource: granola\ntitle: Noah-Kai weekly\n---\n## Transcript\n\n"
            "**Me** *(10:01:00)*: reviewed the precinct captain staffing plan and the budget numbers are tight\n\n"
            "**Them** *(10:02:00)*: the recruiting pipeline for captains stalled after the retreat honestly\n")
    m = meetings.parse(tmp_path / "Noah-Kai weekly.md", note)
    enrolled = []
    monkeypatch.setattr(speakers, "enroll", lambda vdir, name, emb, allowed, **kw: enrolled.append(name) or 1)
    mapping = proc.apply_meeting_names(con, "DEVA", 1, day, heading, m,
                                       {"speakers": {"allowed": ["Noah"], "wearer": "Noah"}})
    assert mapping == {"Speaker A": "Noah", "Speaker B": "Kai"}
    text = daily.read_text()
    assert "**Noah:** reviewed" in text and "**Kai:** recruiting" in text
    assert enrolled == ["Noah"]                      # Kai isn't on the consent list
    rows = dict(con.execute("SELECT tag, label FROM speaker_clusters").fetchall())
    assert rows == {"Noah": "Noah", "Kai": None}


# ---------------------------------------------------------------------------
# stars (short presses of the pendant's button)

def button_msg(*, page_index: int, ts_ms: int, presses: list[tuple[int, int]], packets=(),
               ingest_type: int = 2) -> bytes:
    """A page with audio packets followed by button chunks [(button_event, press ms)]."""
    page = flash_page_pb2.FlashPage()
    page.absolute_timestamp_ms = ts_ms
    for p in packets:
        page.chunks.add().audio_data.codec_beamforming_data = p
    for event, press_ms in presses:
        b = page.chunks.add().button_status
        b.button_event = event
        b.button_event_absolute_timestamp_ms = press_ms
    msg = server_pb2.PendantAllMsg()
    msg.storage_buffer.ingest_type = ingest_type
    msg.storage_buffer.index = page_index
    msg.storage_buffer.flash_page = page.SerializeToString()
    return msg.SerializeToString()


def stars(con):
    return con.execute("SELECT device, ts_ms FROM stars ORDER BY ts_ms").fetchall()


def test_only_short_presses_become_stars(proc):
    presses = [(0, at(0) + 100), (1, at(0) + 200), (2, at(0) + 300), (3, at(0) + 400), (4, at(0) + 500),
               (1, 12345)]  # a short press without a valid clock can't be placed
    write_segment(proc.SPOOL, "DEVA_20260105T150000Z.pmsg",
                  [button_msg(page_index=1, ts_ms=at(0), presses=presses, packets=[opus("DEVA", 1, 0)])])
    con = proc.db()
    proc.ingest(con)
    assert stars(con) == [("DEVA", at(0) + 200)]
    # the button chunks don't disturb the audio
    assert con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 1


def test_same_press_live_and_drained_is_one_star(proc):
    press = at(0) + 700
    live = button_msg(page_index=7, ts_ms=at(0), presses=[(1, press)], ingest_type=1)
    drained = button_msg(page_index=7, ts_ms=at(0) + 900, presses=[(1, press)],
                         packets=[opus("DEVA", 7, j) for j in range(3)])
    write_segment(proc.SPOOL, "DEVA_20260105T150000Z.pmsg", [live])
    write_segment(proc.SPOOL, "DEVA_20260105T150100Z.pmsg", [drained])
    write_segment(proc.SPOOL, "DEVA_20260105T150200Z.pmsg", [drained])  # re-drain
    con = proc.db()
    proc.ingest(con)
    assert stars(con) == [("DEVA", press)]


def paused_conversation(proc, fake_whisper, extra=()):
    """The two-paragraph conversation of test_stamps_after_pause_but_not_short_gap:
    [10:00] one two (from 10:00:30) and [10:01] three four (from 10:01:22)."""
    t0 = at(0.5)
    t1 = t0 + 2 * PAGE_MS + 50_000
    t2 = t1 + 2 * PAGE_MS + 10_000
    write_segment(proc.SPOOL, "DEVA_20260105T150000Z.pmsg",
                  pages("DEVA", t0, 3) + pages("DEVA", t1, 3, first_index=3)
                  + pages("DEVA", t2, 3, first_index=6) + list(extra))
    fake_whisper.segs = [(0.2, 1.0, "one"), (1.5, 2.5, "two"), (3.1, 5.0, "three"), (6.2, 8.0, "four")]
    return t0, t1


def hl_text(proc):
    return (proc.NOTES_DIR.parent / "Highlights" / "2026-01-05 Highlights.md").read_text()


def test_star_marks_its_paragraph_notable_and_highlights(proc, fake_whisper):
    # 10:01:10 is after the [10:01] stamp's minute but before the second paragraph
    # starts (10:01:22): it belongs to the first.
    press = at(1) + 10_000
    paused_conversation(proc, fake_whisper,
                        [button_msg(page_index=20, ts_ms=at(1), presses=[(1, press), (2, press + 1)])])
    con, n = run(proc)
    assert n == 1
    heading = "10:00 — One Two Three Four (0m09s) · DEVA"
    assert section_body(daily_for(proc).read_text()) == "[10:00] ⭐ one two\n\n[10:01] three four"
    assert con.execute("SELECT notable, event_file IS NOT NULL FROM conversations").fetchone() == (1, 1)
    assert len(list((proc.NOTES_DIR.parent / "Events").iterdir())) == 1
    text = hl_text(proc)
    assert f"## Starred Moments\n\n- 10:01 — [[2026-01-05#{heading}]] — one two\n" in text
    assert f"- 10:00 — [[2026-01-05T100000 - one-two-three-four - DEVA-{at(0.5)}]]" in text
    # nothing pending: a re-run changes nothing
    before = daily_for(proc).read_text()
    con, n = run(proc)
    assert n == 0 and daily_for(proc).read_text() == before and hl_text(proc) == text


def test_two_presses_in_one_paragraph_star_it_once(proc, fake_whisper):
    t0, t1 = paused_conversation(proc, fake_whisper, [button_msg(
        page_index=20, ts_ms=at(1), presses=[(1, at(1) + 30_000), (1, at(1) + 35_000)])])
    run(proc)
    assert section_body(daily_for(proc).read_text()) == "[10:00] one two\n\n[10:01] ⭐ three four"
    assert hl_text(proc).count("— three four") == 1      # presses 5 s apart are one star


def test_star_arriving_after_the_conversation_is_applied_later(proc, fake_whisper):
    paused_conversation(proc, fake_whisper)
    con, n = run(proc)
    assert n == 1 and "⭐" not in daily_for(proc).read_text()
    assert con.execute("SELECT notable FROM conversations").fetchone() == (0,)
    assert "*No starred moments.*" in hl_text(proc)
    before = daily_for(proc).read_text()
    # the drain carrying the press lands on the next upload
    write_segment(proc.SPOOL, "DEVA_20260105T160000Z.pmsg",
                  [button_msg(page_index=30, ts_ms=at(1), presses=[(1, at(1) + 40_000)])])
    con, n = run(proc)
    assert n == 0
    after = daily_for(proc).read_text()
    assert after == before.replace("[10:01] three four", "[10:01] ⭐ three four")
    assert con.execute("SELECT notable, event_file IS NOT NULL FROM conversations").fetchone() == (1, 1)
    assert "— three four" in hl_text(proc)
    run(proc)
    assert daily_for(proc).read_text() == after


def test_star_waits_for_its_conversation_and_ignores_far_presses(proc, fake_whisper):
    import time as _t
    now = int(_t.time() * 1000)
    # a press during audio that hasn't settled, and one far from any audio
    write_segment(proc.SPOOL, "DEVA_20260105T150000Z.pmsg",
                  pages("DEVA", now - 30_000, 3)
                  + [button_msg(page_index=9, ts_ms=now, presses=[(1, now - 29_000), (1, at(0))])])
    con, n = run(proc)
    assert n == 0
    assert con.execute("SELECT COUNT(*) FROM stars WHERE conv_start_ms IS NULL").fetchone()[0] == 2


def test_star_override_false_keeps_conversation_not_notable(proc, fake_whisper):
    import json as _json
    (proc.DATA / "notable_overrides.json").write_text(_json.dumps({f"DEVA-{at(0.5)}": False}))
    paused_conversation(proc, fake_whisper, [button_msg(page_index=20, ts_ms=at(1), presses=[(1, at(1))])])
    con, _ = run(proc)
    assert "⭐" in daily_for(proc).read_text()
    assert con.execute("SELECT notable, event_file FROM conversations").fetchone() == (0, None)


def test_conversations_record_paragraph_clip_offsets(proc, fake_whisper):
    import json as _json
    paused_conversation(proc, fake_whisper)
    con, _ = run(proc)
    offsets = _json.loads(con.execute("SELECT paragraph_offsets FROM conversations").fetchone()[0])
    paras = _json.loads(con.execute("SELECT paragraph_ms FROM conversations").fetchone()[0])
    assert len(offsets) == len(paras) == 2
    assert all(len(o) == 2 and o[0] < o[1] for o in offsets) and offsets[0][0] < offsets[1][0]


def test_star_falls_back_to_minute_stamps_for_older_sections(proc, fake_whisper):
    paused_conversation(proc, fake_whisper)
    con, _ = run(proc)
    con.execute("UPDATE conversations SET paragraph_ms=NULL")  # written before paragraph times were kept
    con.commit()
    write_segment(proc.SPOOL, "DEVA_20260105T160000Z.pmsg",
                  [button_msg(page_index=30, ts_ms=at(1), presses=[(1, at(1) + 10_000)])])
    run(proc)
    # by minute stamps alone 10:01:10 falls in the [10:01] paragraph
    assert section_body(daily_for(proc).read_text()) == "[10:00] one two\n\n[10:01] ⭐ three four"


def test_starred_conversation_stays_notable_when_its_meeting_note_lands(proc, fake_whisper):
    now = dt.datetime.now(process.TZ).replace(microsecond=0) - dt.timedelta(minutes=20)
    start_ms = int(now.timestamp() * 1000)
    write_segment(proc.SPOOL, "DEVA_20260105T150000Z.pmsg",
                  pages("DEVA", start_ms, 3) + [button_msg(page_index=9, ts_ms=start_ms,
                                                           presses=[(1, start_ms + 1000)])])
    con, n = run(proc)
    assert con.execute("SELECT notable, event_file IS NOT NULL FROM conversations").fetchone() == (1, 1)
    d = proc.NOTES_DIR.parent.parent / "Work"
    d.mkdir(parents=True, exist_ok=True)
    (d / "Late sync.md").write_text(meeting_note(now.date(), now - dt.timedelta(minutes=1),
                                                 now + dt.timedelta(minutes=5), title="Late sync"))
    assert proc.relink_meetings(con) == {now.date().isoformat()}
    row = con.execute("SELECT meeting_note, notable, event_file FROM conversations").fetchone()
    assert row[0] == "Late sync" and row[1] == 1
    assert (proc.NOTES_DIR.parent / "Events" / row[2]).exists()


def test_db_adds_stars_table_and_paragraph_column_keeping_done(tmp_path, monkeypatch):
    import sqlite3
    dbp = tmp_path / "pages.db"
    monkeypatch.setattr(process, "DB", dbp)
    con = sqlite3.connect(dbp)
    con.executescript(f"""{process.CHUNKS_SQL};
        {process.CONVERSATIONS_SQL};
        INSERT INTO chunks VALUES ('D', 1, 0, 'aa', 5, x'00', 1);""")
    con.commit()
    con.close()
    con = process.db()
    assert con.execute("SELECT done FROM chunks").fetchall() == [(1,)]
    assert con.execute("SELECT COUNT(*) FROM stars").fetchone()[0] == 0
    assert {"paragraph_ms", "paragraph_offsets"} <= {r[1] for r in con.execute("PRAGMA table_info(conversations)")}
    assert "para_index" in {r[1] for r in con.execute("PRAGMA table_info(transcript_relabels)")}
    process.db()  # idempotent


def test_star_never_lands_in_another_pendants_conversation(proc, fake_whisper):
    import time as _t
    now = int(_t.time() * 1000)
    old = now - 20 * 60_000
    press = old + 1500
    # DEVB recorded the moment and is written; DEVA (which was pressed) has a
    # longer overlapping stretch that is still settling.
    write_segment(proc.SPOOL, "DEVB_20260105T150000Z.pmsg", pages("DEVB", old, 3))
    write_segment(proc.SPOOL, "DEVA_20260105T150000Z.pmsg",
                  [button_msg(page_index=99, ts_ms=old, presses=[(1, press)])]
                  + pages("DEVA", old, 3) + pages("DEVA", old + 50_000, 3, first_index=3, step_ms=60_000 // 2))
    # keep DEVA's audio running into "now" so it isn't settled
    write_segment(proc.SPOOL, "DEVA_20260105T151000Z.pmsg",
                  [m for k in range(40) for m in pages("DEVA", old + 60_000 + k * 30_000, 1, first_index=10 + k)])
    con, _ = run(proc)
    written = {r[0] for r in con.execute("SELECT device FROM conversations")}
    assert written == {"DEVB"}
    assert con.execute("SELECT conv_start_ms FROM stars").fetchall() == [(None,)]
    assert "⭐" not in daily_for(proc, dt.datetime.fromtimestamp(old / 1000, TZ).date()).read_text()


def test_star_with_missing_section_stays_pending_until_it_appears(proc, fake_whisper):
    paused_conversation(proc, fake_whisper, [button_msg(page_index=20, ts_ms=at(1), presses=[(1, at(1))])])
    proc.NOTES_DIR.mkdir(parents=True)
    daily = daily_for(proc)
    # the daily file is moved away right after the conversation is written
    real_apply = proc.apply_stars
    saved = {}

    def apply_without_file(con, overrides):
        saved["text"] = daily.read_text()
        daily.unlink()
        return real_apply(con, overrides)
    proc.apply_stars = apply_without_file
    try:
        con, n = run(proc)
    finally:
        proc.apply_stars = real_apply
    assert n == 1
    assert con.execute("SELECT conv_start_ms, text FROM stars").fetchall() == [(None, None)]
    assert con.execute("SELECT notable FROM conversations").fetchone() == (0,)
    daily.write_text(saved["text"])
    con, n = run(proc)
    assert n == 0
    assert section_body(daily.read_text()) == "[10:00] ⭐ one two\n\n[10:01] three four"
    assert con.execute("SELECT conv_start_ms, text FROM stars").fetchall() == [(at(0.5), "one two")]


def test_backfill_finds_stars_in_segments_ingested_before_stars_existed(proc, fake_whisper):
    seg = write_segment(proc.SPOOL, "DEVA_20260105T150000Z.pmsg",
                        pages("DEVA", at(0), 3) + [button_msg(page_index=9, ts_ms=at(0),
                                                              presses=[(1, at(0) + 1000), (2, at(0) + 2000)])])
    con = proc.db()
    proc.ingest(con)
    # simulate a database from before stars: no stars, no backfill flag, chunks already done
    con.execute("DELETE FROM stars")
    con.execute("DELETE FROM meta")
    con.execute("UPDATE chunks SET done=1")
    con.commit()
    chunks_before = con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    # a segment that arrives now is read by ingest() itself
    write_segment(proc.SPOOL, "DEVA_20260105T160000Z.pmsg",
                  [button_msg(page_index=50, ts_ms=at(30), presses=[(1, at(30))])])
    assert proc.backfill_stars(con) == 1
    assert stars(con) == [("DEVA", at(0) + 1000)]
    assert con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == chunks_before
    assert con.execute("SELECT COUNT(*) FROM chunks WHERE done=0").fetchone()[0] == 0
    proc.ingest(con)
    assert stars(con) == [("DEVA", at(0) + 1000), ("DEVA", at(30))]
    # once only
    con.execute("DELETE FROM stars")
    con.commit()
    assert proc.backfill_stars(con) == 0 and seg.exists()


def test_presses_within_ten_seconds_are_one_star(proc):
    con = proc.db()
    assert proc.add_stars(con, {("DEVA", 1_000_000), ("DEVA", 1_002_000)}) == 1
    assert proc.add_stars(con, {("DEVA", 1_009_999)}) == 0           # still within 10 s of the first
    assert proc.add_stars(con, {("DEVA", 1_010_000), ("DEVB", 1_000_500)}) == 2
    assert sorted(con.execute("SELECT device, ts_ms FROM stars").fetchall()) == \
        [("DEVA", 1_000_000), ("DEVA", 1_010_000), ("DEVB", 1_000_500)]


def test_existing_duplicate_stars_collapse_once(proc):
    con = proc.db()
    for ts in (1_000_000, 1_002_000, 1_030_000):
        con.execute("INSERT INTO stars (device, ts_ms) VALUES ('DEVA', ?)", (ts,))
    con.commit()
    days = proc.collapse_star_duplicates(con)
    assert len(days) == 1
    assert [r[0] for r in con.execute("SELECT ts_ms FROM stars ORDER BY ts_ms")] == [1_000_000, 1_030_000]
    con.execute("INSERT INTO stars (device, ts_ms) VALUES ('DEVA', 1031000)")
    assert proc.collapse_star_duplicates(con) == set()               # runs once


def test_cleanup_runs_before_new_presses_are_merged(proc):
    con = proc.db()
    for ts in (1_000_000, 1_009_000):                     # an old duplicate pair
        con.execute("INSERT INTO stars (device, ts_ms) VALUES ('DEVA', ?)", (ts,))
    con.commit()
    proc.collapse_star_duplicates(con)
    assert proc.add_stars(con, {("DEVA", 1_018_000)}) == 1  # 18 s after the kept star: a new star


# ---------------------------------------------------------------------------
# word-level turns

def words_for(t0, t1, step=0.4):
    out, t, n = [], t0, 0
    while t + step <= t1 + 1e-9:
        out.append((round(t, 3), round(t + step - 0.05, 3), f"w{n}"))
        t, n = t + step, n + 1
    return out


def test_turns_split_one_whisper_segment_at_the_voice_change(proc, fake_whisper, fake_embedder):
    import numpy as np
    a, b = np.eye(4, dtype=np.float32)[:2]
    write_segment(proc.SPOOL, "DEVA_20260105T150000Z.pmsg", pages("DEVA", at(0), 12))
    words = words_for(0.0, 9.0)
    fake_whisper.segs = [tx.Seg(0.0, 8.95, " ".join(w[2] for w in words), words)]
    fake_embedder.fn = lambda clip, spans: np.stack([a if (s + e) / 2 < 5.0 else b for s, e in spans])
    run(proc)
    body = section_body(daily_for(proc).read_text())
    paras = body.split("\n\n")
    assert len(paras) == 2
    assert paras[0].startswith("[10:00] **Speaker A:** w0 ") and paras[1].startswith("[10:00] **Speaker B:** w")
    assert paras[1].endswith(words[-1][2])


def test_word_turns_failure_falls_back_to_segment_turns(proc, fake_whisper, fake_embedder):
    import numpy as np
    a, b = np.eye(4, dtype=np.float32)[:2]
    write_segment(proc.SPOOL, "DEVA_20260105T150000Z.pmsg", pages("DEVA", at(0), 12))
    words = words_for(0.0, 9.0)
    fake_whisper.segs = [tx.Seg(0.0, 3.5, "hi there", words[:9]), tx.Seg(4.0, 8.95, "hello all", words[10:])]
    calls = []

    def embed(clip, spans):
        calls.append(spans)
        if len(calls) == 1:
            raise RuntimeError("window embedding failed")
        return np.stack([a if s < 3.9 else b for s, e in spans])
    fake_embedder.fn = embed
    run(proc)
    assert len(calls) == 2
    assert section_body(daily_for(proc).read_text()) == "[10:00] **Speaker A:** hi there\n\n[10:00] **Speaker B:** hello all"


def test_segment_missing_word_timing_uses_segment_level_diarization(proc, fake_whisper, fake_embedder):
    import numpy as np
    a, b = np.eye(4, dtype=np.float32)[:2]
    write_segment(proc.SPOOL, "DEVA_20260105T150000Z.pmsg", pages("DEVA", at(0), 12))
    words = words_for(0.0, 4.0)
    fake_whisper.segs = [tx.Seg(0.0, 3.9, "hi there", words), tx.Seg(4.5, 8.5, "hello all")]
    seen = []

    def embed(clip, spans):
        seen.append(spans)
        return np.stack([a if s < 4.0 else b for s, e in spans])
    fake_embedder.fn = embed
    run(proc)
    assert len(seen) == 1 and seen[0][0] == (0.0, 3.9)   # segment spans, not windows
    assert section_body(daily_for(proc).read_text()) == "[10:00] **Speaker A:** hi there\n\n[10:00] **Speaker B:** hello all"


def test_written_conversation_gets_kind_and_importance(proc, fake_whisper):
    dev = "DEVA"
    write_segment(proc.SPOOL, f"{dev}_20260105T150000Z.pmsg", pages(dev, at(0), 3, first_index=0))
    fake_whisper.segs = [(0.0, 1.5, "hello there")]    # 2 words: a fragment
    con, n = run(proc)
    assert n == 1
    kind, imp, why = con.execute("SELECT kind, importance, kind_reasons FROM conversations").fetchone()
    assert (kind, imp) == ("fragment", 0) and "2 words" in json.loads(why)[0]


def test_classification_failure_still_writes_a_conversation(proc, fake_whisper, monkeypatch):
    dev = "DEVA"
    write_segment(proc.SPOOL, f"{dev}_20260105T150000Z.pmsg", pages(dev, at(0), 25, first_index=0))
    monkeypatch.setattr(process.classifier, "classify", lambda *a, **k: 1 / 0)
    con, n = run(proc)
    assert n == 1
    assert con.execute("SELECT kind, importance FROM conversations").fetchone() == ("conversation", 0)
    assert daily_for(proc).exists()


# ---------------------------------------------------------------------------
# tiny recordings: join a close neighbour, else a fragment

def tiny_world(proc, parts, dev="DEVA", extra=()):
    """parts: [(start_s, seconds)] on one pendant, one page (1 s) per second of audio."""
    msgs, idx = [], 0
    for start_s, secs in parts:
        msgs += pages(dev, at(0) + start_s * 1000, secs, first_index=idx)
        idx += secs
    write_segment(proc.SPOOL, f"{dev}_20260105T150000Z.pmsg", msgs + list(extra))
    con = proc.db()
    proc.ingest(con)
    return con


def lens(convs):
    return [len(c) // CHUNKS_PER_PAGE for c in convs]


def test_tiny_conversation_joins_the_previous_one_when_the_gap_is_small(proc):
    con = tiny_world(proc, [(0, 40), (130, 5)])       # 90 s of quiet after the first
    convs, tiny, pieces = proc.plan_conversations(con, "DEVA", proc.cfgmod.load(proc.DATA))
    assert lens(convs) == [45] and tiny == set() and pieces == [[40.0, 5.0]]
    assert [r[1] for r in convs[0]][-1] == at(0) + 134_000      # the real gap stays in the page clock


def test_tiny_prefers_the_preceding_conversation_when_both_are_close(proc):
    con = tiny_world(proc, [(0, 40), (110, 5), (200, 40)])
    convs, _t, _p = proc.plan_conversations(con, "DEVA", proc.cfgmod.load(proc.DATA))
    assert lens(convs) == [45, 40]


def test_tiny_with_no_predecessor_joins_the_next(proc):
    con = tiny_world(proc, [(0, 5), (80, 40)])
    convs, tiny, _p = proc.plan_conversations(con, "DEVA", proc.cfgmod.load(proc.DATA))
    assert lens(convs) == [45] and tiny == set()


def test_tiny_far_from_everything_is_a_fragment_and_still_transcribed(proc, fake_whisper):
    tiny_world(proc, [(0, 40), (400, 5)])
    fake_whisper.segs = [(0.0, 1.5, "hello there friend " * 12)]    # plenty of words: only tiny can flag it
    con, n = run(proc)
    assert n == 2
    rows = con.execute("SELECT audio_s, kind, importance, kind_reasons FROM conversations ORDER BY start_ms").fetchall()
    assert rows[0][1] == "conversation"
    assert rows[1][1:3] == ("fragment", 0) and json.loads(rows[1][3])[-1] == "tiny"


def test_merged_tiny_conversation_writes_one_section(proc, fake_whisper):
    tiny_world(proc, [(0, 40), (130, 5)])
    con, n = run(proc)
    assert n == 1
    assert con.execute("SELECT COUNT(*), MIN(start_ms), MAX(end_ms), MAX(audio_s) FROM conversations").fetchone() \
        == (1, at(0), at(0) + 134_000, 45.0)


def test_a_conversation_of_60s_or_more_is_never_merged(proc):
    cfg = proc.cfgmod.load(proc.DATA)
    cfg["grouping"] = {"tiny_audio_s": 500, "tiny_join_gap_s": 120}     # the cap keeps 60 s conversations whole
    con = tiny_world(proc, [(0, 70), (140, 61)])
    convs, tiny, _p = proc.plan_conversations(con, "DEVA", cfg)
    assert lens(convs) == [70, 61] and tiny == set()
    assert proc.tiny_settings(cfg)[0] == 60.0


def test_gap_over_the_limit_is_not_joined(proc):
    con = tiny_world(proc, [(0, 40), (162, 5)])       # 122 s of quiet
    convs, tiny, _p = proc.plan_conversations(con, "DEVA", proc.cfgmod.load(proc.DATA))
    assert lens(convs) == [40, 5] and len(tiny) == 1


def test_settings_can_turn_it_off_and_bad_values_fall_back(proc):
    con = tiny_world(proc, [(0, 40), (100, 5)])
    off = {"grouping": {"tiny_audio_s": 0}}
    convs, tiny, _p = proc.plan_conversations(con, "DEVA", off)
    assert lens(convs) == [40, 5] and tiny == set()
    assert proc.tiny_settings({"grouping": {"tiny_audio_s": "x", "tiny_join_gap_s": -3}}) == (20, 120)
    assert proc.tiny_settings({"grouping": 7}) == (20, 120)


def test_never_joins_across_transcribed_audio(proc):
    con = tiny_world(proc, [(0, 40), (70, 40), (130, 5)])
    cfg = proc.cfgmod.load(proc.DATA)
    # the middle conversation was written earlier: its chunks are done
    con.execute("UPDATE chunks SET done=1 WHERE ts_ms >= ? AND ts_ms < ?", (at(0) + 70_000, at(0) + 110_000))
    convs, tiny, _p = proc.plan_conversations(con, "DEVA", cfg)
    assert lens(convs) == [40, 5] and len(tiny) == 1   # the 5 s one has no untranscribed neighbour close enough


def test_never_joins_across_an_enrollment_window_or_a_deleted_span(proc):
    cfg = proc.cfgmod.load(proc.DATA)
    con = tiny_world(proc, [(0, 40), (130, 5)])
    con.execute("INSERT INTO enrollments (device, start_ms, end_ms, lo_ms, hi_ms, status, name) "
                "VALUES ('DEVA', 0, 0, ?, ?, 'added', 'x')", (at(0) + 60_000, at(0) + 100_000))
    assert lens(proc.plan_conversations(con, "DEVA", cfg)[0]) == [40, 5]
    con.execute("DELETE FROM enrollments")
    assert lens(proc.plan_conversations(con, "DEVA", cfg)[0]) == [45]
    con.execute("INSERT INTO range_tombstones VALUES ('t', 'DEVA', ?, ?)", (at(0) + 70_000, at(0) + 90_000))
    assert lens(proc.plan_conversations(con, "DEVA", cfg)[0]) == [40, 5]
    con.execute("DELETE FROM range_tombstones")
    con.execute("INSERT INTO tombstones VALUES ('DEVA', 1, 0, 'h', ?, 't')", (at(0) + 80_000,))
    assert lens(proc.plan_conversations(con, "DEVA", cfg)[0]) == [40, 5]


def test_a_failing_merge_pass_falls_back_to_the_plain_grouping(proc, fake_whisper, monkeypatch):
    tiny_world(proc, [(0, 40), (130, 5)])
    monkeypatch.setattr(proc, "merge_tiny", lambda *a, **k: 1 / 0)
    con, n = run(proc)
    assert n == 2
    assert con.execute("SELECT COUNT(*) FROM conversations WHERE kind_reasons LIKE '%tiny%'").fetchone()[0] == 0


def test_star_in_a_tiny_conversation_moves_to_the_merged_conversation(proc, fake_whisper):
    press = at(0) + 132_000
    tiny_world(proc, [(0, 40), (130, 5)],
               extra=[button_msg(page_index=99, ts_ms=at(0) + 135_000, presses=[(1, press)])])
    con, n = run(proc)
    assert n == 1
    assert con.execute("SELECT conv_device, conv_start_ms FROM stars").fetchone() == ("DEVA", at(0))
    assert "⭐" in daily_for(proc).read_text()


def test_plan_day_counts_without_writing(proc, tmp_path):
    con = tiny_world(proc, [(0, 40), (130, 5), (500, 40), (900, 6)])
    con.commit()
    before = (proc.DB.read_bytes(), sorted(p.name for p in tmp_path.iterdir()))
    ro = proc.sqlite3.connect(f"file:{proc.DB}?mode=ro", uri=True)
    res = proc.plan_day(ro, "2026-01-05", proc.cfgmod.load(proc.DATA))
    assert (res["conversations_before"], res["conversations_after"]) == (4, 3)
    assert (res["tiny_before"], res["tiny_merged"], res["tiny_flagged"], res["summary_calls_saved"]) == (2, 1, 1, 1)
    assert res["merged_60s_or_more"] == 0
    ro.close()
    assert before == (proc.DB.read_bytes(), sorted(p.name for p in tmp_path.iterdir()))


def mark_one_done(con, ts_ms):
    """One chunk of the page at ts_ms is already transcribed; the page's others stay pending."""
    con.execute("UPDATE chunks SET done=1 WHERE rowid = (SELECT MIN(rowid) FROM chunks WHERE ts_ms=?)", (ts_ms,))


@pytest.mark.parametrize("edge", ["left", "right"])
def test_written_chunk_at_either_endpoint_blocks_the_join(proc, edge):
    con = tiny_world(proc, [(0, 40), (130, 5)])
    cfg = proc.cfgmod.load(proc.DATA)
    mark_one_done(con, at(0) + (39_000 if edge == "left" else 130_000))
    convs = proc.plan_conversations(con, "DEVA", cfg)[0]
    assert [len(c) // CHUNKS_PER_PAGE for c in convs] != [45]
    assert len(convs) == 2


@pytest.mark.parametrize("edge", ["left", "right"])
def test_page_tombstone_at_either_endpoint_blocks_the_join(proc, edge):
    con = tiny_world(proc, [(0, 40), (130, 5)])
    cfg = proc.cfgmod.load(proc.DATA)
    con.execute("INSERT INTO tombstones VALUES ('DEVA', 1, 0, 'h', ?, 't')",
                (at(0) + (39_000 if edge == "left" else 130_000),))
    assert len(proc.plan_conversations(con, "DEVA", cfg)[0]) == 2


def test_plan_day_uses_the_same_chunk_filter_as_live_grouping(proc):
    con = tiny_world(proc, [(0, 40), (130, 5)])
    # the 5 s clip's audio was dropped (retention): live grouping never sees it
    con.execute("UPDATE chunks SET opus=NULL WHERE ts_ms >= ?", (at(0) + 130_000,))
    cfg = proc.cfgmod.load(proc.DATA)
    assert [len(c) for c in proc.conversations(con, "DEVA", False, at(0) - 1000, at(0) + 999_000)] == [40 * CHUNKS_PER_PAGE]
    res = proc.plan_day(con, "2026-01-05", cfg)
    assert (res["conversations_before"], res["conversations_after"], res["tiny_before"]) == (1, 1, 0)
