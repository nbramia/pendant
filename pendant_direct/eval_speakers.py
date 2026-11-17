"""Offline evaluation of speaker scoring: `python -m pendant_direct.eval_speakers [--data DIR]`.

Reads (never writes) `pages.db` and `config.json` in DIR (default: PENDANT_DATA, else
~/pendant-direct-data) and prints counts and accuracies only, no transcript text.

Hand answers are the test set: voices the wearer named himself (`labeled_by = 'user'`
with a name; Voice Scoring and transcript corrections both end up there) and the
voices he called "a random person" (`strangers`). Each person's samples are rebuilt
from those answers alone and every voice is scored leave-one-out: its own sample
is left out of its person's samples, and its conversation out of the cohort. The
voiceprint files are not used, because their legacy history is one opaque sum
that a voice can't be taken out of.

Three scoring modes (`speakers.scoring`) see the same samples:
  mean        cosine to the person's mean embedding, named at or above speakers.match_threshold
  topk        mean of the 3 closest samples' cosines, same threshold
  calibrated  top-k with adaptive s-norm (calibration.py), at speakers.match_threshold_calibrated

accuracy    share of voices whose person has other samples that got their right name
false names share of all test voices given a wrong name (strangers and people with no
            other sample can only be named wrongly)
other-person trials: every (voice, person) pair where the person isn't the voice's; accepted
            when its score reaches the threshold (a verification false-accept count)
A table puts the modes side by side at the shipped threshold and at each mode's suggested one
(highest accuracy with at most 5% false names and at most 5% of other-person trials accepted;
the middle of the tied range), with how many voices named automatically (`labeled_by =
'match'`) each would still name the same and how many unlabeled voices it would name; then a
threshold sweep per mode.

Context mode (`--context`) scores the same hand voices with the configured mean scorer, threshold and
margin, without and with context bars (context.py) at 1x and 2x the configured sizes: correct and wrong
names, the conversations in a work setting and where bars bind, and each voice a bar changed. The
setting comes from the stored calendar match (conversations.calendar_event), the categories from LifeOS.
It and naming mode (`--naming`) name each conversation's voices together, as live naming does: every
voice of the conversation (not noise, not mixed) goes through `speakers.match` at once, so a name goes
to at most one of them, best pairs first, against voiceprints rebuilt from the hand labels outside that
conversation (leave one conversation out). `--independent` scores each voice on its own instead
(leave-one-out), as the scoring-mode table does.

Line mode (`--lines [--notes DIR] [--day YYYY-MM-DD] [--cache DIR]`) asks who said each
line instead. Its test set is the lines the wearer labelled by hand: lines he reassigned one by
one, and the lines of voices he named himself (all lines of a conversation with one voice
and no tags). Each such conversation's speaker step is replayed on its clip, leave-one-out
(its own voices and lines are left out of the voiceprints, strangers and cohort), and a
line's label is the label covering most of its words' speech (matched by text to a fresh
transcription; a line is scored only when at least half its words, and at least 3, are
reproduced, and the lines left out are counted).
It prints line accuracy with the cluster-level labels (before) and after turn rescoring,
by line and by speech time, and what rescoring moved. `--scoring`, `--margin`,
`--margin-calibrated` and `--person-floor` override the config; `--cache` keeps transcriptions and window
embeddings so a sweep only pays for them once. Voices the wearer named by hand are labelled
per voice, so on them the before column is the cluster-level answer he accepted: a turn
moved off one of them counts against rescoring.

Wearer fit (`--fit-wearer [--include-auto] [--save]`, same --notes/--day/--cache/--scoring)
replays the same way, labels each rescored turn from the lines the wearer labelled himself (with
--include-auto also voices named by voiceprint, never ones the wearer signal named: those labels
lean on the score the model uses, so their accuracy is optimistic), fits the near-field wearer model (wearer.py), and reports
held-out accuracy (leave one conversation out) next to score-only and acoustic-only models,
the voices whose name the model would change and line accuracy without and with it. It writes
`speakers.wearer_model` to config.json in --data only with --save.

Naming spaces (`--spaces [--cache DIR] [--models DIR]`) re-embed the hand-named voices whose audio
is kept (naming.voice_audio), and every voice of their conversations, with ECAPA and WeSpeaker
ResNet293, each as the mean of 1.5 s windows and as one whole utterance, and name the conversations
jointly with voiceprints rebuilt from that audio. Each space is read at the threshold accepting
other-person trials as often as the stored ECAPA embeddings at match_threshold ("matched"). Line mode
names in a space with `--set embedder=NAME naming=window|whole` (voiceprints rebuilt the same way;
`--set voiceprints=rebuilt` does that for ECAPA windows too, the like-for-like control).
`--set voiceprints=whole` (both modes) rebuilds them from the kept whole voices instead (voice_audio.py:
each hand-named person's lines in a conversation, embedded in up to 30 pieces of 20 s, or up to 2,400
windows), with the voices no kept voice covers still from their clips; `voiceprints=whole-only` uses the
kept whole voices alone. In `--spaces` a kept whole voice is also the test embedding of the voice it is
filed under.

Turn mode (`--turns`, same --notes/--day/--cache, plus `--only HH:MM`, `--voices` and `--set
KEY=VALUE`) replays every conversation of the day the same way and rebuilds its paragraphs as the
processor writes them (pauses from the chunk clock). Per conversation, as written in the Raw note
and as replayed (with --set, also with those settings): lines, distinct voices, A–B–A blips of at
most 3 words, paragraphs of 150+ words (all, and in conversations with 2+ voices), line accuracy on
the hand-labelled lines and the lines the wearer reassigned (wrong as written). --voices adds each
voice's speech seconds and closest voiceprints.
"""
from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys
from pathlib import Path

import numpy as np

from . import calibration, speakers
from .config import DEFAULTS, load as load_config, wearer as wearer_name
from .voices import GENERIC as GENERIC_TAG

SMALL_N = 30
MAX_FALSE = 0.05


def _unit(x):
    x = np.asarray(x, dtype=np.float32)
    return x / np.maximum(np.linalg.norm(x, axis=-1, keepdims=True), 1e-9)


def load_voices(con: sqlite3.Connection):
    """(labeled, strangers, auto): labeled = [(id, key, name, emb)], strangers and auto likewise
    ([(id, key, name_or_None, emb)])."""
    labeled, strangers, auto = [], [], []
    cols = {r[1] for r in con.execute("PRAGMA table_info(speaker_clusters)")}
    # a mixed voice (two people) is never anyone's sample
    not_mixed = " AND COALESCE(mixed, 0) = 0" if "mixed" in cols else ""
    for cid, dev, ms, label, by, kind, emb in con.execute(
            "SELECT id, device, start_ms, label, labeled_by, label_kind, embedding FROM speaker_clusters "
            "WHERE embedding IS NOT NULL" + not_mixed):
        v = np.frombuffer(emb, dtype=np.float32)
        key = (dev, ms)
        if by == "user" and kind == "stranger":
            strangers.append((cid, key, None, v))
        elif by == "user" and label and kind in (None, "person"):
            labeled.append((cid, key, label, v))
        elif by == "match" and label:
            auto.append((cid, key, label, v))
    try:
        ids = {r[0] for r in con.execute("SELECT cluster_id FROM strangers")}
        have = {s[0] for s in strangers}
        for cid, dev, ms, emb in con.execute("SELECT id, device, start_ms, embedding FROM speaker_clusters"):
            if cid in ids and cid not in have and emb:
                strangers.append((cid, (dev, ms), None, np.frombuffer(emb, dtype=np.float32)))
    except sqlite3.OperationalError:
        pass
    dim = max((len(v[3]) for v in labeled), default=0)
    ok = lambda rows: [r for r in rows if len(r[3]) == dim]  # noqa: E731
    return ok(labeled), ok(strangers), ok(auto)


def build_samples(labeled) -> dict[str, tuple[np.ndarray, list]]:
    by: dict[str, list] = {}
    for cid, _k, name, v in labeled:
        by.setdefault(name, []).append((cid, v))
    return {n: (_unit(np.stack([v for _c, v in rows])), [c for c, _v in rows]) for n, rows in by.items()}


def old_scores(tests, samples, strangers, cohort_strangers):
    """Per test voice (id, key, emb): {name: cosine to the mean of the person's other samples}
    and the closest stranger cosine (itself left out)."""
    out = []
    for cid, _key, v in tests:
        x = _unit(v)
        sc = {}
        for name, (e, ids) in samples.items():
            keep = [i for i, c in enumerate(ids) if c != cid]
            if keep:
                sc[name] = float(x @ _unit(e[keep].mean(0)))
        near = max((float(x @ _unit(s)) for c, s in cohort_strangers if c != cid), default=-np.inf)
        out.append((sc, near))
    return out


def mode_rows(mode: str, tests, samples, cohort, strangers, old_thr: float, new_thr: float):
    """Per test voice, for a scoring mode: (scores {name: score}, closest stranger cosine,
    raw top-k {name: score} for the stranger veto, configured thresholds {name: cutoff},
    tunable {name: bool} (the score is on the scale the swept threshold applies to))."""
    if mode == "mean":
        return [(sc, near, sc, {n: old_thr for n in sc}, {n: True for n in sc})
                for sc, near in old_scores(tests, samples, None, strangers)]
    dim = len(tests[0][2])
    cal = calibration.Calibration(samples, cohort if mode == "calibrated" else calibration.Cohort.empty(dim),
                                  threshold=new_thr, raw_threshold=old_thr, mode=mode)
    x = np.stack([t[2] for t in tests])
    res = cal.scores(x, exclude=[t[1] for t in tests], self_ids=[t[0] for t in tests])
    out = []
    for i, (cid, _k, v) in enumerate(tests):
        ok = {n: j for j, n in enumerate(cal.names) if np.isfinite(res.match[i, j])}
        near = max((float(_unit(v) @ _unit(s)) for c, s in strangers if c != cid), default=-np.inf)
        out.append(({n: float(res.match[i, j]) for n, j in ok.items()}, near,
                    {n: float(res.raw[i, j]) for n, j in ok.items()},
                    {n: float(res.threshold[i, j]) for n, j in ok.items()},
                    {n: mode != "calibrated" or bool(res.calibrated[i, j]) for n, j in ok.items()}))
    return out


def decide(sc: dict, near: float, thr, ref: dict | None = None, margin: float = 0.0):
    """The name given: the person clearing their cutoff by the most (`thr` is one number or
    {name: cutoff}), if they reach it and no stranger is closer than their raw top-k score.
    With `margin` (speakers.match_margin) they must also beat every other person's raw score
    by that much."""
    if not sc:
        return None
    t = (lambda n: thr[n]) if isinstance(thr, dict) else (lambda n: thr)
    name = max(sc, key=lambda n: sc[n] - t(n))
    if sc[name] < t(name):
        return None
    raw = ref or sc
    if near > raw[name] or (margin > 0 and not speakers._clear_winner([raw[n] for n in raw], list(raw).index(name), margin)):
        return None
    return name


def conversation_voices(con: sqlite3.Connection, dim: int) -> dict:
    """{(device, start_ms): [(id, embedding)]}: the voices of each conversation that live naming
    scores together, as far as the database shows them: every voice with an embedding that isn't
    noise or mixed (a mixed voice's stored embedding is what was left after rescoring, not what
    naming saw)."""
    cols = {r[1] for r in con.execute("PRAGMA table_info(speaker_clusters)")}
    mixed = " AND COALESCE(mixed, 0) = 0" if "mixed" in cols else ""
    out: dict = {}
    for cid, dev, ms, emb in con.execute("SELECT id, device, start_ms, embedding FROM speaker_clusters WHERE "
                                         "embedding IS NOT NULL AND COALESCE(noise, 0) = 0" + mixed + " ORDER BY id"):
        v = np.frombuffer(emb, dtype=np.float32)
        if len(v) == dim:
            out.setdefault((dev, ms), []).append((cid, v))
    return out


