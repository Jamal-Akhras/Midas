"""`midas monitor --loop` keeps going when one pass raises, and stops cleanly
on Ctrl-C. A month-long paper run must not end because one network call
failed at the top of a pass."""

import logging

from midas.scheduler import monitor


def test_loop_survives_a_failing_pass(monkeypatch, caplog):
    calls: list[int | None] = []

    def fake_pass(session_factory, router, interval_minutes=None):
        calls.append(interval_minutes)
        if len(calls) == 1:
            raise RuntimeError("alpaca clock unreachable")
        return {"ok": True}

    monkeypatch.setattr(monitor, "run_monitor_once", fake_pass)

    sleeps: list[float] = []

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 2:
            raise KeyboardInterrupt

    seen: list[dict] = []
    with caplog.at_level(logging.ERROR):
        monitor.run_monitor_forever(None, None, 15, sleep=fake_sleep, on_summary=seen.append)

    assert calls == [15, 15]  # the failed pass did not end the loop
    assert seen == [{"ok": True}]  # only the successful pass produced a summary
    assert sleeps == [900.0, 900.0]
    assert "monitor pass failed" in caplog.text
    assert "alpaca clock unreachable" in caplog.text


def test_interrupt_during_a_pass_stops_the_loop(monkeypatch):
    def interrupted(session_factory, router, interval_minutes=None):
        raise KeyboardInterrupt

    monkeypatch.setattr(monitor, "run_monitor_once", interrupted)
    slept: list[float] = []
    monitor.run_monitor_forever(None, None, 5, sleep=slept.append)
    assert slept == []  # returned straight away, no sleep
