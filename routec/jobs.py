"""Input jobs: queued, run one at a time, journalled, idempotent.

This is the structural fix for Route C's one rule ("an error does not mean it did not happen").
Every input request is a job with an id. It runs inside the daemon to completion whether or not
the caller is still listening, so a transport timeout cannot cut it off half way. Asking again
with the same id returns the same job; it never runs twice. The journal survives a daemon
restart, and a job that was running when the daemon died is reported as 'interrupted' with
may_have_executed = true.
"""
import collections
import json
import os
import queue
import threading
import time
import uuid

DONE = ("done", "failed", "refused", "interrupted", "cancelled")


class Job(dict):
    pass


class JobManager:
    def __init__(self, cfg, runner, log=None, keep=500):
        self.cfg = cfg
        self.runner = runner
        self.log = log or (lambda *a: None)
        self.path = os.path.join(cfg["state_dir"], "journal.jsonl")
        os.makedirs(cfg["state_dir"], exist_ok=True)
        self.jobs = collections.OrderedDict()
        self.keep = keep
        self._lock = threading.Condition()
        self._q = queue.Queue()
        self._cancel = {}
        self._load()
        self._worker = threading.Thread(target=self._work, name="jobs", daemon=True)
        self._worker.start()

    # ── journal ─────────────────────────────────────────────────────────────────────────────
    def _load(self):
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path) as fh:
                for line in fh:
                    try:
                        j = Job(json.loads(line))
                        self.jobs[j["id"]] = j
                    except ValueError:
                        continue
        except OSError:
            return
        for j in self.jobs.values():
            if j["status"] in ("queued", "running"):
                j["status"] = "interrupted"
                j["may_have_executed"] = j.get("started") is not None
                j["error"] = "the Route C daemon stopped while this job was %s" % (
                    "running: it may have partly executed; read the screen" if j["may_have_executed"] else "queued")
        while len(self.jobs) > self.keep:
            self.jobs.popitem(last=False)
        self._rewrite()

    def _rewrite(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w") as fh:
            for j in self.jobs.values():
                fh.write(json.dumps(j) + "\n")
        os.replace(tmp, self.path)

    def _record(self, job):
        with open(self.path, "a") as fh:
            fh.write(json.dumps(job) + "\n")

    # ── public ──────────────────────────────────────────────────────────────────────────────
    def submit(self, kind, params, owner, job_id=None):
        """Returns (job, existing). An id seen before returns that job and runs nothing."""
        job_id = job_id or uuid.uuid4().hex[:12]
        with self._lock:
            if job_id in self.jobs:
                old = self.jobs[job_id]
                if old.get("kind") != kind or old.get("params") != params:
                    raise ValueError("job id %r was already used for a different request (%s); nothing was run"
                                     % (job_id, old.get("kind")))
                return old, True
            job = Job(id=job_id, kind=kind, params=params, owner=owner, status="queued",
                      created=time.time(), started=None, finished=None, result=None, error=None,
                      error_code=None, progress=None, may_have_executed=False)
            self.jobs[job_id] = job
            while len(self.jobs) > self.keep:
                self.jobs.popitem(last=False)
            self._cancel[job_id] = threading.Event()
            self._record(job)
        self._q.put(job_id)
        return job, False

    def get(self, job_id):
        return self.jobs.get(job_id)

    def wait(self, job_id, timeout):
        deadline = time.time() + max(0.0, float(timeout))
        with self._lock:
            while True:
                j = self.jobs.get(job_id)
                if j is None or j["status"] in DONE:
                    return j
                left = deadline - time.time()
                if left <= 0:
                    return j
                self._lock.wait(min(left, 1.0))

    def cancel(self, job_id):
        ev = self._cancel.get(job_id)
        if ev:
            ev.set()
        return self.jobs.get(job_id)

    def recent(self, limit=20):
        return list(self.jobs.values())[-limit:]

    def progress(self, job, value):
        with self._lock:
            job["progress"] = value
            self._lock.notify_all()

    def busy(self):
        return any(j["status"] in ("queued", "running") for j in self.jobs.values())

    # ── worker ──────────────────────────────────────────────────────────────────────────────
    def _work(self):
        while True:
            job_id = self._q.get()
            job = self.jobs.get(job_id)
            if job is None:
                continue
            cancel = self._cancel.get(job_id) or threading.Event()
            if cancel.is_set():
                self._finish(job, "cancelled", error="cancelled before it started")
                continue
            with self._lock:
                job["status"] = "running"
                job["started"] = time.time()
                self._record(job)
                self._lock.notify_all()
            try:
                result = self.runner(job, cancel)
                self._finish(job, "done", result=result)
            except Exception as e:
                code = getattr(e, "code", None) or type(e).__name__
                maybe = getattr(e, "may_have_executed", None)
                status = "refused" if getattr(e, "refused", False) else "failed"
                self._finish(job, status, error=str(e), code=code, result=getattr(e, "partial", None),
                             maybe=maybe)
            finally:
                self._cancel.pop(job_id, None)

    def _finish(self, job, status, result=None, error=None, code=None, maybe=None):
        with self._lock:
            job["status"] = status
            job["finished"] = time.time()
            job["result"] = result
            job["error"] = error
            job["error_code"] = code
            if maybe is not None:
                job["may_have_executed"] = bool(maybe)
            elif status == "failed":
                job["may_have_executed"] = True       # unless the runner proved otherwise
            self._record(job)
            self._lock.notify_all()
        self.log("job %s %s %s%s" % (job["id"], job["kind"], status, (": " + error) if error else ""))
