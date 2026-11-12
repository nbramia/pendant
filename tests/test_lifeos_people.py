"""LifeOS people: links, suggestions, and the closeness naming prior."""
from __future__ import annotations

import io
import json
import threading
import urllib.error
import urllib.request
from types import SimpleNamespace

import numpy as np
import pytest

from pendant_direct import closeness, config, feed, lifeos_people, link_people, speakers, voices

PEOPLE = [
    {"entity_id": "e-lex-p", "display_name": "Alex", "relationship_strength": 59.5, "days_since_contact": 0,
     "category": "personal"},
    {"entity_id": "e-lex-w", "display_name": "Alex", "relationship_strength": 24.7, "days_since_contact": 215,
     "category": "personal"},
    {"entity_id": "e-alex-rivera", "display_name": "Alex Rivera", "relationship_strength": 100.0,
     "days_since_contact": 0, "category": "family"},
    {"entity_id": "e-lex-old", "display_name": "Alex Stone", "relationship_strength": 59.5, "days_since_contact": 30,
     "category": "personal"},
    {"entity_id": "e-bot", "display_name": "Your activity", "email": "no-reply@github.com",
     "relationship_strength": 99.0, "days_since_contact": 0, "category": "personal"},
    {"entity_id": "e-bad", "display_name": "J. R. R. 123", "relationship_strength": 90.0, "days_since_contact": 0},
    {"entity_id": "e-riley", "display_name": "Riley", "relationship_strength": 80.0, "days_since_contact": 2,
     "category": "family"},
]


def fake_lifeos(people=PEOPLE, calls=None):
    def get(path, params):
        if calls is not None:
            calls.append((path, dict(params)))
        if path == "/api/people/search":
            q = params["q"].lower()
            return {"people": [p for p in people if q in p["display_name"].lower()]}
        if path == "/api/people/list":
            return {"people": list(people)}
        if path.startswith("/api/people/entity/"):
            eid = path.rsplit("/", 1)[1]
            for p in people:
                if p["entity_id"] == eid:
                    return p
            raise RuntimeError("404")
        if path == "/api/calendar/search":
            return {"events": []}
        raise RuntimeError(path)
    return get


def down(path, params):
    raise OSError("LifeOS is down")


# ---------------------------------------------------------------- the CRM people list

def crm(eid, name, circle, strength, category="personal", emails=(), company=None, display=None):
    return {"id": eid, "canonical_name": name, "display_name": display or name, "dunbar_circle": circle,
            "relationship_strength": strength, "category": category, "emails": list(emails), "company": company}


CRM = [
    crm("e-lex-w", "Alex Rivera", 0, 100.0, "family", ["alex.rivera@example.com"]),
    crm("e-noah", "Noah Park", 0, 100.0, "self", ["noah@home.example"]),
    crm("e-riley", "Riley Park", 0, 100.0, "family"),
    crm("e-omara", "O'Mara Alex", 2, 85.1),
    crm("e-lex-frag", "Alex", 4, 59.5),
    crm("e-lex-solo", "Alex", 5, 24.7, emails=["alex@example.org"], company="Studio Nine"),
    crm("e-lex-moore", "Alex Moore", 5, 30.2),
    crm("e-noah-stub", "Noah", 4, 63.3, "work", ["noah.r@work.example"]),
    crm("e-nate", "Jonah Price", 3, 76.9),
    crm("e-bot", "Your activity", 0, 99.0, emails=["no-reply@github.com"]),
    crm("e-bad", "123 456", 0, 90.0),
    crm("e-ben", "Ben Ramos", 1, 96.9, "family"),
    crm("e-riley-doc", "Riley Doctor Paula Grant", 5, 33.6),
    crm("e-zoe", "Zo\u00eb Lane", 3, 40.0),
]


def fake_crm(people=CRM, calls=None, me="e-noah"):
    def get(path, params):
        if calls is not None:
            calls.append((path, dict(params)))
        if path == "/api/crm/people":
            q = params["q"].lower()
            rows = [p for p in people if not q or all(t in p["canonical_name"].lower() for t in q.split())]
            rows.sort(key=lambda p: -p["relationship_strength"])
            return {"people": rows[:params["limit"]]}
        if path == "/api/crm/config":
            return {"my_person_id": me}
        if path.startswith("/api/crm/people/"):
            for p in people:
                if p["id"] == path.rsplit("/", 1)[1]:
                    return p
            raise urllib.error.HTTPError(path, 404, "not found", {}, None)
        raise RuntimeError(path)
    return get


# ---------------------------------------------------------------- suggestions

