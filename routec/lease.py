"""The Route C input lease.

Same file and JSON shape as kvm_native.py (and the KVM console, which holds it as 'eugene-console'),
so all three respect one another: {"owner", "until", "taken"}. Route C adds "unattended": an
unattended lease taken on a weekday before the cutoff (07:00) ends at the cutoff, whatever was
asked for; input after that is refused because the lease has lapsed.
"""
import datetime as dt
import fcntl
import json
import os
import time


class LeaseError(RuntimeError):
    pass


def _cutoff_today(now, cutoff):
    h, m = (int(v) for v in cutoff.split(":"))
    d = dt.datetime.fromtimestamp(now)
    return d.replace(hour=h, minute=m, second=0, microsecond=0).timestamp(), d.weekday() < 5


def state(path, now=None):
    now = now or time.time()
    try:
        st = json.loads(open(path).read())
    except (OSError, ValueError):
        return None
    if now > st.get("until", 0):
        return None
    return st


def describe(st):
    if not st:
        return "free"
    return "held by %r until %s%s" % (st["owner"], time.strftime("%H:%M", time.localtime(st["until"])),
                                      " (unattended)" if st.get("unattended") else "")


class _Locked:
    def __init__(self, path):
        self.path = path + ".lock"

    def __enter__(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self.fh = open(self.path, "a")
        fcntl.flock(self.fh, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        fcntl.flock(self.fh, fcntl.LOCK_UN)
        self.fh.close()


def take(path, owner, minutes=15, unattended=False, cutoff="07:00", now=None):
    if not owner or owner == "anonymous":
        raise LeaseError("name an owner (a slug such as 'inbox-sweep'); anonymous leases are not allowed")
    now = now or time.time()
    with _Locked(path):
        st = state(path, now)
        if st and st["owner"] != owner:
            raise LeaseError("Route C input lease %s; not touching the laptop" % describe(st))
        until = now + float(minutes) * 60
        note = None
        if unattended:
            cut, weekday = _cutoff_today(now, cutoff)
            if weekday and now < cut:
                if until > cut:
                    note = "unattended lease capped at %s (weekday cutoff)" % cutoff
                until = min(until, cut)
                if until - now < 30:
                    raise LeaseError("it is too close to the %s weekday cutoff to start unattended input" % cutoff)
        rec = {"owner": owner, "until": until, "taken": time.strftime("%Y-%m-%dT%H:%M", time.localtime(now)),
               "unattended": bool(unattended), "by": "routec"}
        tmp = path + ".tmp"
        with open(tmp, "w") as fh:
            fh.write(json.dumps(rec))
        os.replace(tmp, path)
    rec["note"] = note
    rec["until_hm"] = time.strftime("%H:%M", time.localtime(until))
    return rec


def release(path, owner):
    with _Locked(path):
        st = state(path)
        if st and st["owner"] != owner:
            raise LeaseError("the lease belongs to %r, not %r" % (st["owner"], owner))
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
    return "released"


def require(path, owner, now=None):
    """Input is allowed only for the current lease holder."""
    st = state(path, now)
    if not st:
        raise LeaseError("no input lease is held. Take it first: routec lease <owner> [minutes] "
                         "(and release it when finished)")
    if st["owner"] != owner:
        raise LeaseError("Route C input lease %s; refusing input from %r" % (describe(st), owner))
    return st
