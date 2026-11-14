"""Tests of the command line (``cli.py``): argument parsing, ``check`` on every example
configuration, ``serve`` settings and a real ``serve`` process, ``node`` usage errors,
``accuracy`` and ``montecarlo`` against stand-ins of their engines, and ``demo`` run as
a subprocess with its node processes over real WebSockets.
"""
from __future__ import annotations

import importlib.util
import json
import re
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import types
import urllib.parse
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


@pytest.mark.parametrize("command", [None, "serve", "node", "demo", "check", "accuracy", "montecarlo"])
def test_every_help_text_prints_without_format_escapes(command: str | None, capsys: pytest.CaptureFixture[str]) -> None:
    """argparse %-formats help= strings but not description= (except %(prog)s), so a '%%'
    in a description is printed as it is."""
    with pytest.raises(SystemExit) as stop:
        cli.build_parser().parse_args([*([command] if command else []), "--help"])
    assert stop.value.code == 0
    text = capsys.readouterr().out
    assert "%%" not in text and "%(" not in text, text
    if command == "accuracy":
        assert "Predicted 95 % error" in text


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


#: British spellings that must not reach an operator (US spelling in UI and CLI text).
#: Wire codes such as "cancelled" are left alone, so only "cancelling" is listed.
_BRITISH = re.compile(r"\b(centre\w*|catalogue\w*|colour\w*|behaviour\w*|honour\w*|labell\w*|cancelling|"
                      r"metres?|kilometres?|licence|organis\w*|recognis\w*|authoris\w*|initialis\w*|normalis\w*|"
                      r"analys(?:e|ed|es|ing)|favour\w*|neighbour\w*|defence|programme|whilst|grey)\b", re.I)


def _operator_strings(path: Path) -> list[tuple[int, str]]:
    """Every string constant of a module that is not a docstring (messages, help, tables)."""
    import ast

    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                docstrings.add(id(first.value))
    return [(node.lineno, node.value) for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings]


def test_operator_text_of_the_cli_and_drivers_is_in_us_spelling(capsys: pytest.CaptureFixture[str]) -> None:
    """'centre MHz' in check, 'centre ... Hz' in driver capture errors and 'catalogue
    number' in the TLE error reached operators (residual R10)."""
    package = ROOT / "src" / "petal_locate"
    paths = [package / "cli.py", package / "core" / "orbits.py", *sorted((package / "node" / "drivers").glob("*.py"))]
    found = [f"{path.relative_to(package)}:{line}: {match.group(0)!r}"
             for path in paths for line, text in _operator_strings(path) for match in _BRITISH.finditer(text)]
    assert found == []
    assert cli.main(["check", "--config", str(ROOT / "config" / "pacific-uhf.yaml"), "--time", T_FIXED,
                     "--log-level", "warning"]) == 0
    out = capsys.readouterr().out
    assert "center MHz" in out and "first downlink center" in out and not _BRITISH.search(out)


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


def test_accuracy_pair_ends_may_name_the_satellite(files: dict[str, Path], capsys: pytest.CaptureFixture[str]) -> None:
    """``--pair charlie-ab@sat-a,bravo-b`` names the satellite of a channel that watches
    two; the request checks refused it as an unknown channel (residual R1). The real
    engine runs the map."""
    out = files["tmp"] / "map.json"
    base = ["accuracy", "--config", str(files["config"]), "--time", T_FIXED, "--log-level", "warning",
            "--f-e-hz", "300e6", "--eirp-dbw", "20", "--bandwidth-hz", "10000", "--step-deg", "10"]
    assert cli.main([*base, "--pair", "charlie-ab@sat-a,bravo-b", "--pair", "charlie-ab@sat-b,alpha-a",
                     "--out", str(out)]) == 0
    schemas.AccuracyGrid.model_validate_json(out.read_text())
    assert cli.main([*base, "--pair", "charlie-ab@sat-a,charlie-ab@sat-b"]) == 2
    assert "pairs.0: the two channels of a pair must differ" in capsys.readouterr().err
    assert cli.main([*base, "--pair", "alpha-a@sat-b,bravo-b"]) == 2
    assert "does not watch satellite 'sat-b'" in capsys.readouterr().err


