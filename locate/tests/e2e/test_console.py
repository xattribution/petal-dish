"""The operator console against the real server: the Playwright smoke test on a live demo.

`petal-locate demo` runs here as for an operator (one server, one `petal-locate node`
process per site with the simulated radio, real WebSockets on a free loopback port) and
serves the production console build at `/`. Once the default scenario's jammer has been
detected and located, the console's own smoke test (`ui/e2e/smoke.spec.ts`) walks every
screen in both themes against it: the watch view, an event dossier, the spectrum, the
network, look angles and an accuracy map computed by the server, a configuration field
error and a version conflict, YAML export, and placing an emitter on the simulation map.
Any console error in the browser fails the walk.

Skipped, with the reason, when Node, the console's npm dependencies, the Playwright
browser or the console build are missing. Nothing is left behind: the demo is stopped
with SIGINT and every node process it started must have exited.
"""
from __future__ import annotations

import contextlib
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
STATIC = ROOT / "src" / "petal_locate" / "server" / "static"
API = "/api/v1"
STARTUP_S = 90.0
START_EPOCH = "2026-11-13T03:00:00Z"
"""Simulated time the demo starts from (`petal-locate demo --start`), so the walk sees
the same geometry whatever the time of day."""
LOCATE_S = 120.0
"""Longest the default scenario's jammer may take to be located after start-up."""
WALK_S = 600.0

pytestmark = pytest.mark.slow


def _browser_problem(env: dict[str, str]) -> str | None:
    """Why the smoke test cannot run here, or None."""
    node = shutil.which("node")
    if node is None:
        return "Node.js is not installed (the console's Playwright smoke test needs it)"
    cli = UI / "node_modules" / "@playwright" / "test" / "cli.js"
    if not cli.is_file():
        return "the console's npm dependencies are not installed (cd ui && npm ci)"
    if not (STATIC / "index.html").is_file():
        return "the console is not built (cd ui && npm run build)"
    probe = subprocess.run(
        [node, "-e", "process.stdout.write(require('playwright').chromium.executablePath())"],
        cwd=UI, env=env, capture_output=True, text=True, timeout=60,
    )
    path = probe.stdout.strip()
    if env.get("PW_CHROMIUM"):
        path = env["PW_CHROMIUM"]
    if probe.returncode != 0 or not path or not Path(path).exists():
        return f"no Playwright Chromium at {path or '(unknown)'} (set PLAYWRIGHT_BROWSERS_PATH or PW_CHROMIUM)"
    return None


def _node_env() -> dict[str, str]:
    env = dict(os.environ)
    if "PLAYWRIGHT_BROWSERS_PATH" not in env and Path("/opt/pw-browsers").is_dir():
        env["PLAYWRIGHT_BROWSERS_PATH"] = "/opt/pw-browsers"
    return env


