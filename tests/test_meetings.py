import datetime as dt
from pathlib import Path

from pendant_direct import meetings

GRANOLA = """---
date: 2026-11-08
source: granola
created_at: 2026-11-08 at 14:00
people:
- Kai
title: Noah-Kai weekly
type: transcript
---

# Noah-Kai weekly

### Notes

- something

## Transcript

**Me** *(14:01:05)*: Morning Kai.

**Them** *(14:01:09)*: Hey, how's it going?

**Me** *(14:29:50)*: Talk next week.
"""

NO_TRANSCRIPT = """---
date: 2026-11-08
source: wispr
created_at: 2026-11-08 at 16:00
---
# Standup
"""


def ms(h, m, s=0):
    return int(dt.datetime(2026, 11, 8, h, m, s, tzinfo=meetings.TZ).timestamp() * 1000)


def test_parse_granola_spans_and_utterances():
    m = meetings.parse(Path("Noah-Kai weekly 20261108.md"), GRANOLA)
    assert m.source == "granola" and m.title == "Noah-Kai weekly" and m.people == ["Kai"]
    assert m.start_ms == ms(14, 1, 5) and m.end_ms == ms(14, 29, 50) + 30_000
    assert m.utterances[1] == (ms(14, 1, 9), "Them", "Hey, how's it going?")
    assert m.link == "Noah-Kai weekly 20261108"


def test_parse_without_transcript_uses_created_at_and_ignores_other_sources():
    m = meetings.parse(Path("Standup.md"), NO_TRANSCRIPT)
    assert m.start_ms == ms(16, 0) and m.end_ms == ms(16, 30)
    assert meetings.parse(Path("x.md"), "---\nsource: limitless\ndate: 2026-11-08\n---\n") is None


def test_find_requires_majority_overlap():
    m = meetings.parse(Path("k.md"), GRANOLA)
    assert meetings.find([m], ms(14, 5), ms(14, 25)) is m          # pendant inside the meeting
    assert meetings.find([m], ms(13, 50), ms(14, 10)) is None      # 9 of 20 min shared: not a majority
    assert meetings.find([m], ms(14, 25), ms(15, 0)) is None       # mostly after


def test_load_scans_vault_skipping_pendant_and_archive(tmp_path):
    (tmp_path / "Work" / "Team" / "Meetings").mkdir(parents=True)
    (tmp_path / "Work" / "Team" / "Meetings" / "Noah-Kai weekly 20261108.md").write_text(GRANOLA)
    (tmp_path / "zArchive").mkdir()
    (tmp_path / "zArchive" / "old.md").write_text(GRANOLA)
    (tmp_path / "Pendant" / "Raw").mkdir(parents=True)
    # An otherwise eligible meeting note: only the folder keeps it out.
    (tmp_path / "Pendant" / "Raw" / "2026-11-08.md").write_text(GRANOLA)
    found = meetings.load(tmp_path, {dt.date(2026, 11, 8)})
    assert [m.path.name for m in found] == ["Noah-Kai weekly 20261108.md"]
    assert meetings.load(tmp_path, {dt.date(2026, 11, 9)}) == []


def test_span_handles_identical_stamps_and_appended_sessions():
    words = " ".join(["word"] * 1500)                       # ~10 minutes of talk
    same = [(ms(18, 25), "A", words), (ms(18, 25), "B", "ok")]
    s, e = meetings.span(same)
    assert s == ms(18, 25) and 9 * 60_000 <= e - s <= 11 * 60_000
    appended = [(ms(12, 1), "Me", "hi"), (ms(12, 10), "Them", "yo"), (ms(16, 48), "Me", "later")]
    s, e = meetings.span(appended)
    assert e == ms(12, 10) + 30_000


ONE_ON_ONE = """---
date: 2026-11-08
source: granola
people:
- Kai
title: Noah-Kai weekly
---
## Transcript

**Me** *(14:01:00)*: I reviewed the precinct captain staffing plan and the budget numbers look tight.

**Them** *(14:02:00)*: Agreed, the recruiting pipeline for captains stalled after the retreat, honestly.

**Me** *(14:03:00)*: Let's revisit captain budget numbers Thursday with finance.
"""


def test_meeting_names():
    m = meetings.parse(Path("k.md"), ONE_ON_ONE)
    assert meetings.meeting_names(m, "Noah") == {"Me": "Noah", "Them": "Kai"}
    group = meetings.parse(Path("g.md"), GRANOLA.replace("title: Noah-Kai weekly", "title: Staff sync")
                           .replace("- Kai", "- Kai\n- Dana"))
    assert meetings.meeting_names(group, "Noah") == {"Me": "Noah"}
    wispr = meetings.parse(Path("w.md"), NO_TRANSCRIPT)
    assert meetings.meeting_names(wispr) == {}


def test_map_speakers_aligns_pendant_tags_to_meeting_speakers():
    m = meetings.parse(Path("k.md"), ONE_ON_ONE)
    paras = [
        (ms(14, 1), "Speaker A", "I reviewed the precinct captain staffing plan and budget numbers look tight"),
        (ms(14, 2), "Speaker B", "agreed the recruiting pipeline for captains stalled after the retreat honestly"),
        (ms(14, 3), "Speaker A", "let's revisit captain budget numbers Thursday with finance"),
    ]
    assert meetings.map_speakers(paras, m, meetings.meeting_names(m, "Noah")) == {"Speaker A": "Noah", "Speaker B": "Kai"}


def test_map_speakers_needs_evidence():
    m = meetings.parse(Path("k.md"), ONE_ON_ONE)
    assert meetings.map_speakers([(ms(14, 1), "Speaker A", "totally unrelated words here")], m,
                                 meetings.meeting_names(m, "Noah")) == {}


def test_section_paragraphs_and_rename():
    body = "[14:01] **Speaker A:** hello there\n\n[14:02] **Speaker B:** hi\n\n[14:03] plain"
    paras = meetings.section_paragraphs(body, dt.date(2026, 11, 8))
    assert [(p[1], p[2]) for p in paras] == [("Speaker A", "hello there"), ("Speaker B", "hi"), (None, "plain")]
    assert meetings.rename_tags(body, {"Speaker A": "Noah"}).count("**Noah:**") == 1


def test_section_paragraphs_reads_speaker_after_a_star():
    paras = meetings.section_paragraphs("[10:02] ⭐ **Speaker B:** key point", dt.date(2026, 11, 8))
    assert [(p[1], p[2]) for p in paras] == [("Speaker B", "key point")]
