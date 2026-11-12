"""Jev (TypeSafe System One) checks: calibrated yes/no judgments.

Used to keep only action items that were explicitly stated as tasks. The
summarizer proposes candidates; Jev answers, per candidate, whether someone in
the transcript actually said it should be done (a commitment, a request, or a
reminder request) rather than it being implied. One request per conversation.

Key: env TYPESAFE_API_KEY, else the TYPESAFE_API_KEY line in PENDANT_ENV_FILE
(default ~/.config/pendant-direct/.env). Without a key, nothing is filtered and no
transcript text goes to TypeSafe. Failures keep the candidates (never block).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

API_URL = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-latest"
ENV_FILE = Path(os.environ.get("PENDANT_ENV_FILE") or Path.home() / ".config" / "pendant-direct" / ".env").expanduser()
EXPLICIT_MIN = 0.6
FOLLOWUP_MIN = 0.6

INSTRUCTIONS = (
    "In `transcript`, did someone explicitly say that the task `items[{i}]` should be done? "
    "Count it only if it was stated outright: {W} saying he will or needs to do it, someone "
    "asking him to do it, or him asking to be reminded. A task that is merely implied by the "
    "topic, suggested as a maybe, or already done does not count.")
CRITERIA = {
    "true": "The transcript contains an explicit statement or request that this task be done",
    "false": "The task is implied, inferred, hypothetical, already done, or not in the transcript",
}


FOLLOWUP_INSTRUCTIONS = (
    "`transcript` is everyday talk recorded by {W}'s wearable microphone. Is `items[{i}]` a "
    "real follow-up that {W} still has to do later, after this conversation? Requests handled "
    "on the spot (pass the salt, grab something, come here, quick favours) are not follow-ups. "
    "Neither are instructions {W} is dictating to an AI assistant, coding agent or computer "
    "while working, unless he asks to be reminded or to add it as his own task.")
FOLLOWUP_CRITERIA = {
    "true": "A task {W} has to do later, beyond this conversation",
    "false": "Handled in the moment, a trivial immediate ask, or an instruction to an assistant or device",
}


def api_key() -> str | None:
    if os.environ.get("TYPESAFE_API_KEY"):
        return os.environ["TYPESAFE_API_KEY"]
    try:
        for line in ENV_FILE.read_text().splitlines():
            if line.startswith("TYPESAFE_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'") or None
    except OSError:
        pass
    return None


def _post(url: str, headers: dict, body: dict) -> dict:
    import urllib.request
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def item_probabilities(transcript: str, items: list[str], post=None, key=None,
                      wearer: str = "the wearer") -> list[tuple[float, float]]:
    """[(p explicitly stated, p a real later follow-up)] per item, from one request.
    Raises on any problem."""
    key = key or api_key()
    if not key:
        raise RuntimeError("no TypeSafe key")
    questions = {}
    for i in range(len(items)):
        questions[f"item{i}"] = {"type": "noul", "instructions": INSTRUCTIONS.format(i=i, W=wearer), "criteria": CRITERIA}
        questions[f"later{i}"] = {"type": "noul", "instructions": FOLLOWUP_INSTRUCTIONS.format(i=i, W=wearer),
                                  "criteria": {k: v.replace("{W}", wearer) for k, v in FOLLOWUP_CRITERIA.items()}}
    body = {"model": MODEL, "state": {"transcript": transcript, "items": items}, "questions": questions}
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    answers = (post or _post)(API_URL, headers, body)["answers"]
    return [(float(answers[f"item{i}"]["noul"]), float(answers[f"later{i}"]["noul"])) for i in range(len(items))]


def explicit_probabilities(transcript: str, items: list[str], post=None, key=None,
                           wearer: str = "the wearer") -> list[float]:
    """p(explicitly stated) per item. Raises on any problem."""
    return [e for e, _ in item_probabilities(transcript, items, post=post, key=key, wearer=wearer)]


def keep_explicit(transcript: str, items: list[str], log=print, post=None, key=None,
                  threshold: float = EXPLICIT_MIN, followup: float = FOLLOWUP_MIN,
                  wearer: str = "the wearer") -> list[str]:
    """Items stated outright AND real work for later (not an in-the-moment ask or an
    instruction to an assistant)."""
    if not items or not (key or api_key()):
        return items
    try:
        probs = item_probabilities(transcript, items, post=post, key=key, wearer=wearer)
    except Exception as e:  # never block transcription
        log(f"jev check failed ({type(e).__name__}); keeping {len(items)} item(s)")
        return items
    kept = [it for it, (p, q) in zip(items, probs) if p >= threshold and q >= followup]
    if len(kept) < len(items):
        log(f"jev kept {len(kept)} of {len(items)} action item(s)")
    return kept


ASK_INSTRUCTIONS = (
    "In `transcript`, did the wearer explicitly ask to add something to his task or to-do list "
    "here, specifically `item`? Count it only if he told someone or the device to add it, make it a "
    "task, or remind him. Merely mentioning his list, complaining about it, or talking about tasks "
    "in general does not count.")
ASK_CRITERIA = {
    "true": "The wearer explicitly asked to add this item to his task list or to be reminded of it",
    "false": "The wearer only mentioned a list or task, or did not ask for this item to be added",
}


def asked_to_add(span: str, item: str, post=None, key=None) -> float:
    """p(the wearer explicitly asked to add `item` to his task list). Raises on any problem."""
    key = key or api_key()
    if not key:
        raise RuntimeError("no TypeSafe key")
    body = {"model": MODEL, "state": {"transcript": span, "item": item},
            "questions": {"ask": {"type": "noul", "instructions": ASK_INSTRUCTIONS,
                                  "criteria": ASK_CRITERIA}}}
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    return float((post or _post)(API_URL, headers, body)["answers"]["ask"]["noul"])