def _children_of(pid: int) -> list[int]:
    """Node processes started by the demo with ``--exit-with-parent <pid>``."""
    out: list[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            argv = (entry / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        if b"--exit-with-parent" in argv:
            index = argv.index(b"--exit-with-parent")
            if index + 1 < len(argv) and argv[index + 1] == str(pid).encode():
                out.append(int(entry.name))
    return out


@pytest.fixture(scope="module")
def demo_url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[str, str]]:
    """The demo on its default scenario (pacific-demo), on a free loopback port, from a
    fixed simulated epoch, with its per-run API token: its console base URL and the token
    its address carries."""
    problem = _browser_problem(_node_env())
    if problem:
        pytest.skip(problem)
    work = tmp_path_factory.mktemp("console-demo")
    log_path = work / "demo.log"
    env = dict(os.environ, OMP_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1", PYTHONUNBUFFERED="1")
    argv = [sys.executable, "-m", "petal_locate.cli", "demo", "--port", "0", "--host", "127.0.0.1",
            "--data-dir", str(work / "data"), "--log-level", "info", "--node-log-level", "warning",
            # the demo's random API token protects it as an operator runs it; every walk of
            # ui/e2e/smoke.spec.ts enters it from PETAL_TOKEN
            "--start", START_EPOCH]
    with open(log_path, "w", encoding="utf-8") as log:
        process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=log, text=True, env=env, cwd=str(ROOT),
                                   start_new_session=True)
    try:
        assert process.stdout is not None
        base_url = token = None
        deadline = time.monotonic() + STARTUP_S
        while time.monotonic() < deadline and base_url is None:
            line = process.stdout.readline()
            if not line:
                raise AssertionError(f"the demo exited before serving:\n{log_path.read_text()[-4000:]}")
            match = re.search(r"Console:\s+(http://[^/\s]+)/(?:\?token=(\S+))?\s*$", line)
            if match:
                base_url, token = match.group(1), match.group(2) or ""
        assert base_url is not None, "the demo printed no console address"
        _wait_located(base_url, token)
        yield base_url, token
    finally:
        pid = process.pid
        if process.poll() is None:
            process.send_signal(signal.SIGINT)
            try:
                process.wait(timeout=45)
            except subprocess.TimeoutExpired:
                os.killpg(pid, signal.SIGKILL)
                process.wait()
        deadline = time.monotonic() + 15.0
        leftovers = _children_of(pid)
        while leftovers and time.monotonic() < deadline:
            time.sleep(0.2)
            leftovers = _children_of(pid)
        for child in leftovers:
            with contextlib.suppress(ProcessLookupError):
                os.kill(child, signal.SIGKILL)
        if process.stdout is not None:
            process.stdout.close()
        text = log_path.read_text(encoding="utf-8", errors="replace")
    assert not leftovers, f"node processes left running after the demo stopped: {leftovers}"
    assert "Traceback" not in text, "the server logged a traceback:\n" + text[max(0, text.find("Traceback") - 2000):][:5000]


def _wait_located(base_url: str, token: str) -> None:
    """Every node online and the default scenario's jammer located, so the walk sees a
    populated queue, a dossier with a fix, lines of position and a correlation surface."""
    deadline = time.monotonic() + STARTUP_S + LOCATE_S
    last: object = None
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    with httpx.Client(base_url=base_url, timeout=10.0, headers=headers) as client:
        while time.monotonic() < deadline:
            status = client.get(f"{API}/status").json()
            events = client.get(f"{API}/events", params={"state": "located"}).json()
            last = (sum(n["online"] for n in status["nodes"]), [e["id"] for e in events])
            if status["nodes"] and all(n["online"] for n in status["nodes"]) and events:
                return
            time.sleep(1.0)
    raise AssertionError(f"no located event in time (nodes online, located events): {last}")


def test_console_smoke_walk_against_the_live_server(demo_url: tuple[str, str], tmp_path: Path) -> None:
    base_url, token = demo_url
    env = _node_env()
    # the walk enters the demo's API token as an operator does in the token dialog
    env.update(PETAL_URL=base_url, PETAL_SHOTS=str(tmp_path / "screens"), CI="1")
    if token:
        env["PETAL_TOKEN"] = token
    node = shutil.which("node")
    assert node is not None
    started = time.monotonic()
    run = subprocess.run(
        [node, str(UI / "node_modules" / "@playwright" / "test" / "cli.js"), "test", "--trace", "off",
         "--reporter", "line"],
        cwd=UI, env=env, capture_output=True, text=True, timeout=WALK_S,
    )
    elapsed = time.monotonic() - started
    output = (run.stdout + run.stderr)[-6000:]
    print(f"\n[console] Playwright smoke walk: exit {run.returncode} in {elapsed:.0f} s")
    assert run.returncode == 0, f"the console smoke test failed against the live server:\n{output}"
    # the four walks of smoke.spec.ts; fixes.spec.ts needs the mock and is skipped here
    assert re.search(r"\b4 passed\b", run.stdout), output
    assert token, "the demo printed no API token: the walk must run against a token-protected server"
    shots = sorted(p.name for p in (tmp_path / "screens").glob("*.png"))
    assert {"watch-dark.png", "event-dark.png", "plan-accuracy-dark.png", "config-conflict-dark.png"} <= set(shots), shots
