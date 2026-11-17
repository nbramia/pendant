"""The offline eval names each conversation's voices together, as live naming does (each name at most
once per conversation, best pairs first), with per-voice scoring behind `independent`. Fake embeddings.
These pin the strict rule (split_voice_cosine null); test_split_voice covers split voices sharing a name."""
from __future__ import annotations

import io

import numpy as np
from test_voices import env, unit  # noqa: F401

from pendant_direct import eval_speakers

NOAH, ALEX = 0, 1
STRICT = {"split_voice_cosine": None}


def voice(center: int, rng, noise: float = 0.05, toward: int | None = None, mix: float = 0.0) -> np.ndarray:
    v = np.zeros(16, np.float32)
    v[center] = 1.0
    if toward is not None:
        v[toward] = mix
    return unit(v + rng.normal(0, noise, 16).astype(np.float32))


def fill(env, rng):
    """Three hand-named voices each for Noah and Alex, one per conversation; then a conversation
    where Noah's voice was split in two (both hand-named Noah), and one where a voice the wearer
    named Alex sounds more like Noah than Alex, next to a clearer Noah voice."""
    for i in range(3):
        env.add(voice(NOAH, rng), tag="Noah", label="Noah", labeled_by="user", start_ms=10 + i)
        env.add(voice(ALEX, rng), tag="Alex", label="Alex", labeled_by="user", start_ms=20 + i)
    env.add(voice(NOAH, rng), tag="Noah", label="Noah", labeled_by="user", start_ms=3000)
    env.add(voice(NOAH, rng), tag="Noah", label="Noah", labeled_by="user", start_ms=3000)
    env.add(voice(NOAH, rng, noise=0.01), tag="Noah", label="Noah", labeled_by="user", start_ms=4000)
    env.add(voice(NOAH, rng, toward=ALEX, mix=0.6), tag="Alex", label="Alex", labeled_by="user", start_ms=4000)


def test_context_eval_names_each_conversation_once_per_name(env):
    fill(env, np.random.default_rng(5))
    joint = eval_speakers.context_eval(env.data, io.StringIO(), scales=(1.0,), people={}, overrides=STRICT)[0]
    alone = eval_speakers.context_eval(env.data, io.StringIO(), scales=(1.0,), people={}, independent=True)[0]
    # on its own every Noah voice is Noah, and the Noah-like Alex voice is named Noah too
    assert (alone["base_correct"], alone["base_wrong"]) == (9, 1)
    # together, Noah goes once per conversation: one split half stays unnamed, and the Alex voice loses
    # the name to the clearer Noah voice beside it
    assert (joint["base_correct"], joint["base_wrong"]) == (8, 0)


def test_joint_names_follow_live_matching_and_leave_the_conversation_out(env):
    fill(env, np.random.default_rng(5))
    import sqlite3
    con = sqlite3.connect(f"file:{env.data / 'pages.db'}?mode=ro", uri=True)
    labeled, strangers, _auto = eval_speakers.load_voices(con)
    voices = eval_speakers.conversation_voices(con, 16)
    con.close()
    tests = [(c, k, v) for c, k, _n, v in labeled if k[1] == 3000]
    got = eval_speakers.joint_names(tests, labeled, strangers, voices, 0.5, 0.0)
    assert sorted(got, key=str) == ["Noah", None]
    # this conversation's own hand labels never reach its voiceprints
    prints = eval_speakers.loco_prints(labeled, tests[0][1])
    assert prints["Noah"][1] == 4 and prints["Alex"][1] == 4


def test_naming_eval_counts_the_once_per_conversation_rule(env):
    fill(env, np.random.default_rng(5))
    joint = {(r["threshold"], r["margin"]): r
             for r in eval_speakers.naming_eval(env.data, io.StringIO(), (0.5,), (0.0,), overrides=STRICT)}
    alone = {(r["threshold"], r["margin"]): r
             for r in eval_speakers.naming_eval(env.data, io.StringIO(), (0.5,), (0.0,), independent=True)}
    assert alone[(0.5, 0.0)]["named"] == 10 and alone[(0.5, 0.0)]["wrong"] == 1
    assert joint[(0.5, 0.0)]["named"] == 8 and joint[(0.5, 0.0)]["wrong"] == 0