def short_voices(con: sqlite3.Connection, sp: dict) -> set[int]:
    """Ids of the voices with less speech than speakers.min_sample_speech_s: never a voiceprint
    sample, so the eval leaves them out of the voiceprints it rebuilds (they are still scored)."""
    min_s = float(sp.get("min_sample_speech_s", DEFAULTS["speakers"]["min_sample_speech_s"]) or 0)
    if min_s <= 0:
        return set()
    return {r[0] for r in con.execute("SELECT id FROM speaker_clusters WHERE COALESCE(speech_s, 0) < ?", (min_s,))}


def loco_prints(labeled, key) -> dict:
    """{name: (mean unit embedding, sample count)} from the hand-named voices outside conversation `key`."""
    by: dict = {}
    for _c, k, name, v in labeled:
        if k != key:
            by.setdefault(name, []).append(_unit(v))
    return {n: (_unit(np.mean(vs, 0)), len(vs)) for n, vs in by.items()}


def joint_names(tests, labeled, strangers, voices, threshold: float, margin: float, extra_of=None,
                settings: dict | None = None) -> list:
    """The name each test voice (id, key, embedding) gets when its conversation is named as live
    naming names it: `speakers.match` over all of the conversation's voices (`voices`, from
    conversation_voices) at once, so each name goes to at most one voice there, best pairs first.
    Voiceprints are rebuilt from the hand-named voices outside that conversation (leave one
    conversation out) and the strangers are those of other conversations. extra_of(key): that
    conversation's context bars; settings: the rest of match's keywords (speakers.naming_settings)."""
    got: dict = {}
    for key in dict.fromkeys(k for _c, k, _v in tests):
        rows = list(voices.get(key, ()))
        have = {c for c, _v in rows}
        rows += [(c, v) for c, k, v in tests if k == key and c not in have]
        st = [v for _c, k, _n, v in strangers if k != key]
        names = speakers.match(np.stack([_unit(v) for _c, v in rows]), loco_prints(labeled, key), threshold,
                               np.stack(st) if st else None, margin=margin,
                               extra=extra_of(key) if extra_of else None, **(settings or {}))
        got.update({c: n for (c, _v), n in zip(rows, names)})
    return [got.get(c) for c, _k, _v in tests]


def name_changes(base, got, truths) -> dict:
    """{transition: voices} between two namings of the same test voices."""
    moves: dict = {}
    for g0, g1, t in zip(base, got, truths):
        if g1 == g0:
            continue
        if g1 is None:
            kind = "right->unnamed" if g0 == t else "wrong->unnamed"
        else:
            kind = "right->wrong" if g0 == t else "unnamed->named" if g0 is None else "wrong->other"
        moves[kind] = moves.get(kind, 0) + 1
    return moves


def metrics(rows, truths, thr_of, margin: float = 0.0):
    """rows: [(sc, near, raw)], truths: [name or None (stranger)], thr_of(i) -> threshold (or {name: threshold})."""
    pos = ok = wrong = imp = imp_acc = tgt = tgt_acc = 0
    for i, ((sc, near, top1), truth) in enumerate(zip(rows, truths)):
        th = thr_of(i)
        for name, v in sc.items():       # one trial per (voice, person): is this their voice?
            hit = v >= (th[name] if isinstance(th, dict) else th)
            if name == truth:
                tgt += 1
                tgt_acc += hit
            else:
                imp += 1
                imp_acc += hit
        got = decide(sc, near, thr_of(i), top1, margin)
        scorable = truth is not None and truth in sc
        if scorable:
            pos += 1
            ok += got == truth
        if got is not None and got != truth:
            wrong += 1
    return {"pos": pos, "correct": ok, "wrong": wrong, "n": len(rows),
            "imp": imp, "imp_accepted": imp_acc, "tgt": tgt, "tgt_accepted": tgt_acc,
            "fa_rate": imp_acc / imp if imp else 0.0,
            "accuracy": ok / pos if pos else float("nan"),
            "false_rate": wrong / len(rows) if rows else float("nan")}


def suggest(grid, ms):
    """Threshold with the highest accuracy among those keeping false names and accepted
    other-person trials within MAX_FALSE; the middle of the tied range, so it sits away
    from both edges."""
    ok = [(t, m) for t, m in zip(grid, ms)
          if m["pos"] and m["false_rate"] <= MAX_FALSE and m["fa_rate"] <= MAX_FALSE]
    if not ok:
        return None
    top = max(m["accuracy"] for _t, m in ok)
    tied = [(t, m) for t, m in ok if m["accuracy"] == top]
    mid = (tied[0][0] + tied[-1][0]) / 2
    return min(tied, key=lambda p: abs(p[0] - mid))


def corrected_wrong_names(con: sqlite3.Connection) -> dict[int, str]:
    """{cluster id: the wrong name it carried}: voices the wearer renamed from one person to another
    (transcript_relabels with a name, not a "Speaker X" tag, as old_tag), matched to the voice
    that now carries the new name. A voice relabelled from a placeholder tag was never a false name."""
    out: dict[int, str] = {}
    try:
        rows = con.execute("SELECT device, start_ms, old_tag, new_tag FROM transcript_relabels "
                           "WHERE old_tag IS NOT NULL AND old_tag != '' AND old_tag NOT LIKE 'Speaker %' "
                           "AND old_tag != new_tag").fetchall()
    except sqlite3.OperationalError:
        return out
    for dev, ms, old, new in rows:
        for (cid,) in con.execute("SELECT id FROM speaker_clusters WHERE device=? AND start_ms=? AND label=? "
                                  "AND labeled_by='user'", (dev, ms, new)):
            out[cid] = old
    return out


def naming_eval(data: Path, out=None, thresholds=(0.5, 0.55, 0.6, 0.65), margins=(0.0, 0.03, 0.05, 0.08, 0.1),
                independent: bool = False, overrides: dict | None = None) -> list[dict]:
    """Voice-level naming trade-off for the "mean" scorer on the wearer's labels. Population: every
    voice that could be named (hand-named, "random person", auto-named and unlabeled). By default each
    conversation is named as live naming names it (joint_names: its voices together, each name at most
    once, against voiceprints without that conversation's hand labels); `independent` scores each hand
    voice on its own (its own sample left out) and the rest against all samples. For each
    (match_threshold, match_margin): naming rate = named / all voices;
    false names = hand-named or stranger voices given a wrong name, plus the voices the wearer
    renamed away from a wrong name (transcript_relabels) that would still get that wrong name.
    `overrides` replace `speakers` settings (split_voice_cosine, ...)."""
    out = out or sys.stdout
    sp = {**load_config(data)["speakers"], **(overrides or {})}
    nk = speakers.naming_settings(sp)
    con = sqlite3.connect(f"file:{Path(data) / 'pages.db'}?mode=ro", uri=True)
    try:
        labeled, strangers, auto = load_voices(con)
        samples = build_samples(labeled)
        wrong_old = corrected_wrong_names(con)
        dim = len(labeled[0][3]) if labeled else 0
        unl = [(r[0], (r[1], r[2]), None, np.frombuffer(r[3], dtype=np.float32)) for r in con.execute(
            "SELECT id, device, start_ms, embedding FROM speaker_clusters WHERE label IS NULL "
            "AND COALESCE(noise, 0) = 0 AND COALESCE(labeled_by, '') != 'dismissed' "
            "AND COALESCE(label_kind, '') NOT IN ('stranger', 'noise') AND embedding IS NOT NULL")]
        unl = [u for u in unl if len(u[3]) == dim]
        voices = conversation_voices(con, dim)
        short = short_voices(con, sp)
    finally:
        con.close()
    cstr = [(c, v) for c, _k, _n, v in strangers]
    hand = [(c, k, v) for c, k, _n, v in labeled] + [(c, k, v) for c, k, _n, v in strangers]
    truths = [n for _c, _k, n, _v in labeled] + [None] * len(strangers)
    rest = [(c, k, v) for c, k, _n, v in auto] + [(c, k, v) for c, k, _n, v in unl]
    rows_h = rows_r = []
    if independent:
        rows_h = [(sc, near, sc) for sc, near in old_scores(hand, samples, None, cstr)]
        rows_r = [(sc, near, sc) for sc, near in old_scores([(-1, k, v) for _c, k, v in rest], samples, None, cstr)]
    n_all = len(hand) + len(rest)
    how = "each voice on its own" if independent else (
        "each conversation's voices together, each name once, split voices "
        + (f"share a name at cosine >= {nk['split_cosine']:g}" if nk["split_cosine"] is not None else "never share one"))
    print(f"naming eval (mean scorer; {how}): {len(hand)} hand voices ({len(strangers)} strangers), {len(rest)} "
          f"auto/unlabeled voices, {len(wrong_old)} voices corrected away from a wrong name", file=out)
    res = []
    for thr in thresholds:
        for mg in margins:
            if independent:
                got_h = [decide(sc, near, thr, ref, mg) for sc, near, ref in rows_h]
                got_r = [decide(sc, near, thr, ref, mg) for sc, near, ref in rows_r]
            else:
                got = joint_names(hand + rest, [r for r in labeled if r[0] not in short], strangers, voices, thr, mg,
                                  settings=nk)
                got_h, got_r = got[:len(hand)], got[len(hand):]
            named = sum(g is not None for g in got_h + got_r)
            wrong = sum(g is not None and g != t for g, t in zip(got_h, truths))
            still = sum(1 for (c, _k, _v), g in zip(hand, got_h) if c in wrong_old and g == wrong_old[c])
            res.append({"threshold": thr, "margin": mg, "named": named, "total": n_all, "naming_rate": named / n_all,
                        "wrong": wrong, "hand": len(hand), "false_rate": wrong / len(hand) if hand else float("nan"),
                        "false_of_named": wrong / max(1, sum(g is not None for g in got_h)),
                        "corrected_still_wrong": still, "corrected": len(wrong_old)})
    print("  threshold margin | naming rate | hand voices named wrong | corrected voices still given the wrong name", file=out)
    for r in res:
        print(f"  {r['threshold']:9.2f} {r['margin']:6.2f} | {r['named']}/{r['total']} = {_pct(r['naming_rate'])} | "
              f"{r['wrong']}/{r['hand']} = {_pct(r['false_rate'])} | {r['corrected_still_wrong']}/{r['corrected']}", file=out)
    return res


def space_embeddings(con: sqlite3.Connection, data: Path, ids, space: tuple, cache: Path | None, ecapa=None,
                     models: Path | None = None, audio: dict | None = None) -> dict[int, np.ndarray]:
    """{voice id: its embedding in `space` (embedder, naming)}, rebuilt from the voice's audio
    (naming.voice_audio: its library clip, else its snippet of the conversation clip) as a rebuild
    of the voiceprints would; voices without audio are left out. Cached per space in `cache`."""
    from . import naming
    name, kind = space
    f = cache / f"space-{name}-{kind}.npz" if cache else None
    have: dict[int, np.ndarray] = {}
    if f and f.exists():
        z = np.load(f)
        have = {int(i): v for i, v in zip(z["ids"], z["emb"])}
    want = [i for i in ids if i not in have]
    if want:
        audio = audio if audio is not None else naming.voice_audio(con, data, want)
        emb = naming.model(name, data, ecapa, models)
        for i in want:
            if i in audio:
                have[i] = naming.embed_audio(emb, audio[i], kind == "whole")
        if f:
            f.parent.mkdir(parents=True, exist_ok=True)
            keys = sorted(have)
            np.savez(f, ids=np.array(keys, dtype=np.int64), emb=np.stack([have[k] for k in keys]) if keys else np.zeros((0, 1)))
    return {i: have[i] for i in ids if i in have}


