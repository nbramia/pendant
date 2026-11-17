"""A person's voice split in two by the clustering (speakers.split_voice_cosine): a second voice may
share a name already given in the conversation only when its centroid is close to the voice holding
it, in cluster naming, turn rescoring, the processor and the offline eval. Fake embeddings."""
from __future__ import annotations

import io
import json

import numpy as np
import pytest
from test_eval_speakers import fill
from test_process import fake_embedder, fake_whisper, proc, run  # noqa: F401
from test_turn_rescore import conversation, write_two_voices
from test_voices import env  # noqa: F401

from pendant_direct import calibration, eval_speakers, speakers

E = np.eye(4, dtype=np.float32)
PRINTS = {"Noah": (E[0], 5), "Alex": (E[1], 5)}


def unit(*v):
    return speakers.normalize(np.array(v, dtype=np.float32))


def test_a_split_voice_shares_the_name_only_when_close_to_the_voice_holding_it():
    close = np.stack([unit(1, 0, 0.2, 0), unit(1, 0, 0, 0.3)])      # Noah 0.98 and 0.96; 0.94 apart
    far = np.stack([unit(1, 0, 1.2, 0), unit(1, 0, 0, 1.2)])        # both Noah 0.64; 0.41 apart
    assert speakers.match(close, PRINTS, 0.55) == ["Noah", None]
    assert speakers.match(close, PRINTS, 0.55, split_cosine=0.5) == ["Noah", "Noah"]
    assert sorted(speakers.match(far, PRINTS, 0.55, split_cosine=0.5), key=str) == ["Noah", None]


def test_a_second_voice_still_has_to_clear_the_person_on_its_own():
    cents = np.stack([unit(1, 0, 0.3, 0), unit(1, 0, 2.0, 0)])     # the second: Noah 0.45, 0.69 from the first
    assert speakers.match(cents, PRINTS, 0.55, split_cosine=0.5) == ["Noah", None]
    # and to beat everyone else by the margin
    cents = np.stack([unit(1, 0, 0.3, 0), unit(1, 0.95, 0.3, 0)])
    assert speakers.match(cents, PRINTS, 0.55, margin=0.1, split_cosine=0.5) == ["Noah", None]


def test_a_third_voice_must_be_close_to_every_voice_holding_the_name():
    cents = np.stack([unit(1, 0, 0.3, 0), unit(1, 0, 1.0, 0), unit(1, 0, -1.0, 0)])
    # the third is close to the first (0.53) but not the second (0)
    assert speakers.match(cents, PRINTS, 0.55, split_cosine=0.5) == ["Noah", "Noah", None]


def test_the_calibrated_scorer_shares_a_name_the_same_way():
    samples = {"Noah": (np.stack([E[0]] * 3), [1, 2, 3]), "Alex": (np.stack([E[1]] * 3), [4, 5, 6])}
    cal = calibration.Calibration(samples, calibration.Cohort.empty(4), mode="topk", raw_threshold=0.55)
    close = np.stack([unit(1, 0, 0.2, 0), unit(1, 0, 0, 0.3)])
    assert speakers.match(close, {}, 0.55, calibration=cal) == ["Noah", None]
    assert speakers.match(close, {}, 0.55, calibration=cal, split_cosine=0.5) == ["Noah", "Noah"]


def rescore(split_cosine):
    """Voice 0 is named Noah; voice 1 is unnamed (its centroid scores 0.64 for Noah, under the 0.7
    threshold) and lies 0.64 from voice 0. Its first turn is clearly Noah (0.89) but sounds a little
    more like its own voice than like voice 0."""
    segs, emb = conversation([(E[0], 4), (unit(1, 0, 0.5, 0), 4), (unit(0.3, 0, 1, 0), 4)])
    words = [w for s in segs for w in s.words]
    wseg = [i for i, s in enumerate(segs) for _w in s.words]
    windows, _v = speakers.voice_windows(words)
    x = speakers.normalize(emb(None, windows))
    centre = np.array([(a + b) / 2 for a, b in windows])
    cents = speakers.normalize(np.stack([x[centre < 4.15].mean(0), x[centre >= 4.15].mean(0)]))
    wc = [0 if i < 1 else 1 for i in wseg]
    out, _c, _n, info = speakers.rescore_turns(words, wseg, wc, windows, x, cents, ["Noah", None],
                                               speakers.TurnScorer(PRINTS, threshold=0.7, split_cosine=split_cosine))
    return info["moves"]


def test_an_unnamed_split_voice_gives_its_clear_turns_to_the_person_only_when_close():
    assert rescore(None) == {}                  # the turn sounds more like its own voice: it stays
    assert rescore(0.5) == {(1, 0): 1}          # its voice could share Noah's name: the turn joins Noah
    assert rescore(0.7) == {}                   # the two voices are 0.64 apart, under 0.7


@pytest.mark.parametrize("value", [None, 0.42])
def test_the_processor_hands_the_setting_to_naming_and_rescoring(proc, fake_whisper, fake_embedder, monkeypatch,
                                                                  value):
    A, B = E[0], unit(0.3, 0.954, 0, 0)
    write_two_voices(proc, fake_whisper, fake_embedder, [(A, 4), (B, 4), (A, 4)])
    cfg = json.loads((proc.DATA / "config.json").read_text())
    cfg["speakers"]["split_voice_cosine"] = value
    (proc.DATA / "config.json").write_text(json.dumps(cfg))
    seen_match, seen_turns = [], []
    real_match, real_rescore = speakers.match, speakers.rescore_turns
    monkeypatch.setattr(speakers, "match", lambda *a, **k: seen_match.append(k.get("split_cosine")) or real_match(*a, **k))
    monkeypatch.setattr(speakers, "rescore_turns",
                        lambda *a, **k: seen_turns.append(a[7].split_cosine) or real_rescore(*a, **k))
    run(proc)
    assert seen_match and all(s == value for s in seen_match)
    assert seen_turns == [value]


def test_split_voices_never_share_a_name_by_default():
    assert speakers.naming_settings({}) == {"split_cosine": None}
    assert speakers.naming_settings({"split_voice_cosine": 0.5}) == {"split_cosine": 0.5}


def test_the_eval_mirrors_the_setting(env):
    fill(env, np.random.default_rng(5))
    strict = eval_speakers.context_eval(env.data, io.StringIO(), scales=(1.0,), people={},
                                        overrides={"split_voice_cosine": None})[0]
    split = eval_speakers.context_eval(env.data, io.StringIO(), scales=(1.0,), people={},
                                       overrides={"split_voice_cosine": 0.5})[0]
    assert (strict["base_correct"], strict["base_wrong"]) == (8, 0)
    # both halves of the split Noah voice are Noah; the rule's cost: the Noah-like Alex voice is close
    # to the clear Noah voice beside it, so it shares that name too
    assert (split["base_correct"], split["base_wrong"]) == (9, 1)