def test_suggest_lifeos_first_by_circle_then_strength_with_full_names_and_labels():
    out = lifeos_people.suggest("lex", [{"name": "Noah"}], ["Noah", "Zed"], {}, get=fake_crm())
    assert out["lifeos"] is True
    rows = [(p["name"], p["label"], p["circle"], p["category"]) for p in out["people"]]
    assert rows[0] == ("Alex Rivera", "Alex Rivera", 0, "family")       # closest circle first
    assert [r[0] for r in rows[:3]] == ["Alex Rivera", "O'Mara Alex", "Alex"]
    assert out["people"][-1] == {**out["people"][-1], "name": "Lex", "label": "Lex", "new": True, "entity_id": None}
    assert [p["new"] for p in out["people"]].count(True) == 1               # the typed name is only the last row
    assert "Zed" not in [p["name"] for p in out["people"]]                  # known voices are filtered by q too


def test_suggest_same_full_name_twice_gets_distinct_labels():
    labels = [p["label"] for p in lifeos_people.suggest("alex", [], [], {}, get=fake_crm())["people"] if not p["new"]]
    assert len(labels) == len({l.lower() for l in labels})
    assert "Alex" in labels and "Alex S" in labels                      # Studio Nine's initial


def test_suggest_marks_known_voices_and_keeps_a_linked_name():
    out = lifeos_people.suggest("", [{"name": "Alex"}], ["Noah", "Alex", "Riley Park"], {"Alex": "e-lex-w"},
                                get=fake_crm())
    by = {p["entity_id"]: p for p in out["people"] if p["entity_id"]}
    assert by["e-lex-w"]["label"] == "Alex" and by["e-lex-w"]["known"] and by["e-lex-w"]["voiceprint"]
    assert by["e-lex-w"]["name"] == "Alex Rivera"                         # shown in full, labelled as already named
    assert by["e-riley"]["known"] and not by["e-riley"]["voiceprint"]       # allowed, no voice yet
    assert not by["e-ben"]["known"]
    # nothing typed: tagged people come first (enrolled, then allowed), then the closest LifeOS circles
    names = [p["label"] for p in out["people"]]
    assert names[:4] == ["Alex", "Noah", "Riley Park"] + names[3:4]
    assert all(p["known"] for p in out["people"][:3]) and not any(p["known"] for p in out["people"][3:])
    assert names.index("Noah") < names.index("Ben Ramos")


def test_suggest_typed_query_keeps_lifeos_people_before_known_voices():
    out = lifeos_people.suggest("ram", [{"name": "Ramona"}], ["Ramona"], {}, get=fake_crm())
    names = [p["label"] for p in out["people"]]
    assert names.index("Ben Ramos") < names.index("Ramona")


def test_suggest_empty_query_lists_the_closest_circles_and_drops_unusable_records():
    out = lifeos_people.suggest("", [], [], {}, get=fake_crm())
    circles = [p["circle"] for p in out["people"]]
    assert circles == sorted(circles)
    ids = {p["entity_id"] for p in out["people"]}
    assert "e-bot" not in ids and "e-bad" not in ids                        # no-reply senders, names a speaker can't have
    assert next(p for p in out["people"] if p["entity_id"] == "e-zoe")["label"] == "Zoe Lane"   # accents folded
    assert not any(p["new"] for p in out["people"])                         # nothing typed, no new row


def test_suggest_falls_back_to_known_voices_and_free_text_when_lifeos_is_down():
    out = lifeos_people.suggest("no", [{"name": "Noah"}], ["Noah", "Dana", "Nora"], {}, get=down)
    assert out["lifeos"] is False
    assert [(p["label"], p["known"], p["new"]) for p in out["people"]] == [
        ("Noah", True, False), ("Nora", True, False), ("No", False, True)]


def test_suggestions_create_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("PENDANT_DATA", str(tmp_path))
    (tmp_path / "config.json").write_text(json.dumps({"speakers": {"allowed": ["Noah"]}}))
    before = (tmp_path / "config.json").read_text()
    monkeypatch.setattr(lifeos_people, "_api", lambda path, params, timeout=10.0: fake_crm()(path, params))
    out = feed.suggest_payload("Riley")
    assert [p["name"] for p in out["people"]][:2] == ["Riley Park", "Riley Doctor Paula Grant"]
    assert (tmp_path / "config.json").read_text() == before        # no allowed name, no link
    assert not (tmp_path / "voiceprints").exists()


# ---------------------------------------------------------------- link_people

def write_cfg(tmp_path, **sp):
    (tmp_path / "config.json").write_text(json.dumps({"summarizer": "heuristic", "speakers": sp}))