def test_accuracy_reference_may_be_the_nearest_one(files: dict[str, Path], capsys: pytest.CaptureFixture[str]) -> None:
    """``--reference-carrier nearest`` calibrates each cell against the reference carrier
    nearest it (core.accuracy.NEAREST_REFERENCE); the request checks refused it as an
    unknown carrier. The real engine runs the map."""
    out = files["tmp"] / "map.json"
    base = ["accuracy", "--config", str(files["config"]), "--time", T_FIXED, "--log-level", "warning",
            "--f-e-hz", "300e6", "--eirp-dbw", "20", "--bandwidth-hz", "10000", "--step-deg", "10",
            "--pair", "alpha-a,bravo-b"]
    assert cli.main([*base, "--reference-carrier", "nearest", "--out", str(out)]) == 0
    grid = schemas.AccuracyGrid.model_validate_json(out.read_text())
    # closing D2: the JSON says, per pair, why it is uncalibrated (300 MHz +/- 5 kHz overlaps ref-1)
    cells = len(grid.lat_deg) * len(grid.lon_deg)
    [pair] = grid.pairs
    assert (pair.a_channel, pair.b_channel, pair.a_sat_id, pair.b_sat_id) == ("alpha-a", "bravo-b", "sat-a", "sat-b")
    assert pair.reference_cells == {"none": cells} and pair.uncalibrated_cells == cells and pair.calibrated == []
    assert pair.reference_reason is not None and "ref-1 overlaps the target band" in pair.reference_reason
    assert json.loads(out.read_text())["pairs"][0]["reference_cells"] == {"none": cells}
    assert cli.main([*base, "--reference-carrier", "nearest", "--f-e-hz", "300.004e6", "--bandwidth-hz", "4000",
                     "--out", str(out)]) == 0
    [pair] = schemas.AccuracyGrid.model_validate_json(out.read_text()).pairs
    assert pair.reference_cells == {"ref-1": cells} and pair.calibrated == ["tdoa", "fdoa"] and pair.uncalibrated_cells == 0
    assert cli.main([*base, "--reference-carrier", "nearby"]) == 2
    assert "unknown carrier 'nearby'" in capsys.readouterr().err


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


DEMO_TOKENS: dict[str, str] = {}
"""The API token each demo printed, by its console base URL."""


def console_url(process: subprocess.Popen[str], timeout: float = 60.0) -> str:
    """The console base URL a demo printed (``http://host:port/``); the API token its
    address carries (``?token=``) is kept in ``DEMO_TOKENS``."""
    assert process.stdout is not None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        line = process.stdout.readline()
        if not line:
            raise AssertionError(f"demo ended early: {process.poll()} {process.stderr.read() if process.stderr else ''}")
        if line.strip().startswith("Console:"):
            base, _, query = line.split()[1].partition("?")
            token = urllib.parse.parse_qs(query).get("token", [""])[0]
            assert token, f"the demo's console address carries no API token: {line!r}"
            DEMO_TOKENS[base] = token
            return base
    raise AssertionError("no console URL printed")


def auth(url: str) -> dict[str, str]:
    """The Authorization header of the demo serving ``url``."""
    return {"Authorization": f"Bearer {DEMO_TOKENS[url]}"}


def every_site_online(url: str, n_sites: int) -> bool:
    try:
        reply = httpx.get(f"{url}api/v1/status", timeout=2.0, trust_env=False, headers=auth(url))
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
        assert meta["marking"] == {"level": "UNCLASSIFIED", "line": "UNCLASSIFIED"}
        assert meta["simulation"] is True and meta["api_token_required"] is True
        # a random API token per run: the console interface refuses anything without it
        assert httpx.get(f"{url}api/v1/status", trust_env=False).status_code == 401
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
    process = start_demo(["--data-dir", str(tmp_path / "data"), "--log-level", "warning",
                          "--start", "2026-11-13T12:00:00.250+09:00"])
    try:
        url = console_url(process)
        assert process.stdout is not None
        banner: list[str] = []
        while len(banner) < 20 and (not banner or banner[-1] not in ("", "Press Ctrl-C to stop.\n")):
            banner.append(process.stdout.readline())  # the rest of the banner
        # the banner says the requested epoch, not the clock a second or so later (residual R17)
        assert "  Start:      simulated time starts at 2026-11-13T03:00:00.250Z\n" in banner, banner
        port = url.rsplit(":", 1)[1].strip("/")
        wait_for(lambda: every_site_online(url, n_sites), 90.0, "every site online")
        status = httpx.get(f"{url}api/v1/status", trust_env=False, headers=auth(url)).json()
        assert all(n["driver"] == "sim" for n in status["nodes"])
        # a simulated site reports its clock as simulated (PROTOCOL.md: source "sim")
        assert all(n["clock"]["source"] == "sim" and n["clock"]["locked"] for n in status["nodes"])
        process.send_signal(signal.SIGINT)
        assert process.wait(60.0) == 0
        wait_for(lambda: not processes_with(f"ws://127.0.0.1:{port}"), 15.0, "the node processes to end")
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(10.0)


