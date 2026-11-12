"""Calibrated scores (top-k + adaptive s-norm), the cohort, the fallback, legacy
voiceprints and the offline eval, on fake 16-d embeddings."""
from __future__ import annotations

import io
import json
import sqlite3

import numpy as np
import pytest
from test_voices import DEV, env, unit  # noqa: F401

from pendant_direct import calibration as cal
from pendant_direct import closeness, eval_speakers, feed, speakers, voices

DIM = 16
RNG = np.random.default_rng(7)


def voice(center: int, noise: float = 0.15) -> np.ndarray:
    """A unit vector near axis `center` (a "person"), with noise."""
    v = np.zeros(DIM, np.float32)
    v[center] = 1.0
    return unit(v + RNG.normal(0, noise, DIM).astype(np.float32))


def cohort_of(n: int, centers=(8, 9, 10)) -> cal.Cohort:
    embs = np.stack([voice(centers[i % len(centers)], 0.3) for i in range(n)])
    return cal.Cohort(embs, [("d", 100 + i) for i in range(n)], [None] * n)


def test_legacy_voiceprint_scores_as_one_sample_until_replaced(tmp_path):
    vdir = tmp_path / "voiceprints"
    for _ in range(7):
        speakers.add(vdir, "Noah", None, voice(0))
    e, ids = speakers.load_samples(vdir)["Noah"]
    assert e.shape == (1, DIM) and ids == [None]
    assert np.linalg.norm(e[0]) == pytest.approx(1.0)
    speakers.add(vdir, "Noah", 41, voice(1))
    e, ids = speakers.load_samples(vdir)["Noah"]
    assert e.shape == (2, DIM) and ids == [None, 41]
    assert speakers.load_voiceprints(vdir)["Noah"][1] == 8      # the mean voiceprint still reads


def test_old_npy_voiceprint_reads_as_one_sample(tmp_path):
    vdir = tmp_path / "voiceprints"
    vdir.mkdir()
    np.save(vdir / "Noah.npy", voice(0))
    (vdir / "Noah.json").write_text(json.dumps({"count": 5}))
    assert speakers.load_samples(vdir)["Noah"][0].shape == (1, DIM)


def test_top_k_is_mean_of_the_closest_min_3_samples():
    samples = {"A": (np.stack([voice(0, 0.05) for _ in range(5)]), [1, 2, 3, 4, 5]),
               "B": (np.stack([voice(1, 0.05) for _ in range(2)]), [6, 7])}
    c = cal.Calibration(samples, cal.Cohort.empty(DIM))
    x = voice(0, 0.05)
    r = c.scores(x[None])
    sims = np.sort(samples["A"][0] @ x)[::-1]
    assert r.raw[0, 0] == pytest.approx(sims[:3].mean(), abs=1e-5)
    sims_b = np.sort(samples["B"][0] @ x)[::-1]
    assert r.raw[0, 1] == pytest.approx(sims_b.mean(), abs=1e-5)         # k = 2 samples
    

def test_a_person_in_two_settings_matches_either_better_than_their_mean():
    # one person, two "rooms" far apart; the mean sits between and matches neither
    quiet, car = voice(0, 0.02), voice(5, 0.02)
    samples = {"A": (np.stack([quiet] * 3 + [car] * 3), [1, 2, 3, 4, 5, 6])}
    c = cal.Calibration(samples, cal.Cohort.empty(DIM))
    top = c.scores(car[None]).raw[0, 0]
    mean = float(car @ unit(samples["A"][0].mean(0)))
    assert top > mean + 0.1


def test_snorm_matches_the_formula_and_cohort_is_left_clean():
    person = np.stack([voice(0, 0.1) for _ in range(4)])
    c = cal.Calibration({"A": (person, [1, 2, 3, 4])}, cohort_of(12), top_n=4)
    x = voice(0, 0.1)
    r = c.scores(x[None], exclude=("d", 999))
    assert r.calibrated[0, 0] and r.threshold[0, 0] == c.threshold
    raw = np.sort(person @ x)[::-1][:3].mean()
    coh = c.cohort.embs
    top_e = -np.sort(-(coh @ x))[:4]
    top_t = -np.sort(-(person @ coh.T), axis=1)[:, :4]
    want = 0.5 * ((raw - top_e.mean()) / max(top_e.std(), 0.01) + (raw - top_t.mean()) / max(top_t.std(), 0.01))
    assert r.match[0, 0] == pytest.approx(want, abs=1e-4)
    assert want > 2                          # a real match stands far above the cohort


