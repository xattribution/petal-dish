"""Tests of the command line (``cli.py``): argument parsing, ``check`` on every example
configuration, ``serve`` settings and a real ``serve`` process, ``node`` usage errors,
``accuracy`` and ``montecarlo`` against stand-ins of their engines, and ``demo`` run as
a subprocess with its node processes over real WebSockets.
"""
from __future__ import annotations

import importlib.util
import json
import os
import signal
import socket
import subprocess
import sys
import time
import types
from pathlib import Path
from typing import Any

import httpx
import pytest

from petal_locate import cli
from petal_locate.config import dump_config
from petal_locate.server.api import schemas
from petal_locate.sim.scenario import dump_scenario

from test_api import make_config, make_scenario, stand_in_accuracy  # tests/ is on sys.path (pytest prepend mode)

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = sorted((ROOT / "config").glob("*.yaml"))
T_FIXED = "2026-11-13T12:00:00Z"


@pytest.fixture(autouse=True)
def restore_logging() -> Any:
    """``cli.main`` points the root logger at the stderr of the moment, which pytest
    closes after the test; later tests' log records would then hit a closed stream
    ('I/O operation on closed file'). Put the logging configuration back."""
    import logging

    names = ("", "uvicorn", "uvicorn.error", "uvicorn.access")
    saved = {
        name: (list(logging.getLogger(name).handlers), logging.getLogger(name).level, logging.getLogger(name).propagate)
        for name in names
    }
    yield
    for name, (handlers, level, propagate) in saved.items():
        logger = logging.getLogger(name)
        for handler in list(logger.handlers):
            if handler not in handlers:
                logger.removeHandler(handler)
        for handler in handlers:
            if handler not in logger.handlers:
                logger.addHandler(handler)
        logger.setLevel(level)
        logger.propagate = propagate


@pytest.fixture
def files(tmp_path: Path) -> dict[str, Path]:
    config = tmp_path / "config.yaml"
    config.write_text(dump_config(make_config()))
    scenario = tmp_path / "scenario.yaml"
    scenario.write_text(dump_scenario(make_scenario()))
    return {"config": config, "scenario": scenario, "tmp": tmp_path}


def child_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("PETAL_")}
    env.update({"OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1", "PYTHONUNBUFFERED": "1"})
    return env


def processes_with(marker: str) -> list[int]:
    """PIDs of the processes whose command line contains ``marker``."""
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cmdline = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
        except OSError:
            continue
        if marker in cmdline and int(entry.name) != os.getpid():
            found.append(int(entry.name))
    return found


def wait_for(predicate: Any, timeout: float, what: str, interval: float = 0.2) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError(f"timed out waiting for {what}")


# --------------------------------------------------------------------------- parsing


def test_parser_accepts_the_documented_commands() -> None:
    parser = cli.build_parser()
    args = parser.parse_args(["serve", "--config", "c.yaml", "--scenario", "s.yaml", "--host", "0.0.0.0", "--port",
                              "9000", "--data-dir", "d", "--node-tokens", "t.yaml", "--api-token", "T",
                              "--marking-level", "UNCLASSIFIED", "--marking-line", "UNCLASSIFIED // X"])
    assert (args.command, args.port, args.host, args.node_tokens, args.marking_line) == (
        "serve", 9000, "0.0.0.0", "t.yaml", "UNCLASSIFIED // X")
    args = parser.parse_args(["node", "--server", "ws://s:1", "--site", "a", "--token", "x", "--driver", "sigmf",
                              "--node-id", "n1", "--sigmf-path", "r.sigmf-meta", "--driver-option", "loop=true"])
    assert (args.driver, args.node_id, args.driver_option) == ("sigmf", "n1", ["loop=true"])
    args = parser.parse_args(["demo"])
    assert (args.config, args.scenario, args.port, args.driver) == (None, None, 8080, "sim")
    args = parser.parse_args(["check", "--config", "c.yaml"])
    assert args.handler is cli.cmd_check
    args = parser.parse_args(["accuracy", "--config", "c", "--pair", "a,b", "--pair", "c,d", "--f-e-hz", "3e8"])
    assert args.pair == ["a,b", "c,d"] and args.f_e_hz == 3e8
    args = parser.parse_args(["montecarlo", "--config", "c", "--scenario", "s", "--trials", "5"])
    assert args.trials == 5


