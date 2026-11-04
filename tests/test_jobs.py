import json
import threading

import pytest

from routec.jobs import JobManager


def test_idempotent_by_id(cfg):
    calls = []

    def run(job, cancel):
        calls.append(job["id"])
        return {"ok": 1}

    jm = JobManager(cfg, run)
    j, existing = jm.submit("click", {"x": 1, "y": 2}, "me", "abc")
    assert not existing
    assert jm.wait("abc", 5)["status"] == "done"
    j2, existing = jm.submit("click", {"x": 1, "y": 2}, "me", "abc")
    assert existing and j2["status"] == "done"
    assert calls == ["abc"]
    with pytest.raises(ValueError):
        jm.submit("type", {"text": "x"}, "me", "abc")


def test_failure_marks_may_have_executed(cfg):
    def run(job, cancel):
        raise RuntimeError("serial went away")

    jm = JobManager(cfg, run)
    j, _ = jm.submit("type", {"text": "x"}, "me")
    j = jm.wait(j["id"], 5)
    assert j["status"] == "failed" and j["may_have_executed"] is True


def test_refused_is_not_executed(cfg):
    class E(RuntimeError):
        refused = True
        may_have_executed = False
        code = "lease"

    def run(job, cancel):
        raise E("no lease")

    jm = JobManager(cfg, run)
    j, _ = jm.submit("click", {}, "me")
    j = jm.wait(j["id"], 5)
    assert j["status"] == "refused" and j["may_have_executed"] is False and j["error_code"] == "lease"


def test_restart_marks_running_as_interrupted(cfg):
    import os
    os.makedirs(cfg["state_dir"], exist_ok=True)
    path = os.path.join(cfg["state_dir"], "journal.jsonl")
    with open(path, "w") as f:
        f.write(json.dumps({"id": "j1", "kind": "type", "params": {}, "owner": "me", "status": "running",
                            "created": 1, "started": 2}) + "\n")
    jm = JobManager(cfg, lambda j, c: None)
    j = jm.get("j1")
    assert j["status"] == "interrupted" and j["may_have_executed"] is True