def test_cohort_never_holds_the_voice_its_conversation_or_the_persons_own_samples():
    person = np.stack([voice(0, 0.1) for _ in range(3)])
    base = cohort_of(10)
    # the scored voice sits in the cohort as an unlabeled voice of its own conversation, and the
    # person's own sample as another owner's entry
    x = voice(0, 0.1)
    coh = cal.Cohort(np.vstack([base.embs, x[None], person[:1]]), base.keys + [("d", 1), ("e", 2)],
                     base.owners + [None, "A"], list(range(100, 110)) + [50, 51])
    c = cal.Calibration({"A": (person, [1, 2, 3])}, coh)
    by_key = c.scores(x[None], exclude=("d", 1)).match[0, 0]
    by_id = c.scores(x[None], self_ids=[50]).match[0, 0]     # the voice itself, dropped by its id
    assert by_id == pytest.approx(by_key)
    clean = by_key
    # without the entries it never should have seen the result is the same
    c2 = cal.Calibration({"A": (person, [1, 2, 3])}, cal.Cohort(base.embs, base.keys, base.owners))
    assert c2.scores(x[None]).match[0, 0] == pytest.approx(clean)
    # the voice's own sample is left out of the person's samples
    self_out = c.scores(x[None], self_ids=[1]).raw[0, 0]
    assert self_out == pytest.approx(np.sort(person[1:] @ x)[::-1].mean(), abs=1e-5)


def test_a_small_cohort_falls_back_to_raw_scores_and_the_old_threshold():
    person = np.stack([voice(0, 0.1) for _ in range(3)])
    c = cal.Calibration({"A": (person, [1, 2, 3])}, cohort_of(4), threshold=2.5, raw_threshold=0.5)
    r = c.scores(voice(0, 0.1)[None])
    assert not r.calibrated[0, 0] and r.threshold[0, 0] == 0.5
    assert r.match[0, 0] == pytest.approx(r.raw[0, 0])
    # five voices in the cohort is calibrated; a conversation's own entries don't count
    c = cal.Calibration({"A": (person, [1, 2, 3])}, cohort_of(5))
    assert c.scores(voice(0, 0.1)[None]).calibrated[0, 0]
    assert not c.scores(voice(0, 0.1)[None], exclude=("d", 100)).calibrated[0, 0]
    assert not cal.Calibration({"A": (person[:1], [1])}, cal.Cohort.empty(DIM)).scores(voice(0)[None]).calibrated[0, 0]


def test_voices_score_uses_calibration_for_confidence_and_stays_raw_on_fallback():
    persons = {"A": (np.stack([voice(0, 0.1) for _ in range(4)]), [1, 2, 3, 4]),
               "B": (np.stack([voice(1, 0.1) for _ in range(4)]), [5, 6, 7, 8])}
    c = cal.Calibration(persons, cohort_of(15), threshold=2.5)
    s = voices.score(np.stack([voice(0, 0.1), voice(7, 0.1)]), {}, calibration=c)
    assert s[0].best_name == "A" and s[0].calibrated and s[0].matched and s[0].confidence > 0.5
    assert s[0].threshold == 2.5 and s[0].similarity > 0.8
    assert not s[1].matched                                  # a stranger's voice is under the threshold
    tiny = cal.Calibration(persons, cohort_of(2), threshold=2.5, raw_threshold=0.5)
    f = voices.score(voice(0, 0.1)[None], {}, calibration=tiny)[0]
    assert not f.calibrated and f.threshold == 0.5 and f.matched and f.match_score == pytest.approx(f.similarity)


def test_match_names_with_calibration_and_respects_strangers():
    persons = {"A": (np.stack([voice(0, 0.1) for _ in range(4)]), [1, 2, 3, 4])}
    c = cal.Calibration(persons, cohort_of(15), threshold=2.5)
    cents = np.stack([voice(0, 0.1), voice(7, 0.1)])
    assert speakers.match(cents, {}, 0.5, calibration=c) == ["A", None]
    near = np.stack([cents[0]])
    assert speakers.match(cents, {}, 0.5, strangers=near, calibration=c) == [None, None]


def scoring(env, mode):
    cfg = json.loads((env.data / "config.json").read_text())
    cfg["speakers"]["scoring"] = mode
    (env.data / "config.json").write_text(json.dumps(cfg))