def test_link_people_wearer_full_name_and_dominant_first_names(tmp_path):
    write_cfg(tmp_path, allowed=["Noah", "Alex", "Riley", "Ben", "Unknownperson"], wearer="Noah",
              people={"Ben": "e-keep"})
    out = io.StringIO()
    chosen = link_people.run(tmp_path, get=fake_crm(), out=out)
    assert chosen == {"Noah": "e-noah", "Alex": "e-lex-w", "Riley": "e-riley"}
    text = out.getvalue()
    assert "Noah: Noah Park (e-noah) circle 0" in text and "Alex: Alex Rivera" in text
    assert "Unknownperson: ambiguous, not linked" in text
    cfg = json.loads((tmp_path / "config.json").read_text())
    assert cfg["speakers"]["people"] == {"Ben": "e-keep", "Noah": "e-noah", "Alex": "e-lex-w", "Riley": "e-riley"}
    assert cfg["summarizer"] == "heuristic" and cfg["speakers"]["allowed"][0] == "Noah"


def test_link_people_never_takes_the_fragment_or_the_work_stub():
    chosen, ambiguous = lifeos_people.choose_links(["Noah", "Alex"], wearer="Noah", get=fake_crm())
    assert chosen["Noah"]["entity_id"] == "e-noah" and chosen["Alex"]["entity_id"] == "e-lex-w"
    # without the wearer rule the first-name rule still lands on the right person (100 vs 63.3)
    chosen, _ = lifeos_people.choose_links(["Noah"], get=fake_crm())
    assert chosen["Noah"]["entity_id"] == "e-noah"


def test_link_people_ambiguous_first_names_link_nothing():
    people = [crm("a", "Sam Ford", 1, 80.0), crm("b", "Sam Lee", 1, 70.0),           # not 1.5x ahead
              crm("c", "Pat Roe", 3, 90.0), crm("d", "Pat Poe", 5, 10.0),            # ahead, but circle 3
              crm("e", "Kim Roe", 1, 50.0, display="Kim")]
    chosen, ambiguous = lifeos_people.choose_links(["Sam", "Pat"], get=fake_crm(people))
    assert chosen == {} and set(ambiguous) == {"Sam", "Pat"}
    assert [r["name"] for r in ambiguous["Sam"]] == ["Sam Ford", "Sam Lee"]
    chosen, _ = lifeos_people.choose_links(["Kim"], get=fake_crm(people))
    assert chosen["Kim"]["entity_id"] == "e"                                     # the only Kim, close circle
    chosen, _ = lifeos_people.choose_links(["Sam Lee", "Kim Roe"], get=fake_crm(people))
    assert chosen["Sam Lee"]["entity_id"] == "b" and chosen["Kim Roe"]["entity_id"] == "e"   # full names


def test_link_people_zero_strength_and_tied_names_stay_ambiguous():
    people = [crm("a", "Sam Ford", 1, 0.0), crm("b", "Sam Lee", 5, 0.0),         # no strength at all
              crm("c", "Pat Roe", 1, 50.0), crm("d", "Pat Poe", 1, 50.0)]        # tied
    chosen, ambiguous = lifeos_people.choose_links(["Sam", "Pat"], get=fake_crm(people))
    assert chosen == {} and set(ambiguous) == {"Sam", "Pat"}


def test_link_people_dry_run_changes_nothing(tmp_path):
    write_cfg(tmp_path, allowed=["Noah", "Alex"], wearer="Noah")
    before = (tmp_path / "config.json").read_text()
    out = io.StringIO()
    chosen = link_people.run(tmp_path, dry_run=True, get=fake_crm(), out=out)
    assert chosen == {"Noah": "e-noah", "Alex": "e-lex-w"}
    assert (tmp_path / "config.json").read_text() == before
    assert "dry run: would link 2" in out.getvalue()
    assert [p.name for p in tmp_path.iterdir()] == ["config.json"]   # not even a temp file


def test_link_people_lifeos_down_fails_cleanly_without_writing(tmp_path, monkeypatch, capsys):
    write_cfg(tmp_path, allowed=["Alex"])
    before = (tmp_path / "config.json").read_text()
    monkeypatch.setattr(lifeos_people, "_api", lambda *a, **k: down("", {}))
    assert link_people.main(["--data", str(tmp_path)]) == 1
    assert (tmp_path / "config.json").read_text() == before