WHOLE_PRINTS = ("whole", "whole-only")


def whole_embeddings(con: sqlite3.Connection, data: Path, space: tuple, cache: Path | None, ecapa=None,
                     models: Path | None = None) -> tuple[dict[int, np.ndarray], set[int]]:
    """({voice id: embedding in `space`} of each kept whole voice (voice_audio.py: a person's lines in one
    conversation), under the lowest id of the hand-named voices it covers; every id it covers). Cached
    per space in `cache`."""
    from . import naming, voice_audio
    name, kind = space
    labeled = {c for c, *_r in load_voices(con)[0]}
    reps: dict[int, dict] = {}
    covered: set[int] = set()
    for e in voice_audio.entries(data):
        ids = [i for i in e.get("cluster_ids") or () if i in labeled]
        if ids:
            reps[min(ids)] = e
            covered |= set(e.get("cluster_ids") or ())
    f = cache / f"whole-{name}-{kind}.npz" if cache else None
    tag = {c: f"{c}:{e['bytes']}:{len(e['file_segments'])}" for c, e in reps.items()}
    have: dict[int, np.ndarray] = {}
    if f and f.exists():
        z = np.load(f, allow_pickle=False)
        have = {int(i): v for i, t, v in zip(z["ids"], z["tags"], z["emb"]) if tag.get(int(i)) == str(t)}
    want = [c for c in reps if c not in have]
    if want:
        emb = naming.model(name, data, ecapa, models)
        for c in want:
            v = naming.embed_kept(emb, speakers.load_audio(Path(reps[c]["path"])), reps[c]["file_segments"],
                                  kind == "whole")
            if v is not None:
                have[c] = v
        if f:
            f.parent.mkdir(parents=True, exist_ok=True)
            keys = sorted(have)
            np.savez(f, ids=np.array(keys, dtype=np.int64), tags=np.array([tag.get(k, "") for k in keys]),
                     emb=np.stack([have[k] for k in keys]) if keys else np.zeros((0, 1)))
    return {c: have[c] for c in reps if c in have}, covered


def print_pool(labeled, E: dict, mode: str | None, W: dict, covered: set[int], short: set[int]) -> list:
    """The hand-named voices a rebuild in this space makes voiceprints from, [(id, key, name, emb)]: with
    voiceprints=whole each kept whole voice, and the voices no kept voice covers from their clip
    (`E`, naming.voice_audio); whole-only: the kept whole voices alone; otherwise every voice from its clip.
    A voice under speakers.min_sample_speech_s never joins from its clip."""
    if mode not in WHOLE_PRINTS:
        return [(c, k, n, E[c]) for c, k, n, _v in labeled if c in E and c not in short]
    out = [(c, k, n, W[c]) for c, k, n, _v in labeled if c in W]
    if mode == "whole":
        out += [(c, k, n, E[c]) for c, k, n, _v in labeled if c not in covered and c in E and c not in short]
    return out


def matched_threshold(scores_other: np.ndarray, rate: float) -> float:
    """The threshold accepting `rate` of other-person trials (their (1 - rate) quantile)."""
    return float(np.quantile(scores_other, 1 - rate)) if len(scores_other) else float("nan")


def _other_trials(tests, labeled, truth: dict | None = None) -> np.ndarray:
    """Cosines of each test voice (id, key, emb) with every other person's voiceprint rebuilt without
    its conversation (the trials a false accept is counted on). `truth`: each test voice's person
    (default: its name in `labeled`)."""
    truth = truth if truth is not None else {c: n for c, _k, n, _v in labeled}
    out = []
    for c, k, v in tests:
        for n, (mean, _cnt) in loco_prints(labeled, k).items():
            if n != truth.get(c):
                out.append(float(_unit(v) @ _unit(mean)))
    return np.array(out)


SPACES = (("ecapa", "window"), ("ecapa", "whole"), ("wespeaker-resnet293", "window"), ("wespeaker-resnet293", "whole"))


def space_eval(data: Path, spaces=SPACES, cache: Path | None = None, out=None, ecapa=None,
               models: Path | None = None, overrides: dict | None = None) -> list[dict]:
    """Cluster-level naming in each naming space, on the hand-named voices whose audio is kept. Every
    voice of their conversations is re-embedded from its audio (naming.voice_audio) in the space and
    the conversation is named jointly (joint_names: leave one conversation out, the configured
    margin and naming settings). Each space is read at the threshold accepting other-person trials as
    often as the stored ECAPA embeddings do at match_threshold on the same voices ("matched"), and at
    match_threshold itself. Counts only."""
    from . import naming
    out = out or sys.stdout
    sp = {**load_config(data)["speakers"], **(overrides or {})}
    thr, mg = float(sp.get("match_threshold", 0.55)), float(sp.get("match_margin", 0.0))
    nk = speakers.naming_settings(sp)
    mode = sp.get("voiceprints")
    con = sqlite3.connect(f"file:{Path(data) / 'pages.db'}?mode=ro", uri=True)
    try:
        labeled, _strangers, _auto = load_voices(con)
        voices = conversation_voices(con, len(labeled[0][3]) if labeled else 0)
        keys = {k for _c, k, _n, _v in labeled}
        ids = sorted({c for k in keys for c, _v in voices.get(k, ())} | {c for c, *_r in labeled})
        audio = naming.voice_audio(con, data, ids)
        embs = {s: space_embeddings(con, data, ids, s, cache, ecapa, models, audio) for s in spaces}
        wholes = {s: whole_embeddings(con, data, s, cache, ecapa, models) for s in spaces} \
            if mode in WHOLE_PRINTS else {}
    finally:
        con.close()
    have = set(audio) | {c for W, _cov in wholes.values() for c in W}
    lab_s = [r for r in labeled if r[0] in have]
    stored = {c: v for k in keys for c, v in voices.get(k, ())}
    stored.update({c: v for c, _k, _n, v in labeled})
    rows = [("stored ecapa", stored)] + [(naming.label(s), embs[s]) for s in spaces]
    ref = None
    res = []
    print(f"naming spaces (cluster level): {len(lab_s)} of {len(labeled)} hand-named voices have audio; their "
          f"conversations named jointly, leave one conversation out, margin {mg}", file=out)
    if mode in WHOLE_PRINTS:
        W0, cov0 = next(iter(wholes.values()), ({}, set()))
        print(f"  voiceprints={mode}: {len(W0)} kept whole voices cover {len(cov0 & set(c for c, *_r in labeled))} "
              "hand-named voices; a kept voice is its own test embedding and sample in each space", file=out)
    for row, (name, E) in enumerate(rows):
        W, cov = wholes[spaces[row - 1]] if row and mode in WHOLE_PRINTS else ({}, set())
        E = {**E, **W}
        cur = [(c, k, n, E[c]) for c, k, n, _v in lab_s if c in E]
        tests = [(c, k, v) for c, k, _n, v in cur]
        truths = [n for _c, _k, n, _v in cur]
        lab = print_pool(cur, E, mode, W, cov, set()) if W else cur
        vox = {k: [(c, E[c]) for c, _v in voices.get(k, ()) if c in E] for k in keys}
        other = _other_trials(tests, lab, {c: n for c, _k, n, _v in cur})
        if ref is None:
            ref = float((other >= thr).mean()) if len(other) else 0.0
        t = matched_threshold(other, ref)
        r = {"space": name, "threshold": t, "fa": ref}
        for tag, th in (("matched", t), ("shipped", thr)):
            got = joint_names(tests, lab, [], vox, th, mg, settings=nk)
            r[tag] = (sum(g == x for g, x in zip(got, truths)), sum(g is not None and g != x for g, x in zip(got, truths)))
        res.append(r)
        print(f"  {name:28s} matched threshold {t:.3f}: {r['matched'][0]} right, {r['matched'][1]} wrong; "
              f"at {thr:g}: {r['shipped'][0]} right, {r['shipped'][1]} wrong", file=out)
    print(f"  (matched: other-person trials accepted {100 * ref:.1f}% of the time, as stored ECAPA at {thr:g})", file=out)
    return res


def stored_contexts(con: sqlite3.Connection, keys) -> dict:
    """{(device, start_ms): context.Context} from each conversation's start and its stored calendar
    match (conversations.calendar_event), so the eval needs no calendar lookups."""
    from . import calendar_hint, context
    out = {}
    for dev, ms in set(keys):
        try:
            row = con.execute("SELECT calendar_event FROM conversations WHERE device = ? AND start_ms = ?",
                              (dev, ms)).fetchone()
        except sqlite3.OperationalError:
            row = None
        ev = calendar_hint.stored(row[0]) if row else None
        out[(dev, ms)] = context.context_at(int(ms), (ev or {}).get("attendees"))
    return out


def context_people(sp: dict, names, run=None) -> dict | None:
    """{name: {category, keys}} for the linked `names` from LifeOS (through a closeness.PriorRun),
    None when LifeOS can't answer."""
    from . import closeness, config, context
    run = run or closeness.PriorRun()
    links = {n: config.linked_entity(sp.get("people"), n) for n in names}
    links = {n: e for n, e in links.items() if e}
    rows = run._people({n: e for n, e in (sp.get("people") or {}).items() if isinstance(e, str) and e}) if links else {}
    if rows is None:
        return None
    return {n: {"category": (rows.get(e) or {}).get("category"), "keys": context.person_keys(rows.get(e) or {})}
            for n, e in links.items()}


def barred(name, score: float, threshold: float, bars: dict) -> bool:
    """A context bar keeps `name` (decided at `threshold` on raw `score`) from being named."""
    bar = bars.get(str(name).lower(), 0.0) if name else 0.0
    return bool(bar) and score < threshold + bar