def test_unknown_payload_carries_calibrated_scores(env):
    scoring(env, "calibrated")
    for i in range(6):
        env.add(voice(9 + i % 3, 0.3), tag=f"Speaker {i}", start_ms=3000 + i)
    for i in range(3):
        cid = env.add(voice(0, 0.1), tag="Noah", label="Noah", labeled_by="user", start_ms=4000 + i)
        env.con.execute("UPDATE speaker_clusters SET enrolled_in = 'Noah' WHERE id = ?", (cid,))
        env.enroll("Noah", voice(0, 0.1), cid)
    env.con.commit()
    env.add(voice(0, 0.1), tag="Speaker Z", start_ms=5000)
    env.add(voice(12, 0.1), tag="Speaker Y", start_ms=5001)
    cards = {c["tag"]: c for c in feed.unknown_payload()["speakers"]}
    z, y = cards["Speaker Z"], cards["Speaker Y"]
    assert z["calibrated"] and z["current_label"] == "Noah" and not z["new_voice"] and z["confidence"] > 0.5
    assert y["calibrated"] and y["current_label"] is None and y["new_voice"]
    assert z["score"] > y["score"] and "new_cut" not in z


# ---------------------------------------------------------------------------
# the offline eval

def populate_eval(env):
    for i in range(5):
        env.add(voice(0, 0.1), tag="Noah", label="Noah", labeled_by="user", start_ms=7000 + i)
        env.add(voice(1, 0.1), tag="Alex", label="Alex", labeled_by="user", start_ms=7100 + i)
    for i in range(8):
        env.add(voice(9 + i % 3, 0.3), start_ms=7200 + i)               # unlabeled cohort
    for i in range(3):
        cid = env.add(voice(0, 0.1), tag="Noah", label="Noah", labeled_by="match", start_ms=7300 + i)
    env.con.commit()


def test_eval_prints_counts_and_accuracies_only_and_changes_nothing(env):
    populate_eval(env)
    before = [tuple(r) for r in env.con.execute("SELECT * FROM speaker_clusters ORDER BY id")]
    out = io.StringIO()
    res = eval_speakers.evaluate(env.data, out)
    text = out.getvalue()
    assert "hand answers: 10 named voices (2 people: Alex 5, Noah 5)" in text
    assert "NOTE: only 10 test voices (< 30)" in text
    assert "scoring modes side by side" in text and "mean" in text and "topk" in text and "calibrated" in text
    assert "suggested threshold" in text and "stays \"mean\"" in text
    assert "these are some spoken words" not in text                    # never transcript text
    for m in ("mean", "topk", "calibrated"):
        d = res[m]["configured"]
        assert d["accuracy"] == 1.0 and d["wrong"] == 0 and d["auto_kept"] == 3
    assert [tuple(r) for r in env.con.execute("SELECT * FROM speaker_clusters ORDER BY id")] == before
    assert not env.vdir.exists() or not list(env.vdir.glob("*.npz"))


def test_eval_counts_strangers_as_negatives_and_runs_as_a_command(env, capsys):
    populate_eval(env)
    for i in range(3):
        cid = env.add(voice(11, 0.1), tag="Someone", labeled_by="user", start_ms=7400 + i)
        env.con.execute("UPDATE speaker_clusters SET label_kind = 'stranger' WHERE id = ?", (cid,))
        env.con.execute("INSERT INTO strangers (cluster_id, embedding, created_at) "
                        "SELECT id, embedding, 0 FROM speaker_clusters WHERE id = ?", (cid,))
    env.con.commit()
    assert eval_speakers.main(["--data", str(env.data)]) == 0
    text = capsys.readouterr().out
    assert "3 'random person' voices" in text
    assert "0/13" in text


def test_eval_without_pages_db_says_so(tmp_path):
    out = io.StringIO()
    assert eval_speakers.evaluate(tmp_path, out) == {} and "no pages.db" in out.getvalue()


def test_suggest_takes_the_middle_of_the_best_range():
    ms = [{"pos": 9, "accuracy": a, "false_rate": f, "fa_rate": 0.0} for a, f in
          [(1.0, 0.5), (0.9, 0.0), (0.9, 0.0), (0.9, 0.0), (0.5, 0.0)]]
    assert eval_speakers.suggest([1, 2, 3, 4, 5], ms)[0] == 3
    assert eval_speakers.suggest([1], [{"pos": 3, "accuracy": 1.0, "false_rate": 0.5, "fa_rate": 0.0}]) is None