def test_config_link_rejects_bad_ids_and_keeps_one_spelling(tmp_path):
    write_cfg(tmp_path, allowed=["Alex"], people={"alex": "e-old"})
    with pytest.raises(ValueError):
        config.link_people(tmp_path, {"Alex": "../x"})
    config.link_people(tmp_path, {"Alex": "e-new"}, replace=True)
    assert json.loads((tmp_path / "config.json").read_text())["speakers"]["people"] == {"Alex": "e-new"}
    assert config.linked_entity({"Alex": "e-new"}, "ALEX") == "e-new"


# ---------------------------------------------------------------- the prior

def test_prior_formula():
    c = {"weight": 0.1, "weight_calendar": 0.1, "weight_age": 0.05}
    assert closeness.prior(100, 0, True, c) == pytest.approx(0.2)
    assert closeness.prior(59.5, 0, False, c) == pytest.approx(0.0595)
    assert closeness.prior(0, 365, False, c) == pytest.approx(-0.05)
    assert closeness.prior(0, 4000, False, c) == pytest.approx(-0.05)      # age is capped at a year
    assert closeness.prior(None, None, False, c) == pytest.approx(-0.05)   # unknown = distant


def test_rerank_key_never_lifts_below_threshold():
    excess = np.array([[0.30, 0.28, -0.01]])
    prior = np.array([0.0, 0.1, 5.0])                   # the third is hugely close but below its threshold
    order = np.argsort(-closeness.rerank_key(excess, prior), axis=1)[0]
    assert list(order) == [1, 0, 2]                     # near-tie flipped; the sub-threshold one stays last
    only = np.array([[-0.02, 0.01, -0.5]])
    assert np.argmax(closeness.rerank_key(only, np.array([9.0, 0.0, 9.0]))) == 1   # sole qualifier wins
    assert list(closeness.rerank_key(excess, None)[0]) == [0.30, 0.28, -0.01]      # no prior, no change


def test_negative_prior_cannot_drop_a_qualifier_below_a_non_qualifier():
    excess = np.array([[0.0, -0.001]])
    key = closeness.rerank_key(excess, np.array([-0.05, 0.9]))
    assert key[0, 0] > key[0, 1]


def unit(*v):
    return np.array(v, dtype=np.float32)


def test_match_prior_reorders_only_clearing_candidates_mean_mode():
    # one voice, equally close to Ann and Bea (both clear 0.5); one voice only Bea clears
    ann, bea = unit(1, 0), unit(0, 1)
    vps = {"Ann": (ann, 3), "Bea": (bea, 3)}
    tie = speakers.normalize(unit(0.70, 0.71))
    assert speakers.match(np.stack([tie]), vps, 0.5) == ["Bea"]                    # Bea is a hair closer
    assert speakers.match(np.stack([tie]), vps, 0.5, prior={"Ann": 0.1}) == ["Ann"]  # the close friend wins the near-tie
    far = speakers.normalize(unit(0.3, 0.95))                                        # Ann: ~0.30, below 0.5
    assert speakers.match(np.stack([far]), vps, 0.5, prior={"Ann": 5.0}) == ["Bea"]  # prior can't lift Ann over the line
    unrelated = speakers.normalize(unit(-1, -1))
    assert speakers.match(np.stack([unrelated]), vps, 0.5, prior={"Ann": 5.0, "Bea": 5.0}) == [None]


def test_match_prior_ignores_names_without_a_voiceprint():
    vps = {"Ann": (unit(1, 0), 3)}
    v = speakers.normalize(unit(0.2, 1))                                             # Ann ~0.2: below threshold
    assert speakers.match(np.stack([v]), vps, 0.5, prior={"Ann": 9.0, "Casey": 9.0}) == [None]


class FakeCal:
    """Calibrated scores: Ann and Bea each have their own cutoff (the calibrated scale)."""
    names = ["Ann", "Bea", "Cy"]

    def __init__(self, match, threshold):
        self.res = SimpleNamespace(match=np.array(match), raw=np.array(match) / 5, calibrated=np.ones((len(match), 3), bool),
                                   threshold=np.array(threshold))

    def scores(self, centroids, exclude=None, self_ids=None):
        return self.res