def context_eval(data: Path, out=None, scales=(1.0, 2.0), people=None, independent: bool = False,
                 overrides: dict | None = None) -> list[dict]:
    """Context bars (context.py) on the wearer's hand-labelled voices with the configured mean
    scorer, match_threshold and match_margin (as naming_eval). By default each conversation is named
    as live naming names it (joint_names: all its voices at once, each name at most once, leave one
    conversation out); `independent` scores each voice on its own instead (leave-one-out: only its
    own sample left out). The setting of each conversation comes from its start and its stored
    calendar match, each linked person's category from LifeOS (`people` {name: {category, keys}}
    instead, for tests). For each bar scale (x the configured work_off and personal_in_meeting):
    correct and wrong names against the baseline, the labelled conversations in a work setting,
    those where some labelled voice's top person has a bar ("bars present") and those where a bar
    changed a name ("bars bind"), and each changed voice's transition. `overrides` replace `speakers`
    settings (split_voice_cosine, ...). Counts only."""
    from . import context
    out = out or sys.stdout
    sp = {**load_config(data)["speakers"], **(overrides or {})}
    nk = speakers.naming_settings(sp)
    thr, mg = float(sp.get("match_threshold", 0.55)), float(sp.get("match_margin", 0.0))
    cfg = {**context.settings(sp), "enabled": True}
    con = sqlite3.connect(f"file:{Path(data) / 'pages.db'}?mode=ro", uri=True)
    try:
        labeled, strangers, _auto = load_voices(con)
        samples = build_samples(labeled)
        hand = [(c, k, v) for c, k, _n, v in labeled] + [(c, k, v) for c, k, _n, v in strangers]
        truths = [n for _c, _k, n, _v in labeled] + [None] * len(strangers)
        ctxs = stored_contexts(con, [k for _c, k, _v in hand])
        voices = conversation_voices(con, len(hand[0][2]) if hand else 0)
        short = short_voices(con, sp)
    finally:
        con.close()
    pool = [r for r in labeled if r[0] not in short]     # the hand voices that are voiceprint samples
    if people is None:
        people = context_people(sp, list(samples))
        if people is None:
            print("context eval: LifeOS did not answer; no categories", file=out)
            return []
    rows = [(sc, near) for sc, near in old_scores(hand, samples, None, [(c, v) for c, _k, _n, v in strangers])]
    if independent:
        base = [decide(sc, near, thr, sc, mg) for sc, near in rows]
    else:
        base = joint_names(hand, pool, strangers, voices, thr, mg, settings=nk)
    convs = {k for _c, k, _v in hand}
    work = sum(context.is_work(ctxs[k], cfg) for k in convs)
    meetings = sum(ctxs[k].meeting for k in convs)

    def tally(got):
        return (sum(g is not None and g == t for g, t in zip(got, truths)),
                sum(g is not None and g != t for g, t in zip(got, truths)))
    b_ok, b_wrong = tally(base)
    how = ("each voice on its own (leave-one-out)" if independent else
           "each conversation's voices together, each name once (leave one conversation out), split voices "
           + (f"share a name at cosine >= {nk['split_cosine']:g}" if nk["split_cosine"] is not None else "never share one"))
    print(f"context eval (mean scorer, threshold {thr}, margin {mg}; {how}): {len(hand)} hand voices "
          f"({len(strangers)} strangers) in {len(convs)} conversations; {work} in a work setting "
          f"({meetings} with a calendar meeting); {len(people)} linked people", file=out)
    print(f"  baseline: {b_ok} correct, {b_wrong} wrong names", file=out)
    res = []
    for scale in scales:
        c = {**cfg, "work_off": cfg["work_off"] * scale, "personal_in_meeting": cfg["personal_in_meeting"] * scale}
        bars = {k: context.extra_bars(ctxs[k], people, c) for k in convs}
        if independent:
            got = [None if g is not None and barred(g, sc[g], thr, bars[k]) else g
                   for (sc, _near), g, (_c, k, _v) in zip(rows, base, hand)]
        else:
            got = joint_names(hand, pool, strangers, voices, thr, mg, extra_of=bars.get, settings=nk)
        present = {k for g, (_c, k, _v) in zip(base, hand) if g is not None and bars[k].get(g.lower())}
        bind = {k for g0, g1, (_c, k, _v) in zip(base, got, hand) if g0 != g1}
        # bars only ever unname: a name that changed or appeared would be a defect, so it is counted
        moves = name_changes(base, got, truths)
        ok, wrong = tally(got)
        r = {"scale": scale, "work_off": c["work_off"], "personal_in_meeting": c["personal_in_meeting"],
             "correct": ok, "wrong": wrong, "base_correct": b_ok, "base_wrong": b_wrong,
             "conversations": len(convs), "work": work, "meetings": meetings,
             "present": len(present), "bind": len(bind), "moves": moves}
        res.append(r)
        print(f"  bars x{scale:g} (work_off {c['work_off']:.2f}, personal_in_meeting {c['personal_in_meeting']:.2f}): "
              f"{ok} correct, {wrong} wrong names; bars present in {len(present)}, bind in {len(bind)} conversations; "
              f"changes: {', '.join(f'{k} {v}' for k, v in sorted(moves.items())) or 'none'}", file=out)
    return res


def _pct(x):
    return "n/a" if x != x else f"{100 * x:.0f}%"


def evaluate(data: Path, out=None) -> dict:
    out = out or sys.stdout
    cfg = load_config(data)["speakers"]
    db = Path(data) / "pages.db"
    if not db.exists():
        print(f"no pages.db in {data}", file=out)
        return {}
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        labeled, strangers, auto = load_voices(con)
        samples = build_samples(labeled)
        cohort = calibration.build_cohort(con, samples)
        dim = len(labeled[0][3]) if labeled else 0
        unlabeled = [(r[0], (r[1], r[2]), None, np.frombuffer(r[3], dtype=np.float32)) for r in con.execute(
            "SELECT id, device, start_ms, embedding FROM speaker_clusters WHERE label IS NULL "
            "AND COALESCE(noise, 0) = 0 AND COALESCE(labeled_by, '') != 'dismissed' "
            "AND COALESCE(label_kind, '') NOT IN ('stranger', 'noise') AND embedding IS NOT NULL")]
        unlabeled = [u for u in unlabeled if len(u[3]) == dim]
    finally:
        con.close()
    old_thr = float(cfg.get("match_threshold", DEFAULTS["speakers"]["match_threshold"]))
    new_thr = float(cfg.get("match_threshold_calibrated", DEFAULTS["speakers"]["match_threshold_calibrated"]))
    cal = calibration.Calibration(samples, cohort, threshold=new_thr, raw_threshold=old_thr)
    cstr = [(c, v) for c, _k, _n, v in strangers]
    tests = [(c, k, v) for c, k, _n, v in labeled] + [(c, k, v) for c, k, _n, v in strangers]
    truths = [n for _c, _k, n, _v in labeled] + [None] * len(strangers)

    print(f"data: {data}", file=out)
    print(f"hand answers: {len(labeled)} named voices ({len(samples)} people: "
          + ", ".join(f"{n} {len(e)}" for n, (e, _i) in sorted(samples.items())) + f"), "
          f"{len(strangers)} 'random person' voices", file=out)
    print(f"cohort: {len(cohort)} voices ({sum(1 for o in cohort.owners if o is None)} unlabeled, "
          f"{sum(1 for o in cohort.owners if o == calibration.STRANGER)} strangers, "
          f"{sum(1 for o in cohort.owners if o not in (None, calibration.STRANGER))} other people's samples)",
          file=out)
    if len(tests) < SMALL_N:
        print(f"NOTE: only {len(tests)} test voices (< {SMALL_N}). Accuracies below are counts of a handful of "
              "voices: read them as a sanity check, not a measurement.", file=out)
    if not tests or not samples:
        print("nothing to evaluate", file=out)
        return {}
    modes = ("mean", "topk", "calibrated")
    sets = {"tests": tests, "auto": [(c, k, v) for c, k, _n, v in auto],
            "unlabeled": [(c, k, v) for c, k, _n, v in unlabeled]}
    rows = {m: {k: (mode_rows(m, t, samples, cohort, cstr, old_thr, new_thr) if t else [])
                for k, t in sets.items()} for m in modes}
    a_truth = [n for _c, _k, n, _v in auto]
    cal_n = sum(1 for r in rows["calibrated"]["tests"] if r[4] and all(r[4].values()))
    print(f"voices scored on the calibrated scale: {cal_n}/{len(tests)}", file=out)

    def thr_fn(r, t):
        """thresholds for row r: the configured ones (t None) or t on the tunable scale."""
        return r[3] if t is None else {n: (t if r[4][n] else r[3][n]) for n in r[0]}

    def at(m, t):
        r = rows[m]
        mt = metrics([(x[0], x[1], x[2]) for x in r["tests"]], truths, lambda i: thr_fn(r["tests"][i], t))
        keep = sum(decide(x[0], x[1], thr_fn(x, t), x[2]) == tr for x, tr in zip(r["auto"], a_truth))
        named = sum(decide(x[0], x[1], thr_fn(x, t), x[2]) is not None for x in r["unlabeled"])
        return {**mt, "auto_kept": keep, "unlabeled_named": named}

    res: dict = {}
    sweeps = {}
    for m in modes:
        r = rows[m]["tests"]
        vals = [v for x in r for n, v in x[0].items() if x[4][n]]
        lo, hi = ((0.3, 0.9) if m != "calibrated" else (min(vals), max(vals))) if vals else (0.0, 1.0)
        grid = np.round(np.linspace(lo, hi, 21), 3)
        ms = [metrics([(x[0], x[1], x[2]) for x in r], truths, lambda i, t=t: thr_fn(r[i], t)) for t in grid]
        sweeps[m] = (grid, ms)
        best = suggest(grid, ms)
        res[m] = {"configured": at(m, None), "suggested": (best[0], at(m, best[0])) if best else None}

    shipped = {"mean": old_thr, "topk": old_thr, "calibrated": new_thr}
    col = 22
    print(f"\nscoring modes side by side ({len(auto)} auto-named voices, {len(unlabeled)} unlabeled; "
          f"speakers.scoring = {cfg.get('scoring', 'mean')})", file=out)
    print("  " + "".ljust(34) + "".join(m.ljust(col) for m in modes), file=out)

    def line(label, f):
        print("  " + label.ljust(34) + "".join(f(m).ljust(col) for m in modes), file=out)

    def block(title, pick):
        print(f"  {title}", file=out)
        line("  accuracy", lambda m: (lambda d: f"{d['correct']}/{d['pos']} = {_pct(d['accuracy'])}")(pick(m)))
        line("  false names", lambda m: (lambda d: f"{d['wrong']}/{d['n']}")(pick(m)))
        line("  other-person trials accepted", lambda m: (lambda d: f"{d['imp_accepted']}/{d['imp']}")(pick(m)))
        line("  auto-named voices kept", lambda m: f"{pick(m)['auto_kept']}/{len(auto)}")
        line("  unlabeled voices named", lambda m: f"{pick(m)['unlabeled_named']}/{len(unlabeled)}")

    line("shipped threshold", lambda m: f"{shipped[m]:g}")
    block("at the shipped threshold", lambda m: res[m]["configured"])
    line("suggested threshold", lambda m: f"{res[m]['suggested'][0]:.3f}" if res[m]["suggested"] else "none")
    if all(res[m]["suggested"] for m in modes):
        block("at its suggested threshold", lambda m: res[m]["suggested"][1])
    print("  (unlabeled voices have unknown identity: a count, not an accuracy. Suggested = highest "
          "accuracy with false names and accepted other-person trials <= 5%, middle of the tied range.)", file=out)
    print(f"  Default scoring stays \"mean\" until the eval has at least {SMALL_N} labels including some "
          "stranger answers.", file=out)

    for m in modes:
        grid, ms = sweeps[m]
        print(f"\n{m} threshold sweep:", file=out)
        for t, d in zip(grid, ms):
            print(f"  {t:7.3f}: accuracy {d['correct']}/{d['pos']} {_pct(d['accuracy'])}, false names "
                  f"{d['wrong']}/{d['n']}, other-person trials accepted {d['imp_accepted']}/{d['imp']}", file=out)
    return res


# ---------------------------------------------------------------------------
# line mode: who said each line, before and after turn rescoring

LINE_RE = re.compile(r"^\[\d{1,2}:\d{2}\] (?:⭐ )?(?:\*\*([^*\n]{1,40}):\*\* )?(.*)$")


def _tokens(text: str) -> list[str]:
    return [t for t in (re.sub(r"[^a-z0-9']", "", w.lower()) for w in text.split()) if t]


