from pendant_direct import jev


def test_keeps_only_explicit_items():
    sent = {}

    def post(url, headers, body):
        sent.update(body=body, auth=headers["Authorization"])
        return {"answers": {"item0": {"type": "noul", "noul": 0.93}, "item1": {"type": "noul", "noul": 0.12},
                            "later0": {"type": "noul", "noul": 0.9}, "later1": {"type": "noul", "noul": 0.9}}}
    kept = jev.keep_explicit("I'll email Sam. We could maybe paint.", ["Email Sam", "Paint the room"],
                             post=post, key="k", log=lambda m: None)
    assert kept == ["Email Sam"]
    assert sent["auth"] == "Bearer k"
    assert sent["body"]["model"] == "jev-latest"
    q = sent["body"]["questions"]["item1"]
    assert q["type"] == "noul" and "`items[1]`" in q["instructions"]
    assert sent["body"]["state"]["items"] == ["Email Sam", "Paint the room"]


def test_no_key_or_no_items_is_a_no_op(monkeypatch):
    monkeypatch.setattr(jev, "api_key", lambda: None)
    assert jev.keep_explicit("t", ["a"]) == ["a"]
    assert jev.keep_explicit("t", [], key="k") == []


def test_failure_keeps_items():
    def boom(*a):
        raise OSError("down")
    logs = []
    assert jev.keep_explicit("t", ["a"], post=boom, key="k", log=logs.append) == ["a"]
    assert "failed" in logs[0]


def test_drops_in_the_moment_asks_and_assistant_instructions():
    sent = {}

    def post(url, headers, body):
        sent.update(body=body)
        # all three were said outright; only the first is work for later
        return {"answers": {"item0": {"noul": 0.95}, "later0": {"noul": 0.88},
                            "item1": {"noul": 0.97}, "later1": {"noul": 0.04},
                            "item2": {"noul": 0.92}, "later2": {"noul": 0.10}}}
    items = ["Reply to First Service", "Pass the salt", "Use Opus for the lead orchestrators"]
    kept = jev.keep_explicit("t", items, post=post, key="k", log=lambda m: None)
    assert kept == ["Reply to First Service"]
    q = sent["body"]["questions"]
    assert set(q) == {"item0", "item1", "item2", "later0", "later1", "later2"}   # one request
    assert "`items[2]`" in q["later2"]["instructions"] and "later" in q["later2"]["instructions"]


def test_summarizer_prompt_excludes_trivial_asks_and_agent_dictation():
    from pendant_direct import summarize
    assert "pass the salt" in summarize.SYSTEM and "AI assistant" in summarize.SYSTEM