def test_match_prior_calibrated_uses_margin_over_own_threshold():
    # Ann 1.9 vs her 1.5 (margin .4); Bea 1.62 vs her 1.2 (margin .42). Bea leads by a hair without a prior.
    cal = FakeCal([[1.9, 1.62, 0.1]], [[1.5, 1.2, 1.5]])
    cents = np.array([[1.0, 0.0]], dtype=np.float32)
    assert speakers.match(cents, {}, 0.5, calibration=cal) == ["Bea"]    # margins: .42 > .4, with or without a prior
    assert speakers.match(cents, {}, 0.5, calibration=cal, prior={}) == ["Bea"]
    assert speakers.match(cents, {}, 0.5, calibration=cal, prior={"Bea": 0.0, "Ann": 0.0}) == ["Bea"]   # margins: .42 > .4
    assert speakers.match(cents, {}, 0.5, calibration=cal, prior={"Ann": 0.05}) == ["Ann"]
    # Cy, hugely close but below her cutoff, is never named (any prior ranks by margin over each cutoff)
    assert speakers.match(cents, {}, 0.5, calibration=cal, prior={"Cy": 50.0}) == ["Bea"]
    below = FakeCal([[1.4, 1.1, 0.1]], [[1.5, 1.2, 1.5]])
    assert speakers.match(cents, {}, 0.5, calibration=below, prior={"Ann": 50.0, "Bea": 50.0}) == [None]


def test_voices_score_prior_reorders_clearing_people_only():
    vps = {"Ann": (unit(1, 0), 3), "Bea": (unit(0, 1), 3)}
    emb = np.stack([unit(0.70, 0.71)])
    assert voices.score(emb, vps, threshold=0.5)[0].best_name == "Bea"
    sc = voices.score(emb, vps, threshold=0.5, prior={"Ann": 0.1})[0]
    assert sc.best_name == "Ann" and sc.matched
    assert sc.similarity == pytest.approx(0.70 / np.hypot(0.70, 0.71), abs=1e-4)   # still the real cosine
    low = np.stack([speakers.normalize(unit(0.3, 0.95))])
    assert voices.score(low, vps, threshold=0.5, prior={"Ann": 9.0})[0].best_name == "Bea"
    nothing = np.stack([speakers.normalize(unit(-1, -1))])
    assert not voices.score(nothing, vps, threshold=0.5, prior={"Ann": 9.0})[0].matched
    assert voices.score(emb, vps, threshold=0.5, prior=None)[0].best_name == "Bea"


def test_voices_score_prior_mixed_scales_ranks_by_own_threshold():
    cal = FakeCal([[1.9, 1.62, 0.1]], [[1.5, 1.2, 1.5]])
    emb = np.array([[1.0, 0.0]], dtype=np.float32)
    assert voices.score(emb, {}, calibration=cal)[0].best_name == "Bea"          # already ranked by margin (.42 > .4)
    assert voices.score(emb, {}, calibration=cal, prior={"Ann": 0.05})[0].best_name == "Ann"
    assert voices.score(emb, {}, calibration=cal, prior={"Cy": 50.0})[0].best_name == "Bea"   # Cy is below her cutoff


# ---------------------------------------------------------------- loading priors

SP = {"people": {"Ann": "e-lex-p", "Bea": "e-lex-w"}, "closeness": {"weight": 0.1, "weight_calendar": 0.1, "weight_age": 0.05}}


def test_load_priors_from_lifeos_links_only():
    p = closeness.load_priors(SP, ["Ann", "Bea", "Cy"], get=fake_lifeos())
    assert set(p) == {"Ann", "Bea"}                                  # Cy has no link: no prior
    assert p["Ann"] == pytest.approx(0.1 * 0.595)
    assert p["Bea"] == pytest.approx(0.1 * 0.247 - 0.05 * 215 / 365)
    assert p["Ann"] > p["Bea"]


def test_load_priors_calendar_bonus():
    people = [{"entity_id": "e-lex-p", "display_name": "Alex", "email": "t@x.example", "relationship_strength": 50,
               "days_since_contact": 0}]
    p = closeness.load_priors({"people": {"Ann": "e-lex-p"}}, ["Ann"], attendees={"t@x.example"}, get=fake_lifeos(people))
    assert p["Ann"] == pytest.approx(0.1 * 0.5 + 0.1)
    p = closeness.load_priors({"people": {"Ann": "e-lex-p"}}, ["Ann"], attendees={"someone@else.example"}, get=fake_lifeos(people))
    assert p["Ann"] == pytest.approx(0.05)


def test_load_priors_lifeos_down_means_no_prior_at_all():
    assert closeness.load_priors(SP, ["Ann", "Bea"], get=down) == {}
    assert closeness.load_priors({}, ["Ann"], get=fake_lifeos()) == {}


def test_live_lifeos_failure_is_remembered_briefly(monkeypatch):
    calls = []
    monkeypatch.setattr(lifeos_people, "_api", lambda path, params, timeout=10.0: calls.append(path) or down(path, params))
    assert closeness.load_priors(SP, ["Ann"]) == {}
    assert closeness.load_priors(SP, ["Ann"]) == {}
    assert len(calls) == 1                                           # the second call didn't wait on LifeOS again