def test_usage_errors_exit_2(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main([]) == 2
    assert cli.main(["serve"]) == 2  # --config is required
    assert cli.main(["node", "--server", "ws://x", "--site", "a", "--driver", "bogus"]) == 2
    assert cli.main(["montecarlo", "--config", "c", "--scenario", "s", "--trials", "many"]) == 2
    assert cli.main(["--version"]) == 0
    assert "petal-locate" in capsys.readouterr().out


def test_table_and_key_values() -> None:
    table = cli.format_table(["name", "value"], [["a", "1.5"], ["long name", "-"]])
    lines = table.splitlines()
    assert lines[0].startswith("name") and lines[2].endswith("1.5") and lines[3].endswith("-")
    assert cli._key_values(["a=1", "b-c=true", "d=text", "e="], "--x") == {"a": 1, "b_c": True, "d": "text", "e": ""}
    with pytest.raises(cli.UsageError):
        cli._key_values(["novalue"], "--x")
    assert cli._jsonable({"x": float("nan"), "y": [1.0, float("inf")]}) == {"x": None, "y": [1.0, None]}


# --------------------------------------------------------------------------- check


@pytest.mark.parametrize("path", EXAMPLES, ids=[p.stem for p in EXAMPLES])
def test_check_every_example_configuration(path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["check", "--config", str(path), "--time", T_FIXED, "--log-level", "warning"]) == 0
    out = capsys.readouterr().out
    for section in ("is valid", "Satellites", "Look angles", "Monitor windows", "Link budgets", "Warnings"):
        assert section in out, section
    assert "NaN" not in out and "nan" not in out.replace("nanosec", "")


def test_check_reports_the_example_details(capsys: pytest.CaptureFixture[str]) -> None:
    code = cli.main(["check", "--config", str(ROOT / "config" / "pacific-uhf.yaml"), "--scenario",
                     str(ROOT / "config" / "scenarios" / "pacific-demo.yaml"), "--time", T_FIXED])
    assert code == 0
    out = capsys.readouterr().out
    assert "Scenario 'pacific-demo' fits it" in out
    assert "electrically small dish" in out and "BELOW HORIZON" in out
    assert "carrier ref-guam" in out and "carrier ref-sapporo" in out
    assert "muos-pac:shared-1" in out


def test_check_refuses_bad_input(files: dict[str, Path], capsys: pytest.CaptureFixture[str]) -> None:
    bad = files["tmp"] / "bad.yaml"
    bad.write_text(files["config"].read_text().replace("lat_deg: 35.0", "lat_deg: 135.0"))
    assert cli.main(["check", "--config", str(bad)]) == 2
    assert "sites.0.lat_deg" in capsys.readouterr().err
    assert cli.main(["check", "--config", str(files["tmp"] / "missing.yaml")]) == 2
    assert "cannot be read" in capsys.readouterr().err
    misfit = files["tmp"] / "misfit.yaml"
    misfit.write_text(dump_scenario(make_scenario()).replace("jam-1", "ref-1"))  # collides with the carrier emitter
    assert cli.main(["check", "--config", str(files["config"]), "--scenario", str(misfit)]) == 2
    assert "ref-1" in capsys.readouterr().err
    assert cli.main(["check", "--config", str(files["config"]), "--time", "noon"]) == 2


# --------------------------------------------------------------------------- serve


def test_serve_settings(files: dict[str, Path], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    tokens = files["tmp"] / "tokens.yaml"
    tokens.write_text("alpha: token-a\nnowhere: token-x\n")
    monkeypatch.setenv("PETAL_NODE_TOKEN_BRAVO", "token-b")
    monkeypatch.setenv("PETAL_API_TOKEN", "api-from-env")
    args = cli.build_parser().parse_args(["serve", "--config", str(files["config"]), "--scenario",
                                          str(files["scenario"]), "--node-tokens", str(tokens), "--marking-level",
                                          "CUI", "--port", "0", "--data-dir", str(files["tmp"] / "d")])
    settings, cfg = cli.serve_settings(args)
    assert cfg.name == "api-test" and settings.port == 0 and settings.scenario_path == files["scenario"]
    assert settings.node_tokens == {"alpha": "token-a", "nowhere": "token-x"}
    assert settings.node_token("bravo") == "token-b" and settings.api_token == "api-from-env"
    assert settings.marking is not None and (settings.marking.level, settings.marking.line) == ("CUI", "CUI")
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "nowhere" in text and "charlie" in text  # unknown site, and a site without any token
    assert "token-a" not in text

    for extra, message in [
        (["--marking-line", "TEXT"], "--marking-line needs --marking-level"),
        (["--node-tokens", str(files["tmp"] / "none.yaml")], "cannot be read"),
    ]:
        args = cli.build_parser().parse_args(["serve", "--config", str(files["config"]), *extra])
        with pytest.raises(cli.UsageError, match=message):
            cli.serve_settings(args)
    assert cli.main(["serve", "--config", str(files["tmp"] / "missing.yaml")]) == 2


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_serve_process_runs_and_stops_cleanly_on_sigterm(files: dict[str, Path]) -> None:
    port = free_port()
    env = child_env() | {"PETAL_API_TOKEN": "cli-api-token"}
    process = subprocess.Popen(
        [sys.executable, "-m", "petal_locate.cli", "serve", "--config", str(files["config"]), "--port", str(port),
         "--data-dir", str(files["tmp"] / "data"), "--marking-level", "UNCLASSIFIED", "--log-level", "warning"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        base = f"http://127.0.0.1:{port}/api/v1"

        def meta() -> Any:
            try:
                reply = httpx.get(f"{base}/meta", timeout=2.0, trust_env=False)
            except httpx.HTTPError:
                return None
            return reply.json() if reply.status_code == 200 else None

        doc = wait_for(meta, 30.0, "the server to answer")
        assert doc["marking"] == {"level": "UNCLASSIFIED", "line": "UNCLASSIFIED"} and doc["simulation"] is False
        assert doc["api_token_required"] is True
        assert httpx.get(f"{base}/status", trust_env=False).status_code == 401
        reply = httpx.get(f"{base}/status", headers={"Authorization": "Bearer cli-api-token"}, trust_env=False)
        assert reply.status_code == 200
        process.send_signal(signal.SIGTERM)
        assert process.wait(30.0) == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(10.0)
        _out, err = process.communicate()
    assert "cli-api-token" not in err


# --------------------------------------------------------------------------- node


def test_node_usage_errors(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.delenv("PETAL_NODE_TOKEN", raising=False)
    monkeypatch.delenv("PETAL_NODE_TOKEN_ALPHA", raising=False)
    base = ["node", "--server", "ws://127.0.0.1:9", "--site", "alpha", "--driver", "noise"]
    assert cli.main(base) == 2
    assert "PETAL_NODE_TOKEN_ALPHA" in capsys.readouterr().err
    monkeypatch.setenv("PETAL_NODE_TOKEN_ALPHA", "t")
    assert cli.main([*base, "--driver-option", "lookback"]) == 2
    assert cli.main([*base, "--driver-option", "no_such_option=1"]) == 2
    assert "refused its options" in capsys.readouterr().err
    assert cli.main([*base, "--sigmf-path", "x.sigmf-meta"]) == 2
    assert cli.main(["node", "--server", "ftp://nowhere", "--site", "alpha", "--driver", "noise"]) == 2


def test_node_driver_options() -> None:
    args = cli.build_parser().parse_args(["node", "--server", "ws://s", "--site", "a", "--driver", "soapy",
                                          "--soapy-device", "driver=rtlsdr", "--driver-option", "gain-db=30",
                                          "--driver-option", "time_sync=pps"])
    assert cli.node_driver_options(args) == {"gain_db": 30, "time_sync": "pps", "device": "driver=rtlsdr"}


# --------------------------------------------------------------------------- accuracy


def test_accuracy_command(files: dict[str, Path], monkeypatch: pytest.MonkeyPatch,
                          capsys: pytest.CaptureFixture[str]) -> None:
    calls: list[Any] = []
    module = types.ModuleType("petal_locate.core.accuracy")
    module.accuracy_map = stand_in_accuracy(calls)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "petal_locate.core.accuracy", module)
    out = files["tmp"] / "map.json"
    base = ["accuracy", "--config", str(files["config"]), "--time", T_FIXED, "--log-level", "warning"]
    flags = ["--pair", "alpha-a,bravo-b", "--f-e-hz", "300e6", "--eirp-dbw", "20", "--bandwidth-hz", "10000",
             "--step-deg", "5", "--kinds", "tdoa"]
    assert cli.main([*base, *flags, "--out", str(out)]) == 0
    grid = schemas.AccuracyGrid.model_validate_json(out.read_text())
    assert len(grid.lat_deg) == 17 and len(grid.lon_deg) == 15 and calls and calls[0][2] == ("tdoa",)
    assert int(grid.t_ns) == 1_794_571_200 * 10**9

    request = files["tmp"] / "request.yaml"
    request.write_text("pairs: [[alpha-a, bravo-b]]\nemitter: {eirp_dbw: 20, bandwidth_hz: 10000}\n"
                       "f_e_hz: 300.0e6\nstep_deg: 10\nregion: {lat_min: 0, lat_max: 40, lon_min: 120, lon_max: 160}\n")
    assert cli.main([*base, "--request", str(request)]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["lat_deg"] == [0.0, 10.0, 20.0, 30.0, 40.0]

    assert cli.main([*base, "--request", str(request), "--pair", "alpha-a,nowhere"]) == 2
    assert "pairs.0.1" in capsys.readouterr().err
    assert cli.main([*base, *flags, "--step-deg", "0.25", "--max-cells", "100"]) == 2
    assert cli.main([*base, "--pair", "alpha-a"]) == 2
    assert cli.main([*base, "--pair", "alpha-a,bravo-b"]) == 2  # no emitter, no frequency
    monkeypatch.setitem(sys.modules, "petal_locate.core.accuracy", None)
    assert cli.main([*base, *flags]) == 1


# --------------------------------------------------------------------------- montecarlo


def test_montecarlo_command(files: dict[str, Path], monkeypatch: pytest.MonkeyPatch,
                            capsys: pytest.CaptureFixture[str]) -> None:
    seen: dict[str, Any] = {}

    def run_trials(config: Any, scenario: Any, emitter_id: str, n_trials: int, *, seed: int | None = None,
                   draw_errors: bool = False, progress: Any = None, capture_s: float = 1.0) -> dict[str, Any]:
        seen.update(emitter=emitter_id, n=n_trials, seed=seed, draw=draw_errors, capture_s=capture_s)
        for k in range(n_trials):
            if progress is not None:
                progress(k + 1, n_trials)
        return {"n_trials": n_trials, "miss_m": {"median": 812.5, "p95": 2400.0}, "nees": {"mean": 2.1},
                "misses": [1.0, 2.0], "unset": float("nan")}

    module = types.ModuleType("petal_locate.sim.montecarlo")
    module.run_trials = run_trials  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "petal_locate.sim.montecarlo", module)
    out = files["tmp"] / "mc.json"
    base = ["montecarlo", "--config", str(files["config"]), "--scenario", str(files["scenario"]), "--log-level",
            "warning"]
    assert cli.main([*base, "--trials", "4"]) == 2  # two jammers in the scenario: which one?
    assert "several jammers" in capsys.readouterr().err
    code = cli.main([*base, "--trials", "4", "--emitter", "jam-1", "--seed", "7", "--draw-errors", "--param",
                     "capture_s=0.5", "--out", str(out)])
    assert code == 0
    captured = capsys.readouterr()
    assert seen == {"emitter": "jam-1", "n": 4, "seed": 7, "draw": True, "capture_s": 0.5}
    assert "SIMULATED" in captured.out and "miss_m.median" in captured.out and "812.500" in captured.out
    assert "4/4" in captured.err
    data = json.loads(out.read_text())
    assert data["unset"] is None and data["miss_m"]["p95"] == 2400.0

    assert cli.main([*base, "--trials", "4", "--emitter", "nobody"]) == 2
    assert cli.main([*base, "--trials", "0", "--emitter", "jam-1"]) == 2
    assert cli.main([*base, "--trials", "2", "--emitter", "jam-1", "--param", "unknown=1"]) == 2
    monkeypatch.setitem(sys.modules, "petal_locate.sim.montecarlo", None)
    assert cli.main([*base, "--trials", "2", "--emitter", "jam-1"]) == 1


# --------------------------------------------------------------------------- demo


def start_demo(args: list[str]) -> subprocess.Popen[str]:
    return subprocess.Popen([sys.executable, "-m", "petal_locate.cli", "demo", "--port", "0", *args],
                            env=child_env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def console_url(process: subprocess.Popen[str], timeout: float = 60.0) -> str:
    assert process.stdout is not None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        line = process.stdout.readline()
        if not line:
            raise AssertionError(f"demo ended early: {process.poll()} {process.stderr.read() if process.stderr else ''}")
        if line.strip().startswith("Console:"):
            return line.split()[1]
    raise AssertionError("no console URL printed")


def every_site_online(url: str, n_sites: int) -> bool:
    try:
        reply = httpx.get(f"{url}api/v1/status", timeout=2.0, trust_env=False)
    except httpx.HTTPError:
        return False
    nodes = reply.json()["nodes"] if reply.status_code == 200 else []
    return len(nodes) == n_sites and all(n["online"] for n in nodes)


def test_demo_plumbing_with_the_noise_driver(files: dict[str, Path]) -> None:
    """The demo's process handling on a small configuration (noise radios): every node
    comes online over real WebSockets, Ctrl-C stops every child and the server."""
    process = start_demo(["--config", str(files["config"]), "--scenario", str(files["scenario"]), "--driver", "noise",
                          "--data-dir", str(files["tmp"] / "data"), "--log-level", "warning"])
    try:
        url = console_url(process)
        port = url.rsplit(":", 1)[1].strip("/")
        wait_for(lambda: every_site_online(url, 3), 60.0, "every site online")
        assert len(processes_with(f"ws://127.0.0.1:{port}")) == 3
        meta = httpx.get(f"{url}api/v1/meta", trust_env=False).json()
        assert meta["marking"]["level"] == "UNCLASSIFIED" and "DEMONSTRATION" in meta["marking"]["line"]
        assert meta["simulation"] is True
        page = httpx.get(url, trust_env=False)
        assert page.status_code == 200 and "<html" in page.text.lower()
        process.send_signal(signal.SIGINT)
        assert process.wait(40.0) == 0
        out, _err = process.communicate()
        assert "Demonstration stopped." in out
        wait_for(lambda: not processes_with(f"ws://127.0.0.1:{port}"), 10.0, "the node processes to end")
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(10.0)


def test_demo_nodes_follow_a_killed_demo(files: dict[str, Path]) -> None:
    """Even a demo killed outright (no chance to clean up) leaves no node behind."""
    process = start_demo(["--config", str(files["config"]), "--scenario", str(files["scenario"]), "--driver", "noise",
                          "--data-dir", str(files["tmp"] / "data"), "--log-level", "warning"])
    try:
        url = console_url(process)
        port = url.rsplit(":", 1)[1].strip("/")
        wait_for(lambda: len(processes_with(f"ws://127.0.0.1:{port}")) == 3, 30.0, "three node processes")
        process.kill()
        process.wait(10.0)
        wait_for(lambda: not processes_with(f"ws://127.0.0.1:{port}"), 20.0, "orphaned nodes to stop")
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(10.0)


@pytest.mark.slow
@pytest.mark.skipif(importlib.util.find_spec("petal_locate.node.drivers.sim") is None,
                    reason="the simulated radio driver (node/drivers/sim.py) is not installed yet")
def test_demo_with_the_default_configuration(tmp_path: Path) -> None:
    """``petal-locate demo`` as shipped: the example configuration and scenario, one
    simulated node process per site; every site comes online, then a clean stop."""
    from petal_locate.config import load_config

    n_sites = len(load_config(ROOT / "config" / "pacific-uhf.yaml").sites)
    process = start_demo(["--data-dir", str(tmp_path / "data"), "--log-level", "warning"])
    try:
        url = console_url(process)
        port = url.rsplit(":", 1)[1].strip("/")
        wait_for(lambda: every_site_online(url, n_sites), 90.0, "every site online")
        status = httpx.get(f"{url}api/v1/status", trust_env=False).json()
        assert all(n["driver"] == "sim" for n in status["nodes"])
        process.send_signal(signal.SIGINT)
        assert process.wait(60.0) == 0
        wait_for(lambda: not processes_with(f"ws://127.0.0.1:{port}"), 15.0, "the node processes to end")
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(10.0)
