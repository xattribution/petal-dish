"""Tests of the SQLite store (ARCHITECTURE.md section 7, ``store.py``).

Every test uses its own temporary data directory, runs the store on the test's event
loop and checks that the worker thread is gone afterwards.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from collections.abc import AsyncIterator
from pathlib import Path

import numpy as np
import pytest
import pytest_asyncio

from petal_locate.server.store import (
    DB_FILE_NAME,
    Store,
    StoreError,
    store_threads,
)

T0 = 1_794_528_000_000_000_000


@pytest_asyncio.fixture
async def store(tmp_path: Path) -> AsyncIterator[Store]:
    s = Store(tmp_path / "data", capture_limit_bytes=2500)
    await s.open()
    try:
        yield s
    finally:
        await s.close()
    assert store_threads() == []


def _event(**over: object) -> dict[str, object]:
    base: dict[str, object] = dict(
        event_id="EV-000001",
        seq=1,
        created_ns=T0,
        updated_ns=T0,
        state="detected",
        verdict="undetermined",
        first_seen_ns=T0,
        last_seen_ns=T0,
        uplink_lo_hz=299.995e6,
        uplink_hi_hz=300.005e6,
        doc=json.dumps({"id": "EV-000001"}),
    )
    base.update(over)
    return base


async def test_open_creates_a_wal_database_and_registers_the_run(tmp_path: Path) -> None:
    s = Store(tmp_path / "nested" / "data")
    assert not s.is_open
    await s.open()
    try:
        assert s.is_open and s.path == tmp_path / "nested" / "data" / DB_FILE_NAME and s.path.exists()
        assert s.previous_run_id is None
        mode = await s.query("SELECT value FROM meta WHERE key = 'schema_version'")
        assert mode == [("1",)]
        runs = await s.query("SELECT run_id, stopped_ns FROM runs")
        assert runs == [(s.run_id, None)]
    finally:
        await s.close()
    # Independent check with a second connection: WAL journal, run closed.
    conn = sqlite3.connect(tmp_path / "nested" / "data" / DB_FILE_NAME)
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("SELECT stopped_ns IS NOT NULL FROM runs").fetchone()[0] == 1
    finally:
        conn.close()
    assert store_threads() == []


async def test_config_versions_round_trip_with_their_source(store: Store) -> None:
    assert await store.latest_config() is None
    v1 = await store.save_config("name: a\n", "yaml", "file")
    v2 = await store.save_config('{"name": "b"}', "json", "api")
    assert (v1, v2) == (1, 2)
    latest = await store.latest_config()
    assert latest is not None and latest.version == 2 and latest.source == "api" and latest.fmt == "json"
    assert latest.text == '{"name": "b"}' and latest.run_id == store.run_id
    first = await store.config_version(1)
    assert first is not None and first.text == "name: a\n" and first.source == "file"
    assert await store.config_version(9) is None
    with pytest.raises(ValueError):
        await store.save_config("x", "toml", "file")
    with pytest.raises(ValueError):
        await store.save_config("x", "yaml", "operator")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        await store.save_config("", "yaml", "file")


async def test_scenario_versions_round_trip(store: Store) -> None:
    assert await store.latest_scenario() is None
    assert await store.save_scenario("name: s1\n", "s1", "file") == 1
    assert await store.save_scenario("name: s1\n", "s1", "config") == 2
    latest = await store.latest_scenario()
    assert latest is not None and (latest.version, latest.source, latest.name) == (2, "config", "s1")


async def test_events_and_their_history_round_trip(store: Store) -> None:
    store.put_event(**_event())  # type: ignore[arg-type]
    store.put_event(**_event(state="lost", updated_ns=T0 + 5, doc='{"id": "EV-000001", "v": 2}'))  # type: ignore[arg-type]
    store.put_event(**_event(event_id="EV-000002", seq=2, uplink_lo_hz=None, uplink_hi_hz=None, updated_ns=T0 + 9))  # type: ignore[arg-type]
    store.add_transitions("EV-000001", [(T0, "detected", "undetermined", "detected"), (T0 + 5, "lost", "undetermined", None)])
    store.add_evidence_snapshot("EV-000001", T0, '[{"channel_id": "a"}]')
    store.add_measurement("EV-000001", 1, T0, "tdoa", True, '{"kind": "tdoa"}')
    store.add_solution("EV-000001", T0, True, '{"lat_deg": 1.0}')
    store.add_calibration(
        t_ns=T0,
        carrier_id="ref-1",
        kind="fdoa",
        a_sat_id="sat-a",
        a_site_id="alpha",
        b_sat_id="sat-b",
        b_site_id="alpha",
        correction=0.25,
        sigma=0.01,
        detail={"age_s": 3},
    )
    events = await store.load_events()
    assert [e.event_id for e in events] == ["EV-000002", "EV-000001"]  # newest update first
    first = events[1]
    assert first.state == "lost" and json.loads(first.doc)["v"] == 2 and first.run_id == store.run_id
    assert first.created_ns == T0  # the update keeps the creation time
    assert await store.max_event_seq() == 2
    assert await store.event_transitions("EV-000001") == [
        (T0, "detected", "undetermined", "detected"),
        (T0 + 5, "lost", "undetermined", None),
    ]
    for table, n in (("evidence_snapshots", 1), ("measurements", 1), ("solutions", 1), ("calibrations", 1)):
        assert await store.count_rows(table) == n
    assert await store.count_rows("measurements", "EV-000002") == 0
    rows = await store.query("SELECT uplink_lo_hz FROM events WHERE event_id = ?", ["EV-000002"])
    assert rows == [(None,)]
    with pytest.raises(ValueError):
        await store.count_rows("sqlite_master")
    with pytest.raises(ValueError):
        await store.query("DELETE FROM events")
    with pytest.raises(ValueError):
        store.add_calibration(t_ns=T0, carrier_id="r", kind="x", a_sat_id="a", a_site_id="b", b_sat_id="c",
                              b_site_id="d", correction=0.0, sigma=1.0)
    with pytest.raises(ValueError):
        store.put_event(**_event(uplink_lo_hz=float("nan")))  # type: ignore[arg-type]


async def test_a_failing_fire_and_forget_write_is_logged_and_counted(store: Store, caplog: pytest.LogCaptureFixture) -> None:
    future = store.put_event(**_event(state="exploded"))  # violates the CHECK constraint  # type: ignore[arg-type]
    with pytest.raises(StoreError, match="put_event"):
        await future
    await asyncio.sleep(0)
    assert store.counters()["put_event_failed"] == 1
    assert any("put_event failed" in r.getMessage() for r in caplog.records)
    # The store keeps working afterwards.
    await store.put_event(**_event())  # type: ignore[arg-type]
    assert await store.count_rows("events") == 1


async def test_a_stalled_disk_cannot_queue_writes_without_bound(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    import petal_locate.server.store as store_module

    monkeypatch.setattr(store_module, "MAX_PENDING_WRITES", 3)
    gate = threading.Event()
    blocker = asyncio.get_running_loop().run_in_executor(store._executor, gate.wait)  # the worker is stuck
    accepted = [store.put_event(**_event(event_id=f"EV-00000{k}", seq=k)) for k in range(1, 4)]  # type: ignore[arg-type]
    with pytest.raises(StoreError, match="already waiting"):
        store.put_event(**_event(event_id="EV-000009", seq=9))  # type: ignore[arg-type]
    assert store.counters()["put_event_refused_backlog"] == 1
    gate.set()
    await blocker
    await asyncio.gather(*accepted)
    assert await store.count_rows("events") == 3 and store.pending_writes == 0


async def test_node_sessions_open_close_and_replace(store: Store) -> None:
    store.node_session_opened("alpha", "node-1", T0, "sim", "1.0")
    store.node_session_opened("alpha", "node-1", T0 + 10, "sim", "1.0")  # replaced without a close
    store.node_session_closed("alpha", T0 + 20, "disconnected")
    store.node_session_closed("ghost", T0 + 20, "disconnected")  # no open session: nothing happens
    rows = await store.query("SELECT connected_ns, disconnected_ns, reason FROM node_sessions ORDER BY id")
    assert rows == [(T0, T0 + 10, "replaced"), (T0 + 10, T0 + 20, "disconnected")]


async def test_close_waits_for_pending_writes_and_reopening_resumes(tmp_path: Path) -> None:
    data = tmp_path / "data"
    s = Store(data)
    await s.open()
    await s.save_config("name: x\n", "yaml", "api")
    for i in range(50):
        s.put_event(**_event(event_id=f"EV-{i:06d}", seq=i + 1))  # type: ignore[arg-type]
    s.node_session_opened("alpha", "n", T0)
    await s.close()  # drains all 50 writes before closing
    assert s.pending_writes == 0
    await s.close()  # idempotent
    with pytest.raises(StoreError, match="closed"):
        await s.latest_config()
    with pytest.raises(StoreError, match="closed"):
        s.put_event(**_event())  # type: ignore[arg-type]

    again = Store(data)
    await again.open()
    try:
        assert again.previous_run_id == s.run_id and again.run_id != s.run_id
        latest = await again.latest_config()
        assert latest is not None and latest.source == "api"
        assert len(await again.load_events(limit=1000)) == 50
        assert await again.load_events(limit=3) != []
        reason = await again.query("SELECT reason FROM node_sessions")
        assert reason == [("server stopped",)]
    finally:
        await again.close()
    assert store_threads() == []


async def test_unusable_database_fails_open_with_the_path(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    (data / DB_FILE_NAME).write_bytes(b"this is not an sqlite database, just some bytes" * 100)
    s = Store(data)
    with pytest.raises(StoreError, match="not a usable database"):
        await s.open()
    assert not s.is_open and store_threads() == []
    with pytest.raises(StoreError):
        await s.open()  # a failed store cannot be reopened
    newer = tmp_path / "newer"
    s2 = Store(newer)
    await s2.open()
    await s2.close()
    conn = sqlite3.connect(newer / DB_FILE_NAME)
    with conn:
        conn.execute("UPDATE meta SET value = '99' WHERE key = 'schema_version'")
    conn.close()
    with pytest.raises(StoreError, match="newer version"):
        await Store(newer).open()
    assert store_threads() == []


async def test_captures_are_written_as_sigmf_with_size_retention(store: Store) -> None:
    rng = np.random.default_rng(1)

    def iq(n: int) -> np.ndarray:
        return (rng.standard_normal(n) + 1j * rng.standard_normal(n)).astype(np.complex64)

    first = await store.save_capture(
        iq(100), t_first_ns=T0, sample_rate_hz=31250.0, center_hz=260e6, channel_id="alpha-a", capture_id="c1",
        event_id="EV-000001", extra={"pair": "a-b"},
    )
    assert first is not None and first.bytes == 800 and first.deleted == ()
    data = np.fromfile(first.data_path, dtype="<c8")
    assert data.shape == (100,)
    meta = json.loads(first.meta_path.read_text())
    assert meta["global"]["core:datatype"] == "cf32_le" and meta["global"]["petal:t_first_ns"] == str(T0)
    assert meta["captures"][0]["core:frequency"] == 260e6
    assert meta["captures"][0]["core:datetime"].startswith("2026-11-13T")
    second = await store.save_capture(iq(100), t_first_ns=T0 + 1, sample_rate_hz=31250.0, center_hz=260e6,
                                      channel_id="alpha-a", capture_id="c2")
    third = await store.save_capture(iq(150), t_first_ns=T0 + 2, sample_rate_hz=31250.0, center_hz=260e6,
                                     channel_id="alpha/a", capture_id="c3")
    assert second is not None and third is not None
    # 800 + 800 + 1200 > 2500: the oldest goes.
    assert third.deleted == (first.data_path,)
    assert not first.data_path.exists() and not first.meta_path.exists()
    assert "alpha_a" in third.data_path.name  # unsafe characters are replaced
    assert await store.capture_bytes() == 2000
    too_big = await store.save_capture(iq(400), t_first_ns=T0 + 3, sample_rate_hz=1.0, center_hz=1.0,
                                       channel_id="x", capture_id="c4")
    assert too_big is None
    with pytest.raises(ValueError):
        await store.save_capture(np.zeros(4), t_first_ns=T0, sample_rate_hz=1.0, center_hz=1.0, channel_id="x",
                                 capture_id="c5")


async def test_capture_saving_can_be_disabled(tmp_path: Path) -> None:
    s = Store(tmp_path, save_captures=False)
    await s.open()
    try:
        saved = await s.save_capture(np.ones(4, np.complex64), t_first_ns=T0, sample_rate_hz=1.0, center_hz=1.0,
                                     channel_id="x", capture_id="c")
        assert saved is None and not s.captures_dir.exists()
    finally:
        await s.close()


async def test_bad_construction_arguments() -> None:
    with pytest.raises(ValueError):
        Store("x", capture_limit_bytes=0)
    with pytest.raises(ValueError):
        Store("x", capture_limit_bytes=1.5)  # type: ignore[arg-type]


async def test_writes_never_hold_the_event_loop(store: Store) -> None:
    """Many writes are queued from the loop while a ticker measures how late it runs."""
    lags: list[float] = []
    stop = asyncio.Event()

    async def ticker() -> None:
        loop = asyncio.get_running_loop()
        while not stop.is_set():
            t = loop.time()
            await asyncio.sleep(0.005)
            lags.append(loop.time() - t - 0.005)

    task = asyncio.create_task(ticker())
    big = "x" * 200_000
    for i in range(40):
        store.put_event(**_event(event_id=f"EV-{i:06d}", seq=i + 1, doc=big))  # type: ignore[arg-type]
        await asyncio.sleep(0)
    await store.save_config(big, "yaml", "api")
    stop.set()
    await task
    assert max(lags) < 0.05
    assert threading.current_thread() is threading.main_thread()


# --------------------------------------------------------------------------- retention (review K13)


async def _fill(store: Store, n: int, step_ns: int) -> None:
    """``n`` events, one updated every ``step_ns`` from T0, each with history rows."""
    for k in range(n):
        event_id = f"EV-{k + 1:06d}"
        t = T0 + k * step_ns
        store.put_event(**_event(event_id=event_id, seq=k + 1, created_ns=t, updated_ns=t,  # type: ignore[arg-type]
                                 doc=json.dumps({"id": event_id})))
        store.add_transitions(event_id, [(t, "detected", "undetermined", "detected")])
        store.add_evidence_snapshot(event_id, t, "[]")
        store.add_measurement(event_id, 1, t, "tdoa", True, "{}")
        store.add_solution(event_id, t, True, "{}")
    await asyncio.gather(*list(store._pending), return_exceptions=True)


async def test_history_is_pruned_by_age_and_by_count(tmp_path: Path) -> None:
    """The history was never pruned: a long-running server grew its database without
    bound. With limits set, old events go with every row that belongs to them."""
    hour = 3600 * 10**9
    now = [T0 + 10 * hour]
    store = Store(tmp_path / "d", history_max_age_s=4 * 3600.0, history_max_events=3, clock=lambda: now[0])
    await store.open()
    try:
        assert store.history_limits == (4 * 3600.0, 3)
        await _fill(store, 10, hour)  # updated at T0 .. T0 + 9 h
        store.add_calibration(t_ns=T0, carrier_id="ref", kind="tdoa", a_sat_id="a", a_site_id="x", b_sat_id="b",
                              b_site_id="y", correction=0.0, sigma=1.0)
        store.add_calibration(t_ns=T0 + 9 * hour, carrier_id="ref", kind="tdoa", a_sat_id="a", a_site_id="x",
                              b_sat_id="b", b_site_id="y", correction=0.0, sigma=1.0)
        await asyncio.gather(*list(store._pending), return_exceptions=True)
        result = await store.prune_history()
        # age: updated before T0 + 6 h go (7 events); count: of the 4 left, the oldest goes too
        assert result.events == 7 and result.calibrations == 1
        kept = sorted(r[0] for r in await store.query("SELECT event_id FROM events"))
        assert kept == ["EV-000008", "EV-000009", "EV-000010"]
        for table in ("event_transitions", "evidence_snapshots", "measurements", "solutions"):
            assert await store.count_rows(table) == 3, table
        assert await store.count_rows("calibrations") == 1
        assert (await store.prune_history()).events == 0  # nothing more to do
    finally:
        await store.close()
    # the limits also apply when a store opens on an old database
    later = Store(tmp_path / "d", history_max_events=1, clock=lambda: now[0])
    await later.open()
    try:
        assert await later.count_rows("events") == 1
    finally:
        await later.close()
    # without limits nothing is ever deleted
    plain = Store(tmp_path / "d", clock=lambda: now[0] + 1000 * hour)
    await plain.open()
    try:
        assert (await plain.prune_history()).events == 0 and await plain.count_rows("events") == 1
    finally:
        await plain.close()


async def test_history_is_pruned_while_events_are_written(tmp_path: Path) -> None:
    hour = 3600 * 10**9
    now = [T0]
    store = Store(tmp_path / "d", history_max_events=2, clock=lambda: now[0])
    await store.open()
    try:
        await _fill(store, 5, hour)
        now[0] += 2 * hour  # past PRUNE_INTERVAL_S: the next event write queues a prune
        store.put_event(**_event(event_id="EV-000099", seq=99, created_ns=now[0], updated_ns=now[0]))  # type: ignore[arg-type]
        await asyncio.gather(*list(store._pending), return_exceptions=True)
        await asyncio.gather(*list(store._pending), return_exceptions=True)
        assert await store.count_rows("events") == 2
    finally:
        await store.close()


def test_retention_limits_are_checked(tmp_path: Path) -> None:
    for bad in ({"history_max_age_s": 0.0}, {"history_max_age_s": float("nan")}, {"history_max_events": 0},
                {"capture_limit_bytes": 0}):
        with pytest.raises(ValueError):
            Store(tmp_path, **bad)  # type: ignore[arg-type]
    assert Store(tmp_path, capture_limit_bytes=1024).capture_limit_bytes == 1024