# ---------------------------------------------------------------- the feed

@pytest.fixture()
def srv(tmp_path, monkeypatch):
    notes = tmp_path / "notes"
    notes.mkdir()
    monkeypatch.setenv("PENDANT_NOTES_DIR", str(notes))
    monkeypatch.setenv("PENDANT_DATA", str(tmp_path))
    monkeypatch.setattr(lifeos_people, "_api", lambda path, params, timeout=10.0: fake_crm()(path, params))
    s = feed.make_server(0, "tok")
    threading.Thread(target=s.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{s.server_address[1]}"
    s.shutdown()


def call(url, body=None):
    req = urllib.request.Request(url, headers={"Authorization": "Bearer tok"},
                                 data=None if body is None else json.dumps(body).encode())
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


def test_suggest_endpoint_is_not_a_person_lookup(srv, tmp_path):
    write_cfg(tmp_path, allowed=["Noah", "Alex"], people={"Alex": "e-lex-w"})
    out = call(srv + "/pendant/feed/people/suggest?q=lex")
    assert out["lifeos"] is True
    assert [p["entity_id"] for p in out["people"][:3]] == ["e-lex-w", "e-omara", "e-lex-frag"]
    assert out["people"][0]["label"] == "Alex" and out["people"][0]["name"] == "Alex Rivera"
    assert out["people"][-1]["new"] is True
    rows = call(srv + "/pendant/feed/people/suggest")["people"]
    assert rows[0]["known"] and rows[1]["known"]                          # tagged people first
    assert next(p for p in rows if not p["known"])["circle"] == 0           # then the closest circle


def test_suggest_endpoint_survives_lifeos_down(srv, tmp_path, monkeypatch):
    write_cfg(tmp_path, allowed=["Noah", "Alex"])
    monkeypatch.setattr(lifeos_people, "_api", lambda path, params, timeout=10.0: down(path, params))
    out = call(srv + "/pendant/feed/people/suggest?q=lex")
    assert out["lifeos"] is False and [p["label"] for p in out["people"]] == ["Alex", "Lex"]


def post(url, body):
    req = urllib.request.Request(url, headers={"Authorization": "Bearer tok"}, data=json.dumps(body).encode())
    return json.loads(urllib.request.urlopen(req).read())


def test_relink_endpoint_replaces_the_link_and_nothing_else(srv, tmp_path):
    write_cfg(tmp_path, allowed=["Noah", "Alex"], people={"Alex": "e-lex-frag"}, wearer="Noah")
    vp = tmp_path / "voiceprints"
    vp.mkdir()
    (vp / "Alex.npz").write_bytes(b"voiceprint-bytes")
    out = post(srv + "/pendant/feed/people/alex/link", {"entity_id": "e-lex-w"})
    assert out["name"] == "Alex" and out["lifeos_name"] == "Alex Rivera" and out["lifeos_circle"] == 0
    cfg = json.loads((tmp_path / "config.json").read_text())["speakers"]
    assert cfg["people"] == {"Alex": "e-lex-w"} and cfg["allowed"] == ["Noah", "Alex"]
    assert (vp / "Alex.npz").read_bytes() == b"voiceprint-bytes"


def test_relink_endpoint_refusals(srv, tmp_path):
    write_cfg(tmp_path, allowed=["Noah", "Alex"], people={"Alex": "e-lex-frag"})
    before = (tmp_path / "config.json").read_text()
    for path, body, code in (("/pendant/feed/people/Nobody/link", {"entity_id": "e-lex-w"}, 404),
                             ("/pendant/feed/people/Alex/link", {"entity_id": "e-missing"}, 404),
                             ("/pendant/feed/people/Alex/link", {"entity_id": "../x"}, 400),
                             ("/pendant/feed/people/Alex/link", {}, 400)):
        with pytest.raises(urllib.error.HTTPError) as e:
            post(srv + path, body)
        assert e.value.code == code
    assert (tmp_path / "config.json").read_text() == before


def test_relink_endpoint_needs_lifeos(srv, tmp_path, monkeypatch):
    write_cfg(tmp_path, allowed=["Alex"], people={"Alex": "e-lex-frag"})
    monkeypatch.setattr(lifeos_people, "_api", lambda path, params, timeout=10.0: down(path, params))
    with pytest.raises(urllib.error.HTTPError) as e:
        post(srv + "/pendant/feed/people/Alex/link", {"entity_id": "e-lex-w"})
    assert e.value.code == 502


def test_link_picked_needs_source_user_and_a_valid_id(tmp_path, monkeypatch):
    monkeypatch.setenv("PENDANT_DATA", str(tmp_path))
    write_cfg(tmp_path, allowed=["Noah", "Alex"])
    feed.link_picked("Alex", "e-lex-p", None)               # not Noah's pick
    feed.link_picked("Alex", "../etc", "user")              # not an id
    feed.link_picked("Alex", 7, "user")
    assert "people" not in json.loads((tmp_path / "config.json").read_text())["speakers"]
    feed.link_picked("alex", "e-lex-p", "user")
    cfg = json.loads((tmp_path / "config.json").read_text())["speakers"]
    assert cfg["people"] == {"Alex": "e-lex-p"} and cfg["allowed"] == ["Noah", "Alex"]


def test_people_payload_carries_the_lifeos_link(tmp_path, monkeypatch):
    notes = tmp_path / "notes"
    notes.mkdir()
    monkeypatch.setenv("PENDANT_NOTES_DIR", str(notes))
    monkeypatch.setenv("PENDANT_DATA", str(tmp_path))
    scan = [(__import__("datetime").date.today(), {"time": "09:00", "duration_s": 600},
             {"alex": {"minutes": 5.0, "last": 4.0, "spellings": {"Alex": 3}},
              "riley": {"minutes": 2.0, "last": 1.0, "spellings": {"Riley": 1}}})]
    monkeypatch.setattr(feed, "_people_scan", lambda days: scan)
    monkeypatch.setattr(lifeos_people, "_api", lambda path, params, timeout=10.0: fake_crm()(path, params))
    write_cfg(tmp_path, allowed=["Noah", "Alex"], people={"Alex": "e-lex-w"})
    rows = {r["name"]: r for r in feed.people_payload(7)}
    assert rows["Alex"]["entity_id"] == "e-lex-w" and rows["Alex"]["lifeos_url"].endswith("/crm/e-lex-w")
    assert (rows["Alex"]["lifeos_name"], rows["Alex"]["lifeos_circle"], rows["Alex"]["lifeos_category"]) == \
        ("Alex Rivera", 0, "family")
    assert "entity_id" not in rows["Riley"] and "lifeos_name" not in rows["Riley"]
    monkeypatch.setattr(lifeos_people, "_cache", {})
    monkeypatch.setattr(lifeos_people, "_api", lambda path, params, timeout=10.0: down(path, params))
    rows = {r["name"]: r for r in feed.people_payload(7)}               # LifeOS down: still linked, no details
    assert rows["Alex"]["entity_id"] == "e-lex-w" and "lifeos_name" not in rows["Alex"]


# ---------------------------------------------------------------- review round 1

def two_alexes():
    return [crm("e-lex-p", "Alex", 4, 59.5),
            crm("e-lex-w", "Alex", 5, 24.7, company="Studio Nine"),
            crm("e-lex-x", "Alex Xu", 5, 10.0)]


def test_same_name_other_person_gets_a_distinct_speaker_name():
    out = lifeos_people.suggest("alex", [{"name": "Alex"}], ["Alex"], {"Alex": "e-lex-p"},
                                get=fake_crm(two_alexes()))
    by_id = {p["entity_id"]: p for p in out["people"] if p["entity_id"]}
    assert by_id["e-lex-p"]["label"] == "Alex"
    assert by_id["e-lex-x"]["label"] == "Alex Xu"
    assert by_id["e-lex-w"]["label"] == "Alex S"                       # company initial
    assert by_id["e-lex-w"]["name"] == "Alex" and by_id["e-lex-w"]["detail"] == "Studio Nine"
    assert len({p["label"].lower() for p in out["people"]}) == len(out["people"])


def test_picking_the_other_alex_never_relinks_the_first(tmp_path, monkeypatch):
    monkeypatch.setenv("PENDANT_DATA", str(tmp_path))
    write_cfg(tmp_path, allowed=["Noah", "Alex"], people={"Alex": "e-lex-p"})
    vp = tmp_path / "voiceprints"
    vp.mkdir()
    (vp / "Alex.npz").write_bytes(b"voiceprint-bytes")
    before = (tmp_path / "config.json").read_text()
    ran = []
    with pytest.raises(feed.HTTPError) as e:        # the same name, a different person: refused
        feed.with_link(lambda: ran.append(1), "Alex", "e-lex-w", "user")
    assert e.value.code == 409 and not ran          # and nothing else happened
    assert (tmp_path / "config.json").read_text() == before
    feed.with_link(lambda: ran.append(1), "Alex", "e-lex-p", "user")      # re-picking the same person is fine
    sug = lifeos_people.suggest("alex", [{"name": "Alex"}], ["Alex"], {"Alex": "e-lex-p"},
                                get=fake_crm(two_alexes()))
    other = next(p for p in sug["people"] if p["entity_id"] == "e-lex-w")["label"]
    feed.with_link(lambda: ran.append(2), other, "e-lex-w", "user")
    cfg = json.loads((tmp_path / "config.json").read_text())["speakers"]
    assert cfg["people"] == {"Alex": "e-lex-p", other: "e-lex-w"}         # a separate speaker
    assert (vp / "Alex.npz").read_bytes() == b"voiceprint-bytes"


def test_config_link_conflict_needs_an_explicit_relink(tmp_path):
    write_cfg(tmp_path, allowed=["Alex"], people={"Alex": "e-1"})
    with pytest.raises(config.LinkConflict):
        config.link_people(tmp_path, {"Alex": "e-2"})
    config.link_people(tmp_path, {"Alex": "e-2"}, replace=True)
    assert json.loads((tmp_path / "config.json").read_text())["speakers"]["people"] == {"Alex": "e-2"}


def test_label_endpoint_answers_409_and_changes_nothing(srv, tmp_path):
    write_cfg(tmp_path, allowed=["Noah", "Alex"], people={"Alex": "e-lex-p"})
    before = (tmp_path / "config.json").read_text()
    req = urllib.request.Request(srv + "/pendant/feed/speakers/1/label", headers={"Authorization": "Bearer tok"},
                                 data=json.dumps({"name": "Alex", "source": "user", "entity_id": "e-lex-w"}).encode())
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req)
    assert e.value.code == 409
    assert (tmp_path / "config.json").read_text() == before