def test_config_default_threshold_is_calibrated_and_consent_is_unchanged(env):
    from pendant_direct import config
    assert config.DEFAULTS["speakers"]["match_threshold_calibrated"] > 0
    with pytest.raises(speakers.ConsentError):
        speakers.enroll(env.vdir, "Mallory", voice(0), ["Noah"], cluster_id=1)


# ---------------------------------------------------------------------------
# review round 1

def _two(c_a, c_b=None):
    return {"A": (np.stack([voice(0, 0.1) for _ in range(4)]), [1, 2, 3, 4]), **(c_b or {})}


def test_a_similar_stranger_stays_in_the_cohort_and_is_not_named():
    person = np.stack([voice(0, 0.05) for _ in range(5)])
    base = cohort_of(14)
    # a stranger who sounds like A (cosine about 0.6 to A's samples), recorded in another conversation
    x = unit(person.mean(0) * 0.6 + voice(3, 0.0) * 0.8)
    near = cal.Cohort(np.vstack([base.embs, x[None]]), base.keys + [("s", 1)], base.owners + [cal.STRANGER])
    c = cal.Calibration({"A": (person, [1, 2, 3, 4, 5])}, near, threshold=2.5)
    assert 0.5 < (person @ x).mean() < 0.7
    # the stranger voice itself, scored from another conversation, is not named...
    probe = unit(x + voice(4, 0.02) * 0.05)
    r = c.scores(probe[None], exclude=("p", 9))
    assert r.calibrated[0, 0] and r.match[0, 0] < 2.5
    # ...and the similar stranger is still in the cohort (no similarity filter drops it)
    assert any(o == cal.STRANGER for o in c.cohort.owners)
    s = voices.score(probe[None], {}, calibration=c, exclude=("p", 9))[0]
    assert not s.matched


def test_one_persons_small_cohort_does_not_change_anothers_calibrated_decision():
    noah = np.stack([voice(0, 0.05) for _ in range(4)])
    alex = np.stack([voice(1, 0.05) for _ in range(4)])
    base = cohort_of(12)
    # Alex owns 8 of the 12-voice cohort's entries, leaving her under 5 voices; Noah keeps plenty
    owners = ["Alex"] * 8 + [None] * 4
    coh = cal.Cohort(base.embs, base.keys, owners)
    both = cal.Calibration({"Noah": (noah, [1, 2, 3, 4]), "Alex": (alex, [5, 6, 7, 8])}, coh)
    alone = cal.Calibration({"Noah": (noah, [1, 2, 3, 4])}, coh)
    x = voice(0, 0.05)
    r, r1 = both.scores(x[None]), alone.scores(x[None])
    assert r.calibrated[0, 0] and r1.calibrated[0, 0]
    assert not r.calibrated[0, 1] and r.threshold[0, 1] == 0.5          # Alex falls back by herself
    assert r.match[0, 0] == pytest.approx(r1.match[0, 0])
    s = voices.score(x[None], {}, calibration=both)[0]
    assert s.best_name == "Noah" and s.calibrated and s.matched


def test_stranger_veto_compares_with_the_top_k_score():
    # top-3 cosines 0.9 / 0.4 / 0.4 (mean 0.567) against a stranger at 0.8: vetoed
    e = np.eye(DIM, dtype=np.float32)
    def at(cos, axis):
        return unit(e[0] * cos + e[axis] * np.sqrt(1 - cos * cos))
    samples = np.stack([at(0.9, 1), at(0.4, 2), at(0.4, 3)])
    # samples are unit vectors with cosine c to e0
    x = e[0]
    stranger = at(0.8, 4)[None]
    c = cal.Calibration({"A": (samples, [1, 2, 3])}, cal.Cohort.empty(DIM), raw_threshold=0.5)
    assert c.scores(x[None]).raw[0, 0] == pytest.approx(0.5667, abs=1e-3)
    s = voices.score(x[None], {}, strangers=stranger, calibration=c)[0]
    assert s.stranger_closer and s.best_name is None
    assert speakers.match(x[None], {}, 0.5, strangers=stranger, calibration=c) == [None]
    # a stranger below the top-k score doesn't veto
    far = at(0.5, 4)[None]
    assert voices.score(x[None], {}, strangers=far, calibration=c)[0].best_name == "A"
    assert speakers.match(x[None], {}, 0.5, strangers=far, calibration=c) == ["A"]