def truth_lines(con: sqlite3.Connection, notes: Path, device: str, start_ms: int, day: str, heading: str,
                auto: bool = False):
    """[(name, [tokens])] per stamped paragraph of this conversation's Raw section, name
    None where the wearer didn't say who it is. A paragraph is hand-labelled when he
    reassigned that line, or its tag belongs to a voice he named himself (`labeled_by =
    'user'`; "Someone" for a stranger, as is a line whose name was removed; not a mixed voice, nor a tag two voices share);
    with no tags at all (one voice, never shown) every line is that voice's when he named
    it. auto: voices named by voiceprint (`labeled_by = 'match'`) count too. None when the
    section can't be read."""
    from .process import section_bounds
    daily = Path(notes) / f"{day}.md"
    if not heading or not daily.exists():
        return None
    lines = daily.read_text().split("\n")
    b = section_bounds(lines, heading)
    if not b:
        return None
    cols = {r[1] for r in con.execute("PRAGMA table_info(speaker_clusters)")}
    mixed = "COALESCE(mixed, 0)" if "mixed" in cols else "0"
    wn = "COALESCE(wearer_named, 0)" if "wearer_named" in cols else "0"
    voices = con.execute(f"SELECT tag, label, labeled_by, label_kind, {mixed}, {wn} FROM speaker_clusters "
                         "WHERE device = ? AND start_ms = ?", (device, start_ms)).fetchall()
    count: dict[str, int] = {}
    for v in voices:
        count[v[0]] = count.get(v[0], 0) + 1
    hand = {}
    for tag, label, by, kind, mix, by_wearer in voices:
        if (by == "user" or (auto and by == "match" and not by_wearer)) and not mix and count.get(tag) == 1:
            if kind == "stranger":
                hand[tag] = "Someone"
            elif label:
                hand[tag] = label
    try:
        over = dict(con.execute("SELECT para_index, new_tag FROM transcript_relabels WHERE device = ? AND "
                                "start_ms = ? AND para_index IS NOT NULL ORDER BY id", (device, start_ms)).fetchall())
    except sqlite3.OperationalError:
        over = {}
    paras = [LINE_RE.match(line) for line in lines[b[0]:b[1]]]
    paras = [m for m in paras if m]
    untagged = not any(m[1] for m in paras)
    only = (next(iter(hand.values())) if untagged and len(voices) == 1 and hand else None)
    out = []
    for n, m in enumerate(paras):
        name = over.get(n) or (hand.get(m[1]) if m[1] else only)
        if n in over and GENERIC_TAG.match(name or ""):
            name = "Someone"          # a line whose name was removed: a stranger
        out.append((name, _tokens(m[2])))
    return out


def align(paras: list[tuple], words: list[tuple]) -> list[int | None]:
    """The paragraph index of each transcribed word, by matching the words' text against
    the paragraphs' (None for words the Raw text doesn't have: a later transcription can differ)."""
    from difflib import SequenceMatcher
    ptoks, pidx = [], []
    for i, (_n, toks) in enumerate(paras):
        ptoks += toks
        pidx += [i] * len(toks)
    wtoks = [(_tokens(w[2]) or [""])[0] for w in words]
    out: list[int | None] = [None] * len(words)
    for a, b, size in SequenceMatcher(None, ptoks, wtoks, autojunk=False).get_matching_blocks():
        for k in range(size):
            out[b + k] = pidx[a + k]
    return out


MIN_ALIGNED = 3          # a line needs at least this many of its words reproduced...
MIN_ALIGNED_SHARE = 0.5  # ...and at least this share of them


def evaluable(aligned: int, words: int) -> bool:
    """Whether a line whose `words` tokens had `aligned` matched in the fresh transcription is
    scored (else it is left out of both before and after)."""
    return aligned >= MIN_ALIGNED and aligned >= MIN_ALIGNED_SHARE * words


def word_labels(words: list[tuple], turns) -> list[str | None]:
    """The label of the turn each word's midpoint falls in."""
    import bisect
    starts = [t.start for t in turns]
    out = []
    for s, e, _w in words:
        m = (s + e) / 2
        i = max(0, bisect.bisect_right(starts, m) - 1)
        out.append(turns[i].label if turns else None)
    return out


def _segments(clip: Path, cache: Path | None):
    """Whisper segments with word timings for a clip, from `cache/<clip stem>.json` when
    there (written after transcribing when cache is given)."""
    import json
    from .transcribe import Seg
    f = cache / f"{clip.stem}.json" if cache else None
    if f and f.exists():
        d = json.loads(f.read_text())
        return [Seg(s, e, t, [tuple(w) for w in ws]) for s, e, t, ws in d["segs"]]
    from . import transcribe
    segs, _info = transcribe.transcribe(clip)
    if f:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps({"low_conf_share": segs.low_conf_share,
                                 "segs": [[s[0], s[1], s[2], [list(w) for w in s.words]] for s in segs]}))
    return segs


def _embedder_for(embedder, data: Path, cfg: dict):
    """The embedder a replay uses: the given one (tests) or ECAPA, through the denoiser when
    `speakers.denoise` is on (as process.find_speakers does)."""
    embedder = embedder or speakers.get_embedder(data)
    return speakers.DenoisedEmbedder(embedder) if cfg.get("denoise") is True else embedder


class _Cached:
    """An embedder remembering this clip's embeddings in `cache`: windows by their spans, joined
    pieces (speakers.embed_pieces) by theirs; `tag` keeps models and denoised audio apart."""

    def __init__(self, inner, clip: Path, cache: Path, tag: str = ""):
        self.inner, self.clip, self.cache, self.tag = inner, clip, cache, tag

    def _file(self, kind: str, spans) -> Path:
        import hashlib
        h = hashlib.sha256(repr(spans).encode()).hexdigest()[:16]
        return self.cache / f"{self.clip.stem}.{kind}{h}{self.tag}.npy"

    def _get(self, f: Path, make):
        if f.exists():
            return np.load(f)
        e = np.asarray(make(), dtype=np.float32)
        f.parent.mkdir(parents=True, exist_ok=True)
        np.save(f, e)
        return e

    def __call__(self, c, spans):
        key = [(round(a, 3), round(b, 3)) for a, b in spans]
        return self._get(self._file("", key), lambda: self.inner(c, spans))

    def embed_pieces(self, c, pieces, audio=None):
        key = [[(round(a, 3), round(b, 3)) for a, b in p] for p in pieces]
        return self._get(self._file("p", key), lambda: speakers.embed_pieces(self.inner, c, pieces, audio))


def _cached_embedder(embedder, clip: Path, cache: Path | None, cfg: dict | None = None, tag: str = ""):
    """The embedder, remembering this clip's embeddings in `cache` (same windows, same audio;
    denoised audio and other models are cached apart)."""
    if cache is None:
        return embedder
    return _Cached(embedder, clip, cache, (".dn" if (cfg or {}).get("denoise") is True else "") + tag)


def leave_out(vdir: Path, drop: set[int]):
    """(voiceprints {name: (mean, count)}, samples {name: (embs, ids)}) without the samples
    keyed by `drop` (this conversation's voices and lines)."""
    prints, samples = {}, {}
    for name in speakers._names(vdir):
        vp = speakers._read(vdir, name)
        if vp is None:
            continue
        keep = [i for i, c in enumerate(vp.ids) if c not in drop]
        vp = speakers.Voiceprint(vp.legacy_sum, vp.legacy, [vp.ids[i] for i in keep], vp.embs[keep])
        if vp.count <= 0:
            continue
        prints[name] = (vp.mean(), vp.count)
        rows, ids = ([vp.legacy_sum / vp.legacy], [None]) if vp.legacy > 0 and float(np.abs(vp.legacy_sum).sum()) else ([], [])
        rows += list(vp.embs)
        ids += list(vp.ids)
        if rows:
            samples[name] = (_unit(np.stack(rows)), ids)
    return prints, samples


def _replay(con, data: Path, notes: Path, rows, cfg: dict, cache: Path | None, embedder, out,
            model_for=None, hand_only: bool = True, require_truth: bool = True):
    """Replay the speaker step on each conversation of `rows` [(device, start_ms, day, heading,
    clip)] that has labelled lines, leave-one-out: this conversation's samples out of the
    voiceprints and strangers, its voices out of the cohort. Yields {key, day, heading, truth,
    hand, words, diar, segs, prints, clip}: truth is truth_lines (with voices named automatically
    too unless hand_only), hand which of those lines the wearer labelled himself. `model_for(key)`
    gives the wearer model for that conversation (None: no model). require_truth False: every
    conversation, truth None when it has no labelled lines."""
    vdir = speakers.voiceprint_dir(data)
    from . import process
    short = short_voices(con, cfg)   # stored voiceprints may still hold them: the eval drops them, as the rebuild does
    from . import naming
    space = naming.space(cfg)
    mode = cfg.get("voiceprints")
    rebuilt = space != naming.DEFAULT or mode == "rebuilt" or mode in WHOLE_PRINTS
    pool = []
    if rebuilt:   # voiceprints in this space, rebuilt from the hand-named voices' audio as a rebuild would
        labeled, _s, _a = load_voices(con)
        E = space_embeddings(con, data, [c for c, *_r in labeled], space, cache, embedder, cfg.get("_models"))
        W, covered = whole_embeddings(con, data, space, cache, embedder, cfg.get("_models")) \
            if mode in WHOLE_PRINTS else ({}, set())
        pool = print_pool(labeled, E, mode, W, covered, short)
        print(f"  voiceprints rebuilt in {naming.label(space)} from {len(pool)} hand-named voices"
              + (f" ({sum(c in W for c, *_r in pool)} kept whole voices)" if mode in WHOLE_PRINTS else ""), file=out)
    for device, start_ms, d, heading, clip in rows:
        hand = truth_lines(con, notes, device, start_ms, d, heading)
        truth = hand if hand_only else truth_lines(con, notes, device, start_ms, d, heading, auto=True)
        if not truth or not any(n for n, _t in truth):
            if require_truth:
                continue
            hand = truth = None
        path = Path(data) / "clips" / Path(clip or "").name
        if not path.exists():
            path = Path(clip or "")
        if not path.exists():
            print(f"  {d} {heading[:5]}: clip missing; skipped", file=out)
            continue
        key = (device, start_ms)
        cids = {r[0] for r in con.execute("SELECT id FROM speaker_clusters WHERE device = ? AND start_ms = ?", key)}
        try:
            keys = {r[0] for r in con.execute("SELECT key FROM paragraph_samples WHERE id LIKE ?",
                                              (f"para:{device}:{start_ms}:%",))}
        except sqlite3.OperationalError:
            keys = set()
        prints, samples = leave_out(vdir, cids | keys | short)
        namer = None
        if rebuilt:
            prints, samples = loco_prints(pool, key), {}
            namer = naming.namer(cfg, data, embedder, cfg.get("_models"),
                                 wrap=lambda e, tag, p=path: _cached_embedder(e, p, cache, cfg, "." + tag))
        cal = None
        mode = cfg.get("scoring", "mean")
        if mode in ("topk", "calibrated") and samples:
            dim = len(next(iter(samples.values()))[0][0])
            cal = calibration.Calibration(
                samples, calibration.build_cohort(con, samples) if mode == "calibrated" else calibration.Cohort.empty(dim),
                mode=mode, raw_threshold=float(cfg.get("match_threshold", 0.5)),
                threshold=float(cfg.get("match_threshold_calibrated", DEFAULTS["speakers"]["match_threshold_calibrated"])))
        try:
            srows = con.execute("SELECT cluster_id, embedding FROM strangers").fetchall()
        except sqlite3.OperationalError:
            srows = []
        strangers = [np.frombuffer(e, dtype=np.float32) for c, e in srows if e and c not in cids | keys]
        strangers = np.stack(strangers) if strangers and len({len(v) for v in strangers}) == 1 else None
        if rebuilt:
            strangers = None     # stored in the ECAPA window space
        segs = _segments(path, cache)
        ws = process.wearer_settings({**cfg, "wearer_model": None}, device)
        ws["wearer_model"] = model_for(key) if model_for else None
        scorer = speakers.TurnScorer(prints, threshold=float(cfg.get("match_threshold", 0.5)), calibration=cal,
                                     strangers=strangers, exclude=key,
                                     margin=float(cfg.get("turn_margin", DEFAULTS["speakers"]["turn_margin"])),
                                     margin_calibrated=float(cfg.get("turn_margin_calibrated",
                                                                     DEFAULTS["speakers"]["turn_margin_calibrated"])),
                                     person_floor=float(cfg.get("turn_person_floor",
                                                                DEFAULTS["speakers"]["turn_person_floor"])),
                                     split_cosine=speakers.naming_settings(cfg)["split_cosine"], **ws)
        diar = speakers.diarize_words(path, segs, _cached_embedder(embedder, path, cache, cfg), prints,
                                      cluster_threshold=float(cfg.get("cluster_threshold", 0.25)),
                                      match_threshold=float(cfg.get("match_threshold", 0.5)), strangers=strangers,
                                      match_margin=float(cfg.get("match_margin", DEFAULTS["speakers"]["match_margin"])),
                                      calibration=cal, exclude=key, rescorer=scorer, keep_before=True,
                                      boundaries=process.boundary_settings(cfg), namer=namer,
                                      **speakers.naming_settings(cfg))
        words = sorted((w for s in segs for w in (getattr(s, "words", None) or ())), key=lambda w: w[0])
        yield {"key": key, "day": d, "heading": heading, "truth": truth, "words": words, "diar": diar,
               "segs": segs, "prints": prints, "clip": path,
               "hand": [bool(h[0]) for h in hand] if hand else ([False] * len(truth) if truth else None)}