# a LifeOS that hangs: the whole run's added time stays inside the budget

def slow_lifeos(delay, calls):
    import time
    inner = fake_lifeos()

    def get(path, params):
        calls.append(path)
        time.sleep(delay)
        return inner(path, params)
    return get


def test_hung_lifeos_adds_no_more_than_the_budget_per_run():
    import time
    calls = []
    sp = {"people": {f"P{i}": "e-lex-p" for i in range(6)}}
    run = closeness.PriorRun(budget=0.4, get=slow_lifeos(5.0, calls))
    t0 = time.monotonic()
    for k in range(25):                                  # 25 conversations, 6 linked names
        out = run.priors(sp, [f"P{i}" for i in range(6)], span=(1_794_000_000_000 + k * 3_600_000, 1_794_000_100_000 + k * 3_600_000))
        assert out == {}                                 # a timeout means no prior at all
    assert time.monotonic() - t0 < 0.4 + 0.3
    assert len(calls) <= 1                               # the rest never even called LifeOS


def test_slow_calendar_after_people_stops_the_run_and_no_prior_applies():
    import time
    people = fake_lifeos()
    calls = []

    def get(path, params):
        calls.append(path)
        if path.startswith("/api/calendar"):
            time.sleep(5)
        return people(path, params)
    sp = {"people": {"Ann": "e-lex-p"}}
    run = closeness.PriorRun(budget=0.5, get=get)
    t0 = time.monotonic()
    assert run.priors(sp, ["Ann"]) != {}                 # people load fine without a calendar span
    for k in range(10):
        assert run.priors(sp, ["Ann"], span=(1_794_000_000_000, 1_794_000_100_000)) == {}
    assert time.monotonic() - t0 < 0.5 + 0.3
    assert sum(c.startswith("/api/calendar") for c in calls) <= 1


def test_people_are_fetched_once_per_run_in_one_pass_and_the_calendar_once_per_day():
    calls = []
    inner = fake_lifeos(calls=calls)
    run = closeness.PriorRun(budget=2.0, get=inner)
    sp = {"people": {"Ann": "e-lex-p", "Bea": "e-lex-w"}}
    day = 1_794_000_000_000
    for k in range(5):
        assert set(run.priors(sp, ["Ann", "Bea"], span=(day + k, day + k + 1000))) == {"Ann", "Bea"}
    assert [c[0] for c in calls].count("/api/people/entity/e-lex-p") == 1
    assert sum(1 for c in calls if c[0].startswith("/api/calendar")) <= 2     # work + personal, once


def test_failure_backoff_is_checked_before_the_calendar(monkeypatch):
    import time
    calls = []
    monkeypatch.setattr(closeness, "_down_until", time.time() + 30)
    monkeypatch.setattr(lifeos_people, "_api", lambda *a, **k: calls.append(a))
    run = closeness.PriorRun()
    assert run.priors({"people": {"Ann": "e-lex-p"}}, ["Ann"], span=(1_794_000_000_000, 1_794_000_100_000)) == {}
    assert calls == []