def test_bulk_match_uses_the_calibrated_score(env):
    scoring(env, "calibrated")
    tapped_emb = voice(0, 0.05)
    tid = env.add(tapped_emb, tag="Speaker B", start_ms=8000)
    near = env.add(voice(0, 0.1), tag="Speaker A", start_ms=8100)
    far = env.add(voice(12, 0.1), tag="Speaker A", start_ms=8200)
    for i in range(8):
        env.add(voice(9 + i % 3, 0.3), tag="Speaker A", start_ms=8300 + i)
    for k in (8000, 8100, 8200, *range(8300, 8308)):
        env.con.execute("INSERT OR IGNORE INTO conversations (device, start_ms, day, raw_heading) "
                        "VALUES (?, ?, '2026-11-08', 'h')", (DEV, k))
    env.con.commit()
    tapped = feed._tapped_voice(env.con, DEV, 8000, "Speaker B")
    m = feed.bulk_match(env.con, tapped, "Noah")
    ids = {h["id"]: h for h in m["voices"]}
    assert near in ids and far not in ids and tid not in ids
    assert ids[near]["calibrated"] and ids[near]["score"] >= 1.5


# ---------------------------------------------------------------------------
# speakers.scoring

def old_match(centroids, vps, threshold, strangers=None):
    """The matcher as it was before per-sample scoring (the oracle for "mean")."""
    out = [None] * len(centroids)
    names = list(vps)
    vp = np.stack([unit(vps[n][0]) for n in names])
    sims = np.stack([unit(c) for c in centroids]) @ vp.T
    near = np.full(len(centroids), -np.inf)
    if strangers is not None and len(strangers):
        near = (np.stack([unit(c) for c in centroids]) @ np.stack([unit(t) for t in strangers]).T).max(axis=1)
    pairs = sorted(((sims[i, j], i, j) for i in range(len(centroids)) for j in range(len(names))), reverse=True)
    used = set()
    for s, i, j in pairs:
        if s < threshold:
            break
        if s <= near[i]:
            continue
        if out[i] is None and j not in used:
            out[i] = names[j]
            used.add(j)
    return out


def old_score(embs, vps, strangers):
    names = list(vps)
    x = np.stack([unit(e) for e in embs])
    sims = x @ np.stack([unit(vps[n][0]) for n in names]).T
    out = []
    for i in range(len(x)):
        order = np.argsort(-sims[i])
        best = sims[i, order[0]]
        second = max(sims[i, order[1]], 0.0) if len(names) > 1 else 0.0
        near = max((float(x[i] @ unit(t)) for t in strangers), default=-np.inf)
        closer = near > best
        conf = 0.0 if closer else float(np.clip(min(best, 2 * (best - second)), 0, 1))
        out.append((None if closer else names[order[0]], round(float(best), 5), round(float(best - second), 5),
                    round(conf, 5), bool(closer)))
    return out


def test_default_scoring_is_mean_and_reproduces_the_old_decisions(tmp_path, env):
    from pendant_direct import config
    assert config.DEFAULTS["speakers"]["scoring"] == "mean"
    assert config.scoring_mode({}) == "mean" and config.scoring_mode({"scoring": "nonsense"}) == "mean"
    vdir = tmp_path / "vp"
    for i, name in enumerate(["Noah", "Alex", "Sam"]):
        for k in range(4):
            speakers.add(vdir, name, 10 * i + k, voice(i, 0.4))
    vps = speakers.load_voiceprints(vdir)
    strangers = np.stack([voice(5, 0.3), voice(0, 0.9)])
    cents = np.stack([voice(c, 0.5) for c in [0, 1, 2, 3, 0, 1, 7, 2]])
    # the config default turns calibration off entirely
    cfg = config.load(env.data)["speakers"]
    assert cal.load(env.con, vdir, cfg) is None
    for st in (None, strangers):
        for thr in (0.3, 0.5, 0.7):
            assert speakers.match(cents, vps, thr, st) == old_match(cents, vps, thr, st)
    got = voices.score(cents, vps, strangers)
    want = old_score(cents, vps, strangers)
    assert [(s.best_name, round(s.similarity, 5), round(s.margin, 5), round(s.confidence, 5), s.stranger_closer)
            for s in got] == want
    # bulk matching by default is the cosine to the tapped voice against match_threshold
    tid = env.add(voice(0, 0.05), tag="Speaker B", start_ms=9000)
    close = env.add(voice(0, 0.2), tag="Speaker A", start_ms=9100)
    away = env.add(voice(4, 0.2), tag="Speaker A", start_ms=9200)
    for k in (9000, 9100, 9200):
        env.con.execute("INSERT INTO conversations (device, start_ms, day, raw_heading) VALUES (?, ?, '2026-11-08', 'h')",
                        (DEV, k))
    env.con.commit()
    m = feed.bulk_match(env.con, feed._tapped_voice(env.con, DEV, 9000, "Speaker B"), "Noah")
    hit = {h["id"]: h for h in m["voices"]}
    assert close in hit and away not in hit and not hit[close]["calibrated"]
    assert hit[close]["score"] == hit[close]["similarity"] or abs(hit[close]["score"] - hit[close]["similarity"]) < 1e-3