# --------------------------------------------------------------------------- review hardening


def test_serve_settings_carry_the_new_limits_and_warn_without_a_token(
    files: dict[str, Path], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.delenv("PETAL_API_TOKEN", raising=False)
    tokens = files["tmp"] / "tokens.yaml"
    tokens.write_text("alpha: token-a\n")
    args = cli.build_parser().parse_args([
        "serve", "--config", str(files["config"]), "--node-tokens", str(tokens), "--allowed-host", "Console.Example",
        "--allowed-host", "10.0.0.5", "--allowed-origin", "https://console.example", "--capture-limit-bytes", "1048576",
        "--history-max-age-s", "86400", "--history-max-events", "500"])
    settings, _cfg = cli.serve_settings(args)
    assert settings.allowed_hosts == ("console.example", "10.0.0.5")
    assert settings.allowed_origins == ("https://console.example",)
    assert (settings.capture_limit_bytes, settings.history_max_age_s, settings.history_max_events) == (1048576, 86400.0, 500)
    assert settings.node_tokens_path == tokens  # re-read when it changes: no restart for a new site
    assert any("no API token" in r.getMessage() and r.levelname == "WARNING" for r in caplog.records)
    args = cli.build_parser().parse_args(["serve", "--config", str(files["config"]), "--history-max-events", "0"])
    with pytest.raises(cli.UsageError, match="history_max_events"):
        cli.serve_settings(args)


def test_serve_on_port_0_logs_the_port_it_really_bound(files: dict[str, Path]) -> None:
    """It logged 'http://127.0.0.1:0/' (review M4)."""
    process = subprocess.Popen(
        [sys.executable, "-m", "petal_locate.cli", "serve", "--config", str(files["config"]), "--port", "0",
         "--data-dir", str(files["tmp"] / "data")],
        env=child_env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        assert process.stderr is not None
        lines: list[str] = []
        reader = threading.Thread(target=lambda: lines.extend(iter(process.stderr.readline, "")), daemon=True)
        reader.start()  # a reader thread: a server that logs nothing more cannot block the test

        def logged_port() -> int | None:
            for line in list(lines):
                match = re.search(r"serving 'api-test' on http://127\.0\.0\.1:(\d+)/", line)
                if match:
                    return int(match.group(1))
            return None

        wait_for(lambda: logged_port() is not None, 30.0, "the serving line")
        port = logged_port()
        assert port, "the serving line names port 0, not the port bound"
        meta = wait_for(lambda: _meta(port), 30.0, "the server on the logged port")
        assert meta["config_name"] == "api-test"
        process.send_signal(signal.SIGTERM)
        assert process.wait(30.0) == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(10.0)
        process.communicate()


def _meta(port: int) -> Any:
    try:
        reply = httpx.get(f"http://127.0.0.1:{port}/api/v1/meta", timeout=2.0, trust_env=False)
    except httpx.HTTPError:
        return None
    return reply.json() if reply.status_code == 200 else None


def test_node_options_per_channel_and_from_a_node_file(files: dict[str, Path]) -> None:
    """Every channel used to get the same driver options; chunk and buffer sizes had no
    flags (review K12)."""
    node_file = files["tmp"] / "node.yaml"
    node_file.write_text(
        "driver_options: {lookback_s: 10, gain_db: 20}\n"
        "channels:\n  tokyo-pac: {device: 'driver=uhd,serial=A1'}\n  tokyo-172e: {device: 'driver=uhd,serial=B2'}\n"
        "chunk_payload_bytes: 16384\n"
    )
    args = cli.build_parser().parse_args([
        "node", "--server", "ws://s", "--site", "tokyo", "--driver", "soapy", "--node-config", str(node_file),
        "--driver-option", "gain_db=30", "--channel-option", "tokyo-io:device=driver=uhd,serial=C3",
        "--channel-option", "tokyo-pac:antenna=RX2", "--send-buffer-bytes", "262144"])
    assert cli.node_driver_options(args) == {"lookback_s": 10, "gain_db": 30}  # the flag wins over the file
    assert cli.node_channel_options(args) == {
        "tokyo-pac": {"device": "driver=uhd,serial=A1", "antenna": "RX2"},
        "tokyo-172e": {"device": "driver=uhd,serial=B2"},
        "tokyo-io": {"device": "driver=uhd,serial=C3"},
    }
    assert cli._node_number(args, "chunk_payload_bytes") == 16384
    assert cli._node_number(args, "send_buffer_bytes") == 262144
    for text, message in [("[1, 2]\n", "must be a mapping"), ("gain: 1\n", "unknown key"),
                          ("channels: {a: 3}\n", "channels must map"), ("chunk_payload_bytes: big\n", "integer")]:
        node_file.write_text(text)
        with pytest.raises(cli.UsageError, match=message):
            cli.load_node_config(node_file)
    bad = cli.build_parser().parse_args(["node", "--server", "ws://s", "--site", "a", "--driver", "noise",
                                         "--channel-option", "nochannel"])
    with pytest.raises(cli.UsageError, match="CHANNEL:KEY=VALUE"):
        cli.node_channel_options(bad)


def test_node_command_hands_its_settings_to_the_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    """A sim node reports a 'sim' clock (review K7: it said 'ntp, locked'); per-channel
    options, chunk and buffer sizes and the simulated time offset reach the agent."""
    import petal_locate.node.agent as agent_module

    seen: dict[str, Any] = {}

    class FakeAgent:
        def __init__(self, server: str, site: str, token: str, **kwargs: Any) -> None:
            seen.update(kwargs, server=server, site=site)
            self.site_id = site
            self.server_ws_url = server

        def request_stop(self) -> None:
            pass

        async def run(self) -> None:
            return None

    monkeypatch.setattr(agent_module, "NodeAgent", FakeAgent)
    monkeypatch.setenv("PETAL_NODE_TOKEN_ALPHA", "t")
    base = ["node", "--server", "ws://127.0.0.1:9", "--site", "alpha"]
    assert cli.main([*base, "--driver", "sim", "--channel-option", "alpha-a:lookback_s=5",
                     "--chunk-payload-bytes", "8192", "--send-buffer-bytes", "65536",
                     "--sim-time-offset-ns", str(-86400 * 10**9)]) == 0
    clock = seen["clock"]
    assert clock.status().source == "sim" and clock.status().locked
    assert abs(clock.now_ns() - (time.time_ns() - 86400 * 10**9)) < 10**9
    assert seen["channel_driver_options"] == {"alpha-a": {"lookback_s": 5}}
    assert (seen["chunk_payload_bytes"], seen["send_buffer_bytes"]) == (8192, 65536)
    seen.clear()
    assert cli.main([*base, "--driver", "noise"]) == 0
    assert seen["clock"] is None and "chunk_payload_bytes" not in seen  # the agent's own defaults
    assert cli.main([*base, "--driver", "noise", "--sim-time-offset-ns", "5"]) == 2  # simulation only
    assert cli.main([*base, "--driver", "noise", "--channel-option", "alpha-a:no_such_option=1"]) == 2


def test_node_takes_a_required_driver_option_given_per_channel_only(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Every channel of a sigmf node replays its own recording, so 'path' is given per
    channel only; the node refused to start because it first built a driver from the
    shared options alone (residual R13)."""
    import petal_locate.node.agent as agent_module

    seen: dict[str, Any] = {}

    class FakeAgent:
        def __init__(self, server: str, site: str, token: str, **kwargs: Any) -> None:
            seen.update(kwargs)
            self.site_id = site
            self.server_ws_url = server

        def request_stop(self) -> None:
            pass

        async def run(self) -> None:
            return None

    monkeypatch.setattr(agent_module, "NodeAgent", FakeAgent)
    monkeypatch.setenv("PETAL_NODE_TOKEN_ALPHA", "t")
    base = ["node", "--server", "ws://127.0.0.1:9", "--site", "alpha", "--driver", "sigmf"]
    assert cli.main([*base, "--channel-option", "alpha-a:path=/data/alpha-a.sigmf-meta",
                     "--channel-option", "alpha-b:path=/data/alpha-b.sigmf-meta", "--driver-option", "loop=true"]) == 0
    assert seen["driver_options"] == {"loop": True}
    assert seen["channel_driver_options"] == {"alpha-a": {"path": "/data/alpha-a.sigmf-meta"},
                                              "alpha-b": {"path": "/data/alpha-b.sigmf-meta"}}
    # a channel the server assigns without its own options cannot open: said once, up front
    warned = [line for line in capsys.readouterr().err.splitlines() if "no options of their own" in line]
    assert len(warned) == 1 and "WARNING" in warned[0] and "'path'" in warned[0]
    # still refused: no path anywhere, a bad shared option, a bad per-channel option
    assert cli.main(base) == 2
    assert "path" in capsys.readouterr().err
    assert cli.main([*base, "--channel-option", "alpha-a:path=/a.sigmf-meta", "--driver-option", "loop=maybe"]) == 2
    assert "(channel alpha-a)" in capsys.readouterr().err
    assert cli.main([*base, "--channel-option", "alpha-a:path=/a.sigmf-meta",
                     "--channel-option", "alpha-b:no_such_option=1"]) == 2
    assert "(channel alpha-b)" in capsys.readouterr().err


def test_demo_banner_says_the_requested_start_epoch() -> None:
    """The banner printed the simulated clock when it was written, a second or more
    after the epoch asked for (residual R17)."""
    assert cli.demo_start_line(None) is None
    assert cli.demo_start_line("2026-11-13T03:00:00Z") == ("Start:", "simulated time starts at 2026-11-13T03:00:00Z")
    assert cli.demo_start_line("2026-11-13T12:00:00.250+09:00") == (
        "Start:", "simulated time starts at 2026-11-13T03:00:00.250Z")


def test_demo_start_epoch_offset() -> None:
    now = 1_794_571_200 * 10**9
    assert cli.demo_time_offset_ns(None, now) == 0
    assert cli.demo_time_offset_ns("2026-11-13T03:00:00Z", now) == 1_794_538_800 * 10**9 - now
    for bad in ("noon", "1900-01-01T00:00:00Z", "2260-01-01T00:00:00Z"):
        with pytest.raises(cli.UsageError, match="--start"):
            cli.demo_time_offset_ns(bad, now)
    args = cli.build_parser().parse_args(["demo", "--start", "2026-11-13T03:00:00Z"])
    assert args.start == "2026-11-13T03:00:00Z"
    assert cli.DEMO_MARKING_LINE == "UNCLASSIFIED"  # the SIMULATION tag says simulated (review UX-14)


def test_check_survives_an_orbit_that_does_not_cover_the_time(files: dict[str, Path],
                                                              capsys: pytest.CaptureFixture[str]) -> None:
    """A table orbit that did not cover --time crashed check with a traceback (ADAPT-6)."""
    from petal_locate.config import SystemConfig
    from petal_locate.core.orbits import orbit_state
    from petal_locate.core.timebase import iso_from_ns, sec

    cfg = make_config()
    t_end = 1_794_571_200 * 10**9
    samples = []
    for k in range(-30, 1):
        t = t_end + k * 60 * 10**9
        r, v = orbit_state(cfg.orbit("sat-a"), sec(t))
        samples.append({"t": iso_from_ns(t), "r_m": [float(x) for x in r], "v_mps": [float(x) for x in v]})
    data = cfg.export_dict()
    data["satellites"][0]["orbit"] = {"type": "table", "samples": samples}
    path = files["tmp"] / "table.yaml"
    path.write_text(dump_config(SystemConfig.model_validate(data)))
    assert cli.main(["check", "--config", str(path), "--time", iso_from_ns(t_end + 86400 * 10**9)]) == 0
    out = capsys.readouterr().out
    assert "satellite sat-a: orbit not available at this time" in out
    assert "the orbit cannot be evaluated" in out  # and the warnings say why