def _line_counts(truth, words, before_turns, after_turns) -> dict:
    """Line accuracy of two labellings of the same words against the labelled lines."""
    where = align(truth, words)
    lab_b, lab_a = word_labels(words, before_turns or []), word_labels(words, after_turns or [])
    aligned: dict[int, int] = {}
    for p in where:
        if p is not None:
            aligned[p] = aligned.get(p, 0) + 1
    # a line counts only when the fresh transcription reproduces enough of it
    usable = {p for p, n in aligned.items() if truth[p][0] is not None and evaluable(n, len(truth[p][1]))}
    dropped = sum(1 for p, (name, _t) in enumerate(truth) if name is not None and p not in usable)
    per: dict[int, dict] = {}
    for i, p in enumerate(where):
        if p not in usable:
            continue
        dur = words[i][1] - words[i][0]
        e = per.setdefault(p, {"b": {}, "a": {}})
        e["b"][lab_b[i]] = e["b"].get(lab_b[i], 0.0) + dur
        e["a"][lab_a[i]] = e["a"].get(lab_a[i], 0.0) + dur
    c = {"lines": 0, "before": 0, "after": 0, "speech": 0.0, "before_s": 0.0, "after_s": 0.0,
         "fixed": 0, "broken": 0, "excluded": dropped,
         "named_b": 0, "named_a": 0, "wrong_b": 0, "wrong_a": 0}
    is_name = lambda x: bool(x) and not re.match(r"Speaker [A-Z]+$", x)  # noqa: E731
    for p, e in per.items():
        name = truth[p][0].lower()
        b = max(e["b"], key=e["b"].get)
        a = max(e["a"], key=e["a"].get)
        ok_b, ok_a = (b or "").lower() == name, (a or "").lower() == name
        c["lines"] += 1
        c["named_b"] += is_name(b)
        c["named_a"] += is_name(a)
        c["wrong_b"] += is_name(b) and not ok_b
        c["wrong_a"] += is_name(a) and not ok_a
        c["before"] += ok_b
        c["after"] += ok_a
        c["fixed"] += ok_a and not ok_b
        c["broken"] += ok_b and not ok_a
        c["speech"] += sum(e["b"].values())
        c["before_s"] += sum(v for k, v in e["b"].items() if (k or "").lower() == name)
        c["after_s"] += sum(v for k, v in e["a"].items() if (k or "").lower() == name)
    return c


def _conversation_rows(con, day):
    q = "SELECT device, start_ms, day, raw_heading, clip FROM conversations"
    return con.execute(q + (" WHERE day = ?" if day else "") + " ORDER BY start_ms", (day,) if day else ()).fetchall()


def line_eval(data: Path, notes: Path, day: str | None = None, cache: Path | None = None, out=None,
              overrides: dict | None = None, embedder=None) -> dict:
    """Line mode: replay the speaker step on every conversation with hand-labelled lines
    (truth_lines), leave-one-out (this conversation's samples out of the voiceprints and
    strangers, its voices out of the cohort), and score each hand-labelled line with the
    cluster-level labels (before) and after turn rescoring. A line's label is the one
    covering most of its words' speech. `overrides` replace `speakers` settings (scoring,
    turn_margin, ...). Prints counts and tags only."""
    out = out or sys.stdout
    cfg = {**load_config(data)["speakers"], **(overrides or {})}
    db = Path(data) / "pages.db"
    if not db.exists():
        print(f"no pages.db in {data}", file=out)
        return {}
    embedder = _embedder_for(embedder, data, cfg)
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    total = {"excluded": 0, "lines": 0, "before": 0, "after": 0, "speech": 0.0, "before_s": 0.0, "after_s": 0.0,
             "fixed": 0, "broken": 0, "named_b": 0, "named_a": 0, "wrong_b": 0, "wrong_a": 0, "conversations": 0, "scored": 0, "moved": 0, "cpu_s": 0.0}
    print(f"line mode: {data} (scoring {cfg.get('scoring', 'mean')}, margin {cfg.get('turn_margin')} / "
          f"calibrated {cfg.get('turn_margin_calibrated')})", file=out)
    model = None
    if cfg.get("wearer_model"):
        from . import wearer
        model = wearer.Model.from_config(cfg["wearer_model"])
    try:
        for r_ in _replay(con, data, notes, _conversation_rows(con, day), cfg, cache, embedder, out,
                          model_for=(lambda _k: model) if model else None):
            diar, d, heading = r_["diar"], r_["day"], r_["heading"]
            c = _line_counts(r_["truth"], r_["words"], diar.before, diar.turns)
            r = diar.rescore or {}
            moves = ", ".join(f"{k} x{n}" for k, n in (r.get("moves") or {}).items())
            print(f"  {d} {heading[:5]}: {c['lines']} hand-labelled lines ({c['excluded']} excluded); before {c['before']}/{c['lines']}, "
                  f"after {c['after']}/{c['lines']} (speech {_pct(c['before_s'] / c['speech'] if c['speech'] else float('nan'))}"
                  f" -> {_pct(c['after_s'] / c['speech'] if c['speech'] else float('nan'))}); "
                  + (f"rescoring failed: {r['error']}" if "error" in r else
                     f"turns scored {r.get('scored', 0)}, moved {r.get('moved', 0)}"
                     + (f" [{moves}]" if moves else "") + f", inherited {r.get('inherited', 0)}"
                     + (f", mixed: {', '.join(r['mixed'])}" if r.get("mixed") else "")), file=out)
            for k in c:
                total[k] += c[k]
            total["conversations"] += 1
            total["scored"] += r.get("scored", 0)
            total["moved"] += r.get("moved", 0)
            total["cpu_s"] += r.get("cpu_s", 0.0)
    finally:
        con.close()
    if not total["lines"]:
        print("  no hand-labelled lines (the wearer names a voice or reassigns a line in the app to make some)", file=out)
        return total
    print(f"  all: {total['conversations']} conversations, {total['lines']} lines ({total['excluded']} hand-labelled "
          f"lines excluded: under half, or under {MIN_ALIGNED}, of their words reproduced); line accuracy before "
          f"{total['before']}/{total['lines']} = {_pct(total['before'] / total['lines'])}, after {total['after']}/"
          f"{total['lines']} = {_pct(total['after'] / total['lines'])} ({total['fixed']} fixed, {total['broken']} "
          f"broken); by speech {_pct(total['before_s'] / total['speech'])} -> {_pct(total['after_s'] / total['speech'])}; "
          f"turns scored {total['scored']}, moved {total['moved']}, rescoring {total['cpu_s']:.2f}s cpu", file=out)
    n = total["lines"]
    print(f"  naming (lines given a person's name, before -> after rescoring): named {_pct(total['named_b'] / n)} -> "
          f"{_pct(total['named_a'] / n)}, false names (named, wrong) {total['wrong_b']}/{n} = {_pct(total['wrong_b'] / n)} -> "
          f"{total['wrong_a']}/{n} = {_pct(total['wrong_a'] / n)}", file=out)
    return total


# ---------------------------------------------------------------------------
# --fit-wearer: the near-field wearer model (wearer.py)

TURN_LABELLED = 0.5     # a turn is labelled when this share of its speech is in labelled lines...
TURN_AGREE = 0.8        # ...and one name holds this share of that
MIN_CLASS = 10          # turns of each class needed to fit


def turn_labels(truth, hand, words, turns) -> list[tuple | None]:
    """(name, source "hand"/"auto") per turn [{start, end}], from the labelled lines its
    words fall in; None when too little of it is labelled or the lines disagree."""
    where = align(truth, words)
    out = []
    for t in turns:
        tot, by, src = 0.0, {}, {"hand": 0.0, "auto": 0.0}
        for i, w in enumerate(words):
            m = (w[0] + w[1]) / 2
            if not t["start"] <= m <= t["end"]:
                continue
            d = w[1] - w[0]
            tot += d
            p = where[i]
            if p is not None and truth[p][0]:
                by[truth[p][0].lower()] = by.get(truth[p][0].lower(), 0.0) + d
                src["hand" if hand[p] else "auto"] += d
        lab = sum(by.values())
        if not by or tot <= 0 or lab < TURN_LABELLED * tot:
            out.append(None)
            continue
        name = max(by, key=by.get)
        out.append((name, max(src, key=src.get)) if by[name] >= TURN_AGREE * lab else None)
    return out


def _turn_metrics(rows, probs, wearer_p: float) -> dict:
    y = np.array([r[3] for r in rows], dtype=bool)
    p = np.asarray(probs)
    pred = p >= 0.5
    hand = np.array([r[5] == "hand" for r in rows], dtype=bool)
    hi, lo = p >= wearer_p, p <= 1 - wearer_p
    return {"n": len(rows), "acc": float((pred == y).mean()) if len(rows) else float("nan"),
            "wearer_acc": float(pred[y].mean()) if y.any() else float("nan"),
            "other_acc": float((~pred[~y]).mean()) if (~y).any() else float("nan"),
            "hand_n": int(hand.sum()), "hand_acc": float((pred[hand] == y[hand]).mean()) if hand.any() else float("nan"),
            "hi": int(hi.sum()), "hi_wearer": int((hi & y).sum()), "lo": int(lo.sum()), "lo_other": int((lo & ~y).sum())}