def test_topk_mode_scores_raw_top_k_without_a_cohort(env):
    for k in range(4):
        speakers.add(env.vdir, "Noah", 20 + k, voice(0, 0.3))
    scoring(env, "topk")
    from pendant_direct import config
    c = cal.load(env.con, env.vdir, config.load(env.data)["speakers"])
    assert c.mode == "topk" and len(c.cohort) == 0
    r = c.scores(voice(0, 0.3)[None])
    assert not r.calibrated.any() and r.threshold[0, 0] == config.DEFAULTS["speakers"]["match_threshold"] and r.match[0, 0] == r.raw[0, 0]
    scoring(env, "calibrated")
    assert cal.load(env.con, env.vdir, config.load(env.data)["speakers"]).mode == "calibrated"


def test_duplicate_filter_drops_only_the_scored_voice_by_id():
    person = np.stack([voice(0, 0.1) for _ in range(3)])
    base = cohort_of(10)
    x = voice(0, 0.1)
    # entry 50 is the scored voice; 51 is a different recording that is exactly as similar (a copy)
    coh = cal.Cohort(np.vstack([base.embs, x[None], x[None]]), base.keys + [("p", 1), ("q", 2)],
                     base.owners + [None, None], list(range(100, 110)) + [50, 51])
    mask = coh.mask(("p", 1), {50})
    assert mask.sum() == 11 and not mask[10] and mask[11]            # only the voice itself goes
    only_self = coh.mask(None, {50})
    assert not only_self[10] and only_self[11] and only_self.sum() == 11
    assert coh.mask(None, set()).all()                               # no id, nothing dropped by similarity
    # the mask scoring really uses: with the copy kept, the cohort holds one more near-identical voice
    c = cal.Calibration({"A": (person, [1, 2, 3])}, coh, top_n=4)
    kept = c.scores(x[None], self_ids=[50])
    no_copy = cal.Calibration({"A": (person, [1, 2, 3])}, cal.Cohort(
        np.vstack([base.embs]), base.keys, base.owners, list(range(100, 110))), top_n=4).scores(x[None])
    assert kept.match[0, 0] < no_copy.match[0, 0]                    # the copy raises mu: it was kept
    # and cohort.without keeps ids aligned
    assert coh.without(("q", 2)).ids == list(range(100, 110)) + [50]


# ---------------------------------------------------------------------------
# ranking: margin above each person's own threshold, in every place and mode

class FixedCal(cal.Calibration):
    """Fixed scores per person column ({name: (match, raw, threshold, calibrated)}); a
    conversation's own voice columns (turn rescoring) score a flat 0.1."""
    table: dict = {}

    def scores(self, x, exclude=None, self_ids=None):
        n = len(x)
        cols = [self.table.get(nm, (0.1, 0.1, 0.5, False)) for nm in self.names]
        a = lambda i, dt=float: np.tile(np.array([c[i] for c in cols], dtype=dt), (n, 1))   # noqa: E731
        return cal.Result(a(1), a(0), a(3, bool), a(2))


def fixed(table, names=("Ann", "Bea")):
    samples = {n: (np.eye(4, dtype=np.float32)[i][None], [i + 1]) for i, n in enumerate(names)}
    # a subclass per table: dataclasses.replace (turn rescoring) rebuilds the instance
    return type("Fixed", (FixedCal,), {"table": table})(samples, cal.Cohort.empty(4))


MIXED = {"Ann": (1.6, 0.7, 1.5, True), "Bea": (0.9, 0.9, 0.5, False)}   # calibrated 1.6 at 1.5 vs raw 0.9 at 0.5
CENTS = np.eye(4, dtype=np.float32)[:1]


