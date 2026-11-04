import datetime as dt

import pytest

from routec import lease


def at(y, mo, d, h, mi):
    return dt.datetime(y, mo, d, h, mi).timestamp()


def test_take_require_release(cfg):
    f = cfg["lease_file"]
    lease.take(f, "sweep", 10)
    assert lease.require(f, "sweep")["owner"] == "sweep"
    with pytest.raises(lease.LeaseError):
        lease.require(f, "other")
    with pytest.raises(lease.LeaseError):
        lease.take(f, "other", 5)
    with pytest.raises(lease.LeaseError):
        lease.release(f, "other")
    lease.release(f, "sweep")
    with pytest.raises(lease.LeaseError):
        lease.require(f, "sweep")


def test_anonymous_refused(cfg):
    with pytest.raises(lease.LeaseError):
        lease.take(cfg["lease_file"], "anonymous")


def test_unattended_weekday_cap(cfg):
    f = cfg["lease_file"]
    now = at(2026, 11, 4, 6, 30)            # Wednesday 06:30
    rec = lease.take(f, "night", 90, unattended=True, now=now)
    assert rec["until"] == at(2026, 11, 4, 7, 0)
    assert "capped" in rec["note"]
    lease.release(f, "night")
    with pytest.raises(lease.LeaseError):
        lease.take(f, "night", 30, unattended=True, now=at(2026, 11, 4, 6, 59, ) + 45)


def test_unattended_weekend_not_capped(cfg):
    now = at(2026, 11, 7, 6, 30)            # Saturday
    rec = lease.take(cfg["lease_file"], "night", 90, unattended=True, now=now)
    assert rec["until"] == now + 90 * 60


def test_compatible_with_kvm_native_format(cfg):
    import json, time
    open(cfg["lease_file"], "w").write(json.dumps({"owner": "eugene-console", "until": time.time() + 60,
                                                   "taken": "2026-11-04T13:00"}))
    with pytest.raises(lease.LeaseError) as e:
        lease.require(cfg["lease_file"], "claude")
    assert "eugene-console" in str(e.value)