def fit_wearer(data: Path, notes: Path, day: str | None = None, cache: Path | None = None, out=None,
               overrides: dict | None = None, embedder=None, save: bool = False, hand_only: bool = True) -> dict:
    """Fit the wearer model from labelled turns and report it. Replays the speaker step
    (leave-one-out, as line mode) on every conversation with labelled lines: the wearer's hand
    labels (Voice Scoring answers, named voices, reassigned lines) plus, when not hand_only, voices
    named by voiceprint (never by the wearer signal). Each rescored turn whose speech
    lies mostly in lines of one name is a training turn (the wearer or not). Held-out accuracy
    is leave-one-conversation-out, next to score-only and acoustic-only models; then each
    conversation is replayed with the model fitted without it (and with the full model) to
    count line accuracy and the voices whose name changes. save: write the full model to
    `speakers.wearer_model` in config.json."""
    from . import config as cfgmod
    from . import wearer
    out = out or sys.stdout
    cfg = {**load_config(data)["speakers"], **(overrides or {})}
    db = Path(data) / "pages.db"
    if not db.exists():
        print(f"no pages.db in {data}", file=out)
        return {}
    embedder = _embedder_for(embedder, data, cfg)
    wname = wearer_name(cfg).lower()
    wp = float(cfg.get("wearer_p", DEFAULTS["speakers"]["wearer_p"]))
    mode = cfg.get("scoring", "mean")
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    res: dict = {}
    try:
        convs = _conversation_rows(con, day)
        base, rows, all_feats = {}, [], {}
        cpu = {"features": 0.0, "rescoring": 0.0}
        for r_ in _replay(con, data, notes, convs, cfg, cache, embedder, out, hand_only=hand_only):
            diar, key = r_["diar"], r_["key"]
            base[key] = r_
            info = diar.rescore or {}
            cpu["features"] += info.get("features_cpu_s", 0.0)
            cpu["rescoring"] += info.get("cpu_s", 0.0)
            turns = info.get("turns") or []
            for t in turns:
                all_feats.setdefault(key[0], []).append((t["level_db"], t["decay_db"], t["flatness"]))
            for t, lab in zip(turns, turn_labels(r_["truth"], r_["hand"], r_["words"], turns)):
                if lab and t["score"] is not None:
                    rows.append((key[0], (t["level_db"], t["decay_db"], t["flatness"]), t["score"],
                                 int(lab[0] == wname), key, lab[1]))
        dstats = {d: wearer.stats(v) for d, v in all_feats.items()}
        n_w = sum(r[3] for r in rows)
        n_o = len(rows) - n_w
        print(f"wearer fit: {data} (scoring {mode}, wearer {wearer_name(cfg)}, wearer_p {wp:g}, "
              f"labels {'hand only' if hand_only else 'hand + voiceprint-named voices'})", file=out)
        print(f"  {len(base)} conversations replayed; labelled turns: {len(rows)} ({n_w} wearer, {n_o} other; "
              f"{sum(r[5] == 'hand' for r in rows)} from hand labels)", file=out)
        for d, st in sorted(dstats.items()):
            lab = [r for r in rows if r[0] == d]
            print(f"  pendant {d}: {st['n']} turns with features ({len(lab)} labelled, {sum(r[3] for r in lab)} "
                  f"wearer); mean level {st['mean'][0]:.1f} dB, decay {st['mean'][1]:.1f} dB, "
                  f"flatness {st['mean'][2]:.2f}", file=out)
        for nm, j in (("level_db", 0), ("decay_db", 1), ("flatness", 2)):
            a = [r[1][j] for r in rows if r[3]]
            b = [r[1][j] for r in rows if not r[3]]
            if a and b:
                print(f"  {nm}: wearer median {np.median(a):.2f}, other median {np.median(b):.2f}", file=out)
        if n_w < MIN_CLASS or n_o < MIN_CLASS:
            print(f"  not enough labelled turns to fit (need {MIN_CLASS} of each)", file=out)
            return {"rows": len(rows), "wearer": n_w, "other": n_o}
        variants = {"full": None, "score only": [3], "acoustic only": [0, 1, 2]}
        keys = sorted({r[4] for r in rows})
        loco: dict = {}
        print(f"  held-out (leave one conversation out, {len(keys)} conversations with labelled turns):", file=out)
        for vn, cols in variants.items():
            probs = np.full(len(rows), np.nan)
            for k in keys:
                train = [r for r in rows if r[4] != k]
                if sum(r[3] for r in train) < 2 or sum(1 - r[3] for r in train) < 2:
                    continue
                m = wearer.fit([r[:4] for r in train], mode, dstats, cols=cols)
                for i, r in enumerate(rows):
                    if r[4] == k:
                        probs[i] = wearer.sigmoid(m.logit(r[1], r[2], m.norm(r[0])))
                if vn == "full":
                    loco[k] = m
            ok = ~np.isnan(probs)
            mt = _turn_metrics([r for r, o in zip(rows, ok) if o], probs[ok], wp)
            res[vn] = mt
            print(f"    {vn:14s} hand-labelled accuracy {_pct(mt['hand_acc'])} of {mt['hand_n']} turns; all labelled "
                  f"{_pct(mt['acc'])} of {mt['n']} (wearer {_pct(mt['wearer_acc'])}, other {_pct(mt['other_acc'])}); "
                  f"p >= {wp:g}: {mt['hi_wearer']}/{mt['hi']} wearer; p <= {1 - wp:.2g}: {mt['lo_other']}/{mt['lo']} other",
                  file=out)
        full = wearer.fit([r[:4] for r in rows], mode, dstats)
        full.info = {"turns": len(rows), "wearer": n_w, "other": n_o, "conversations": len(keys),
                     "heldout_accuracy_hand": round(res["full"]["hand_acc"], 4), "hand_only": hand_only}
        print("  coefficients (per-pendant z-scores; score standardized): " + ", ".join(
            f"{n} {c:+.2f}" for n, c in zip(wearer.FEATURES, full.coef)) + f", bias {full.bias:+.2f}", file=out)

        # what the model would change: replay each conversation with the model fitted without it
        # (honest), and with the full model
        def compare(model_for, label):
            tot = {"lines": 0, "before": 0, "after": 0, "speech": 0.0, "before_s": 0.0, "after_s": 0.0,
                   "fixed": 0, "broken": 0, "excluded": 0}
            voices = changed = named = unnamed = 0
            for r_ in _replay(con, data, notes, [c for c in convs if (c[0], c[1]) in base], cfg, cache, embedder,
                              out, model_for=model_for, hand_only=hand_only):
                b = base[r_["key"]]
                c = _line_counts(b["truth"], b["words"], b["diar"].turns, r_["diar"].turns)
                for k_ in tot:
                    tot[k_] += c[k_]
                old = {c_.index: c_.name for c_ in b["diar"].clusters}
                for c_ in r_["diar"].clusters:
                    voices += 1
                    if (old.get(c_.index) or "") != (c_.name or ""):
                        changed += 1
                        named += (c_.name or "").lower() == wname
                        unnamed += (old.get(c_.index) or "").lower() == wname
            n = tot["lines"] or 1
            print(f"  {label}: voices {changed}/{voices} change name ({named} named {wearer_name(cfg)}, "
                  f"{unnamed} lose that name); labelled lines {tot['before']}/{tot['lines']} = "
                  f"{_pct(tot['before'] / n)} -> {tot['after']}/{tot['lines']} = {_pct(tot['after'] / n)} "
                  f"({tot['fixed']} fixed, {tot['broken']} broken); by speech "
                  f"{_pct(tot['before_s'] / tot['speech'] if tot['speech'] else float('nan'))} -> "
                  f"{_pct(tot['after_s'] / tot['speech'] if tot['speech'] else float('nan'))}", file=out)
            return {"voices": voices, "changed": changed, "named": named, "unnamed": unnamed, **tot}

        before = sum(_line_counts(b["truth"], b["words"], b["diar"].before, b["diar"].before)["after"]
                     for b in base.values())
        print(f"  lines right with cluster-level labels: {before}; then rescoring without a wearer model (the "
              f"'before' below)", file=out)
        res["loco_apply"] = compare(lambda k: loco.get(k), "held-out model")
        res["full_apply"] = compare(lambda k: full, "full model")
        print(f"  cpu: near-field features {cpu['features']:.2f}s of {cpu['rescoring']:.2f}s rescoring (embedding "
              f"excluded: see --cache)", file=out)
        res["cpu"] = cpu
        res["model"] = full.to_config()
        import json
        print("  speakers.wearer_model = " + json.dumps(full.to_config()), file=out)
    finally:
        con.close()
    if save:
        path = Path(data) / "config.json"
        raw = cfgmod._read_raw(path)
        sp = raw.get("speakers") if isinstance(raw.get("speakers"), dict) else {}
        sp["wearer_model"] = res["model"]
        raw["speakers"] = sp
        cfgmod._write_raw(path, raw)
        print(f"  saved to {path}", file=out)
    else:
        print("  not saved (--save writes it to config.json)", file=out)
    return res


# ---------------------------------------------------------------------------
# --turns: where turns start and end (blips, long paragraphs), per conversation

BLIP_WORDS = 3          # a blip: a paragraph of at most this many words between two of one other voice
LONG_WORDS = 150        # a paragraph this long in a conversation with 2+ voices usually hides a change
TAG_RE = re.compile(r"^\*\*([^*\n]{1,40}):\*\* ")


def paragraph_stats(paras) -> dict:
    """paras [(label or None, words)]: lines, voices (distinct labels), blips (A–B–A: a paragraph
    of at most BLIP_WORDS words between two paragraphs of one other label), long (paragraphs of
    LONG_WORDS+ words) and long_multi (those, when the conversation has 2+ voices)."""
    labs = [lab for lab, _n in paras]
    voices = len({lab for lab in labs if lab})
    blips = sum(1 for i in range(1, len(paras) - 1)
                if labs[i - 1] and labs[i - 1] == labs[i + 1] and labs[i] != labs[i - 1] and paras[i][1] <= BLIP_WORDS)
    long_ = sum(1 for _l, n in paras if n >= LONG_WORDS)
    return {"lines": len(paras), "voices": voices, "blips": blips, "long": long_,
            "long_multi": long_ if voices >= 2 else 0}


def _split_tag(text: str) -> tuple[str | None, int]:
    m = TAG_RE.match(text)
    return (m[1], len(text[m.end():].split())) if m else (None, len(text.split()))


def written_paragraphs(notes: Path, day: str, heading: str) -> list | None:
    """[(label, words)] per stamped paragraph of a conversation's Raw section as written."""
    from .process import section_bounds
    daily = Path(notes) / f"{day}.md"
    if not heading or not daily.exists():
        return None
    lines = daily.read_text().split("\n")
    b = section_bounds(lines, heading)
    if not b:
        return None
    return [(m[1], len(m[2].split())) for m in (LINE_RE.match(x) for x in lines[b[0]:b[1]]) if m]


def clock(con, device: str, start_ms: int, end_ms: int):
    """(breakpoints, pauses) as process.write_clip made them, rebuilt from the chunk clock."""
    from . import enroll, process
    rows = con.execute("SELECT ts_ms FROM chunks WHERE device = ? AND ts_ms BETWEEN ? AND ? "
                       + enroll.WINDOW_SQL + " ORDER BY ts_ms, page, ord", (device, start_ms, end_ms)).fetchall()
    bp, pauses, prev = [], [], None
    for i, (ts,) in enumerate(rows):
        if not bp or ts != bp[-1][1]:
            if bp and ts - prev >= process.PAUSE_S * 1000:
                pauses.append(i * process.FRAME_S)
            bp.append((i * process.FRAME_S, ts))
        prev = ts
    return bp or [(0.0, start_ms)], pauses


def replay_paragraphs(diar, segs, breakpoints, pauses) -> list:
    """[(label, words)] of the paragraphs the processor would write for this diarization."""
    from . import process
    shown = diar if diar and diar.show else None
    paras, _spans = process.paragraphs_with_spans(segs, breakpoints, pauses, shown.labels if shown else None,
                                                  shown.turns if shown else None)
    return [_split_tag(text) for _t, text in paras]


def voice_report(diar, words, prints) -> list[dict]:
    """Per voice of a replay: tag, speech seconds (its words in the final turns), and the closest
    voiceprints by raw cosine of its centroid. Counts and names only."""
    lab = word_labels(words, diar.turns or [])
    speech: dict = {}
    for w, t in zip(words, lab):
        speech[t] = speech.get(t, 0.0) + w[1] - w[0]
    names = list(prints)
    vp = _unit(np.stack([prints[n][0] for n in names])) if names else None
    out = []
    for c in diar.clusters:
        sims = sorted(((float(_unit(c.embedding) @ vp[j]), n) for j, n in enumerate(names)), reverse=True) if names else []
        out.append({"tag": c.tag, "speech_s": round(speech.get(c.tag, 0.0), 1), "mixed": bool(c.mixed),
                    "closest": [(n, round(s, 2)) for s, n in sims[:2]]})
    return sorted(out, key=lambda v: -v["speech_s"])