def test_a_calibrated_and_a_raw_person_pick_the_same_name_everywhere():
    c = fixed(MIXED)
    # margins: Ann 0.1, Bea 0.4. The processor, the feed's Voice Scoring and rescoring all pick Bea.
    assert speakers.match(CENTS, {}, 0.5, calibration=c) == ["Bea"]
    assert voices.score(CENTS, {}, calibration=c)[0].best_name == "Bea"
    for prior in ({}, {"Ann": 0.0, "Bea": 0.0}):
        assert speakers.match(CENTS, {}, 0.5, calibration=c, prior=prior) == ["Bea"]
        assert voices.score(CENTS, {}, calibration=c, prior=prior)[0].best_name == "Bea"


def test_turn_rescoring_picks_the_same_person_as_match():
    from test_turn_rescore import conversation
    for prior in (None, {}, {"Ann": 0.0, "Bea": 0.0}):
        c = fixed(MIXED)
        segs, _emb = conversation([(CENTS[0], 4), (CENTS[0], 4)])
        words = [w for s in segs for w in s.words]
        wseg = [i for i, s in enumerate(segs) for _w in s.words]
        windows, _v = speakers.voice_windows(words)
        x = np.tile(CENTS, (len(windows), 1))
        # the voice is unnamed; its centroid agrees with the scored person
        sc = speakers.TurnScorer({}, calibration=c, prior=prior)
        out, _c, names, _i = speakers.rescore_turns(words, wseg, [0] * len(words), windows, x, CENTS, [None], sc)
        assert {names[out[i]] for i in range(len(words))} == {"Bea"}


def test_zero_priors_and_no_prior_rank_identically_in_every_mode():
    rng = np.random.default_rng(3)
    for _ in range(300):
        P = 4
        names = [f"P{i}" for i in range(P)]
        thr = rng.choice([0.5, 1.0, 1.5], size=P)
        calb = rng.random(P) < 0.5
        match = np.where(calb, rng.normal(1.2, 0.8, P), rng.uniform(0.2, 0.9, P))
        table = {n: (float(match[i]), float(match[i]) / 3, float(thr[i]), bool(calb[i])) for i, n in enumerate(names)}
        c = fixed(table, names)
        x = np.tile(np.eye(4, dtype=np.float32)[:1], (1, 1))
        zeros = {n: 0.0 for n in names}
        assert speakers.match(x, {}, 0.5, calibration=c) == speakers.match(x, {}, 0.5, calibration=c, prior=zeros)
        a = voices.score(x, {}, calibration=c)[0]
        b = voices.score(x, {}, calibration=c, prior=zeros)[0]
        assert (a.best_name, a.margin) == (b.best_name, b.margin)
    # the mean (voiceprint) mode, one shared threshold
    for _ in range(300):
        vps = {f"P{i}": (speakers.normalize(rng.normal(size=4).astype(np.float32)), 3) for i in range(4)}
        x = speakers.normalize(rng.normal(size=(3, 4)).astype(np.float32))
        zeros = {n: 0.0 for n in vps}
        assert speakers.match(x, vps, 0.0) == speakers.match(x, vps, 0.0, prior=zeros)
        assert [s.best_name for s in voices.score(x, vps)] == [s.best_name for s in voices.score(x, vps, prior=zeros)]


def test_mean_mode_decisions_are_unchanged_by_margin_ranking():
    """With one shared threshold, margin ranking is score ranking: the old and the new pair order
    (and so every decision) agree."""
    rng = np.random.default_rng(11)
    for _ in range(300):
        n, P = int(rng.integers(1, 5)), int(rng.integers(1, 5))
        vps = {f"P{i}": (speakers.normalize(rng.normal(size=4).astype(np.float32)), 3) for i in range(P)}
        x = speakers.normalize(rng.normal(size=(n, 4)).astype(np.float32))
        thr = float(rng.choice([0.0, 0.3, 0.5]))
        names = list(vps)
        sims = x @ speakers.normalize(np.stack([vps[k][0] for k in names])).T
        old = sorted(((sims[i, j], i, j) for i in range(n) for j in range(P)), reverse=True)
        new = sorted(((closeness.pair_key(sims[i, j], thr, None), i, j) for i in range(n) for j in range(P)), reverse=True)
        assert [(i, j) for _s, i, j in old] == [(i, j) for _s, i, j in new]
