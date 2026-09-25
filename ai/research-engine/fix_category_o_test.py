path = "tests/test_global_opportunity_scheduler.py"
with open(path, "rb") as f:
    content = f.read().decode("utf-8")

old = (
    "    # Immediately afterward, a normal scheduled poll must not duplicate it.\r\n"
    "    assert sched._maybe_submit() is False\r\n"
    "    assert len(worker.submissions) == 1\r\n"
    "\r\n"
    "    # An hour later, the ordinary hourly schedule is due again, unaffected.\r\n"
    "    now[\"t\"] = now[\"t\"] + timedelta(hours=1, minutes=1)\r\n"
    "    assert sched._maybe_submit() is True\r\n"
    "    assert len(worker.submissions) == 2\r\n"
)
new = (
    "    # Immediately afterward, a normal scheduled poll must not duplicate it.\r\n"
    "    assert sched._maybe_submit() is False\r\n"
    "    assert len(worker.submissions) == 1\r\n"
    "\r\n"
    "    # Simulate the bootstrap cycle completing (the real OpportunityCycleWorker\r\n"
    "    # clears .active on COMPLETED/FAILED; FakeWorker leaves it set until the\r\n"
    "    # caller does, matching how the other overlap tests in this file drive it).\r\n"
    "    worker.active = None\r\n"
    "    # An hour later, the ordinary hourly schedule is due again, unaffected.\r\n"
    "    now[\"t\"] = now[\"t\"] + timedelta(hours=1, minutes=1)\r\n"
    "    assert sched._maybe_submit() is True\r\n"
    "    assert len(worker.submissions) == 2\r\n"
)
assert content.count(old) == 1, f"anchor count={content.count(old)}"
content = content.replace(old, new, 1)
with open(path, "wb") as f:
    f.write(content.encode("utf-8"))
print("OK")