TURN_KEYS = ("lines", "voices", "blips", "long", "long_multi")


def _turn_pass(con, data, notes, rows, cfg, cache, embedder, out) -> dict:
    """{key: {...}} for one configuration: paragraph stats, line accuracy, voices, passes."""
    res = {}
    for r_ in _replay(con, data, notes, rows, cfg, cache, embedder, out, require_truth=False):
        device, start_ms = r_["key"]
        end_ms = con.execute("SELECT end_ms FROM conversations WHERE device = ? AND start_ms = ?",
                             r_["key"]).fetchone()[0] or start_ms
        bp, pauses = clock(con, device, start_ms, end_ms)
        diar = r_["diar"]
        e = {"stats": paragraph_stats(replay_paragraphs(diar, r_["segs"], bp, pauses)),
             "voices": voice_report(diar, r_["words"], r_["prints"]),
             "boundaries": diar.boundaries or {}, "rescore": diar.rescore or {}, "heading": r_["heading"],
             "day": r_["day"], "lines": None}
        if r_["truth"]:
            e["lines"] = _line_counts(r_["truth"], r_["words"], diar.before, diar.turns)
        res[r_["key"]] = e
    return res


def turn_eval(data: Path, notes: Path, day: str | None = None, cache: Path | None = None, out=None,
              overrides: dict | None = None, embedder=None, only: list | None = None,
              voices: bool = False) -> dict:
    """Turn mode: per conversation of `day` (all of them when None), the paragraphs as written in
    the Raw note and as a replay of the speaker step writes them (leave-one-out, as line mode):
    lines, distinct voices, A–B–A blips of at most BLIP_WORDS words, paragraphs of LONG_WORDS+ words
    (all, and in conversations with 2+ voices), line accuracy on the hand-labelled lines and the
    lines the wearer reassigned (errors as written). With `overrides` (speakers settings) the replay
    runs as configured and with them, side by side. `only`: HH:MM headings to keep. voices: per
    voice speech seconds and closest voiceprints. Prints counts, tags and names only."""
    out = out or sys.stdout
    base_cfg = load_config(data)["speakers"]
    db = Path(data) / "pages.db"
    if not db.exists():
        print(f"no pages.db in {data}", file=out)
        return {}
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows = [r for r in _conversation_rows(con, day) if not only or (r[3] or "")[:5] in only]
        configs = {"as configured": base_cfg}
        if overrides:
            configs["with " + ", ".join(f"{k}={v}" for k, v in overrides.items())] = {**base_cfg, **overrides}
        runs = {}
        for name, cfg in configs.items():
            runs[name] = _turn_pass(con, data, notes, rows, cfg, cache, _embedder_for(embedder, data, cfg), out)
        reassigned = {}
        for device, start_ms, *_r in rows:
            try:
                reassigned[(device, start_ms)] = con.execute(
                    "SELECT COUNT(DISTINCT para_index) FROM transcript_relabels WHERE device = ? AND start_ms = ? "
                    "AND para_index IS NOT NULL", (device, start_ms)).fetchone()[0]
            except sqlite3.OperationalError:
                reassigned[(device, start_ms)] = 0
    finally:
        con.close()
    names = list(runs)
    print(f"turn mode: {data} ({day or 'all days'}); blips = A-B-A paragraphs of <= {BLIP_WORDS} words, "
          f"long = paragraphs of {LONG_WORDS}+ words (multi: in conversations with 2+ voices)", file=out)
    print("  columns: " + " | ".join(["written"] + names), file=out)

    def fmt(st):
        return (f"{st['lines']} lines, {st['voices']} voices, {st['blips']} blips, {st['long']} long "
                f"({st['long_multi']} multi)")
    total = {n: {k: 0 for k in TURN_KEYS} for n in ["written"] + names}
    acc = {n: {"lines": 0, "right": 0, "speech": 0.0, "right_s": 0.0} for n in names}
    hand = {"lines": 0, "reassigned": 0}
    for device, start_ms, d, heading, _clip in rows:
        key = (device, start_ms)
        if key not in runs[names[0]]:
            continue
        w = written_paragraphs(notes, d, heading)
        wst = paragraph_stats(w) if w is not None else None
        cols = [fmt(wst) if wst else "not in the note"] + [fmt(runs[n][key]["stats"]) for n in names]
        if wst:
            for k in TURN_KEYS:
                total["written"][k] += wst[k]
        extra = []
        for n in names:
            e = runs[n][key]
            for k in TURN_KEYS:
                total[n][k] += e["stats"][k]
            if e["lines"]:
                c = e["lines"]
                acc[n]["lines"] += c["lines"]
                acc[n]["right"] += c["after"]
                acc[n]["speech"] += c["speech"]
                acc[n]["right_s"] += c["after_s"]
            b = e["boundaries"]
            if b:
                extra.append(n + ": " + "; ".join(
                    f"{p} failed" if "error" in r else f"{p} " + ", ".join(f"{k} {v}" for k, v in r.items())
                    for p, r in b.items()))
        lc = [runs[n][key]["lines"] for n in names]
        line_txt = ""
        if lc[0]:
            hand["lines"] += lc[0]["lines"]
            line_txt = " lines right: " + " | ".join(f"{c['after']}/{c['lines']}" for c in lc)
        if reassigned.get(key):
            hand["reassigned"] += reassigned[key]
            line_txt += f"; {reassigned[key]} reassigned by hand (wrong as written)"
        print(f"  {d} {heading[:5]}: " + " | ".join(cols) + line_txt, file=out)
        for x in extra:
            print(f"      {x}", file=out)
        if voices:
            for n in names:
                print(f"      voices ({n}): " + "; ".join(
                    f"{v['tag']} {v['speech_s']:.0f}s{' mixed' if v['mixed'] else ''}"
                    + (" (closest " + ", ".join(f"{p} {s:.2f}" for p, s in v["closest"]) + ")" if v["closest"] else "")
                    for v in runs[n][key]["voices"]), file=out)
    print("  all: " + " | ".join(f"{n}: {fmt(total[n])}" for n in total), file=out)
    if hand["lines"]:
        print("  line accuracy on hand-labelled lines: " + " | ".join(
            f"{n}: {acc[n]['right']}/{acc[n]['lines']} = {_pct(acc[n]['right'] / acc[n]['lines'])} "
            f"(by speech {_pct(acc[n]['right_s'] / acc[n]['speech'] if acc[n]['speech'] else float('nan'))})"
            for n in names) + f"; {hand['reassigned']} lines reassigned by hand", file=out)
    else:
        print(f"  no hand-labelled lines; {hand['reassigned']} lines reassigned by hand", file=out)
    return {"runs": runs, "total": total, "accuracy": acc, "reassigned": hand["reassigned"]}


def _setting(text: str) -> tuple[str, object]:
    """KEY=VALUE for --set: VALUE as JSON (true, 0.2, "x"), else the bare string."""
    import json
    if "=" not in text:
        raise argparse.ArgumentTypeError("expected KEY=VALUE")
    k, v = text.split("=", 1)
    try:
        return k.strip(), json.loads(v)
    except ValueError:
        return k.strip(), v


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--data", type=Path, default=Path(os.environ.get("PENDANT_DATA", Path.home() / "pendant-direct-data")))
    ap.add_argument("--naming", action="store_true",
                    help="voice-level naming rate against false-name rate over match_threshold x match_margin")
    ap.add_argument("--context", action="store_true",
                    help="context bars on the hand-labelled voices: baseline against bars at 1x and 2x")
    ap.add_argument("--spaces", action="store_true",
                    help="naming spaces: the hand-named voices with audio re-embedded with ECAPA and ResNet293, "
                         "window-averaged and whole, at a matched false-accept rate")
    ap.add_argument("--models", type=Path, help="folder holding the WeSpeaker model files (default <data>/models)")
    ap.add_argument("--independent", action="store_true",
                    help="--naming, --context: score each voice on its own instead of naming each conversation's "
                         "voices together (each name once) as live naming does")
    ap.add_argument("--match-margin", type=float, help="override speakers.match_margin (line mode)")
    ap.add_argument("--lines", action="store_true",
                    help="line mode: who said each hand-labelled line, before and after turn rescoring")
    ap.add_argument("--notes", type=Path, default=None,
                    help="Raw notes folder (line mode; read only)")
    ap.add_argument("--day", help="only this day's conversations (line mode, YYYY-MM-DD)")
    ap.add_argument("--cache", type=Path, help="keep transcriptions and window embeddings here (line mode)")
    ap.add_argument("--scoring", choices=("mean", "topk", "calibrated"),
                    help="override speakers.scoring (line mode, --fit-wearer)")
    ap.add_argument("--margin", type=float, help="override speakers.turn_margin (line mode)")
    ap.add_argument("--margin-calibrated", type=float, help="override speakers.turn_margin_calibrated (line mode)")
    ap.add_argument("--person-floor", type=float, help="override speakers.turn_person_floor (line mode)")
    ap.add_argument("--fit-wearer", action="store_true",
                    help="fit the near-field wearer model from labelled turns and report held-out accuracy")
    ap.add_argument("--include-auto", action="store_true",
                    help="--fit-wearer: also train on voices named by voiceprint (optimistic: they lean on the score)")
    ap.add_argument("--save", action="store_true", help="--fit-wearer: write the model to config.json")
    ap.add_argument("--turns", action="store_true",
                    help="turn mode: blips, long paragraphs, voices and line accuracy per conversation, "
                         "as written and replayed (with --set: as configured and with the settings)")
    ap.add_argument("--set", dest="settings", action="append", type=_setting, default=[], metavar="KEY=VALUE",
                    help="override a speakers setting (JSON value; repeatable), e.g. blip_absorb=true")
    ap.add_argument("--only", action="append", metavar="HH:MM",
                    help="turn mode: only the conversation(s) starting at this time")
    ap.add_argument("--voices", action="store_true",
                    help="turn mode: per voice speech seconds and closest voiceprints")
    args = ap.parse_args(argv)
    if args.notes is None:
        from .config import notes_dir
        args.notes = notes_dir(args.data.expanduser())
    over = {k: v for k, v in (("scoring", args.scoring), ("turn_margin", args.margin),
                              ("turn_margin_calibrated", args.margin_calibrated),
                              ("turn_person_floor", args.person_floor),
                              ("match_margin", args.match_margin)) if v is not None}
    over.update(dict(args.settings))
    if args.models:
        over["_models"] = args.models.expanduser()       # where line mode finds the WeSpeaker files
    if args.spaces:
        space_eval(args.data.expanduser(), cache=args.cache.expanduser() if args.cache else None,
                   models=args.models.expanduser() if args.models else None,
                   overrides={k: v for k, v in over.items() if k != "_models"})
        return 0
    if args.naming:
        naming_eval(args.data.expanduser(), independent=args.independent, overrides=over)
        return 0
    if args.context:
        context_eval(args.data.expanduser(), independent=args.independent, overrides=over)
        return 0
    if args.turns:
        turn_eval(args.data.expanduser(), args.notes.expanduser(), args.day,
                  args.cache.expanduser() if args.cache else None, overrides=over or None, only=args.only,
                  voices=args.voices)
        return 0
    if args.fit_wearer:
        fit_wearer(args.data.expanduser(), args.notes.expanduser(), args.day,
                   args.cache.expanduser() if args.cache else None, overrides=over, save=args.save,
                   hand_only=not args.include_auto)
        return 0
    if args.lines:
        line_eval(args.data.expanduser(), args.notes.expanduser(), args.day,
                  args.cache.expanduser() if args.cache else None, overrides=over)
        return 0
    evaluate(args.data.expanduser())
    return 0


if __name__ == "__main__":
    sys.exit(main())
