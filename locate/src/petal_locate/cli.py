"""Command line of PETAL Locate: ``petal-locate`` (ARCHITECTURE.md section 8).

::

    petal-locate serve      --config C [--scenario S] [--host H --port P --data-dir D
                            --node-tokens FILE --api-token T --marking-level L --marking-line TEXT]
    petal-locate node       --server URL --site ID [--token T] --driver sim|sigmf|soapy|noise
                            [--node-id ID] [driver options]
    petal-locate demo       [--config C --scenario S --port P]
    petal-locate check      --config C [--scenario S] [--time ISO]
    petal-locate accuracy   --config C (--request FILE | --pair A,B ... --f-e-hz F ...) [--out FILE]
    petal-locate montecarlo --config C --scenario S --trials N [--emitter ID] [--out FILE]

Exit codes: 0 success, 2 usage or configuration error, 1 runtime failure.

Secrets: a node token may be given with ``--token`` but is better passed in the
environment (``PETAL_NODE_TOKEN_<SITE_ID>`` or ``PETAL_NODE_TOKEN``), where other users
of the machine cannot read it from the process list; ``petal-locate demo`` hands its
random per-run tokens to its nodes that way. The API token likewise comes from
``--api-token`` or ``PETAL_API_TOKEN``.

Logging is structured: ``key=value`` pairs after the message (``--log-format text``,
the default) or one JSON object per line (``--log-format json``), on standard error.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import datetime as _dt
import inspect
import json
import logging
import math
import os
import shutil
import signal
import socket
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import IO, Any, Final

import yaml

from petal_locate import __version__

__all__ = [
    "EXIT_OK",
    "EXIT_RUNTIME",
    "EXIT_USAGE",
    "DEMO_MARKING_LEVEL",
    "DEMO_MARKING_LINE",
    "UsageError",
    "build_parser",
    "configure_logging",
    "data_path",
    "main",
]

logger = logging.getLogger("petal_locate.cli")

EXIT_OK: Final[int] = 0
"""Success."""

EXIT_RUNTIME: Final[int] = 1
"""Runtime failure (the server could not start or died, a node was refused, ...)."""

EXIT_USAGE: Final[int] = 2
"""Usage or configuration error (bad arguments, a file that does not load or validate)."""

DEFAULT_CONFIG: Final[str] = "pacific-uhf.yaml"
"""Configuration of ``petal-locate demo`` (under ``config/``)."""

DEFAULT_SCENARIO: Final[str] = "scenarios/pacific-demo.yaml"
"""Scenario of ``petal-locate demo`` (under ``config/``)."""

DEMO_MARKING_LEVEL: Final[str] = "UNCLASSIFIED"
"""Marking level of the demonstration."""

DEMO_MARKING_LINE: Final[str] = "UNCLASSIFIED // DEMONSTRATION // SIMULATED DATA"
"""Banner text of the demonstration."""

DEMO_START_TIMEOUT_S: Final[float] = 60.0
"""Longest the demo waits for its server to start, s."""

DEMO_CHILD_STOP_S: Final[float] = 10.0
"""Longest the demo waits for a node process after SIGTERM before killing it, s."""

DEMO_MAX_RESTARTS: Final[int] = 3
"""Times a node process that exits on its own is restarted by the demo."""

NODE_TOKEN_ENV: Final[str] = "PETAL_NODE_TOKEN"
"""Environment variable holding a node's token when the site-specific one is unset."""

API_TOKEN_ENV: Final[str] = "PETAL_API_TOKEN"
"""Environment variable holding the console API token."""


class UsageError(Exception):
    """A usage or configuration problem: printed without a traceback, exit code 2."""


# --------------------------------------------------------------------------- logging

_RECORD_ATTRS: Final[frozenset[str]] = frozenset(
    vars(logging.LogRecord("", 0, "", 0, "", (), None)).keys()
    | {"message", "asctime", "taskName", "color_message"}
)


def _extras(record: logging.LogRecord) -> dict[str, Any]:
    return {k: v for k, v in record.__dict__.items() if k not in _RECORD_ATTRS and not k.startswith("_")}


def _plain(value: Any) -> str:
    text = str(value)
    return json.dumps(text) if (not text or any(c.isspace() for c in text) or '"' in text) else text


class KeyValueFormatter(logging.Formatter):
    """``time level logger: message key=value ...`` (the record's ``extra`` fields)."""

    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:  # noqa: N802
        stamp = _dt.datetime.fromtimestamp(record.created, tz=_dt.timezone.utc)
        return stamp.strftime("%Y-%m-%dT%H:%M:%S.") + f"{int(record.msecs):03d}Z"

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        extras = _extras(record)
        if extras:
            pairs = " ".join(f"{k}={_plain(v)}" for k, v in extras.items())
            head, sep, tail = text.partition("\n")
            text = f"{head} {pairs}{sep}{tail}"
        return text


class JsonFormatter(logging.Formatter):
    """One JSON object per line: ``t`` (ISO 8601 UTC), ``level``, ``logger``, ``msg``,
    the record's ``extra`` fields and ``exc`` (a traceback) when there is one."""

    def format(self, record: logging.LogRecord) -> str:
        stamp = _dt.datetime.fromtimestamp(record.created, tz=_dt.timezone.utc)
        obj: dict[str, Any] = {
            "t": stamp.strftime("%Y-%m-%dT%H:%M:%S.") + f"{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        obj.update(_extras(record))
        if record.exc_info:
            obj["exc"] = self.formatException(record.exc_info)
        return json.dumps(obj, default=str)


def configure_logging(level: str = "info", fmt: str = "text", *, access_log: bool = False,
                      stream: IO[str] | None = None) -> None:
    """Send every log record to standard error (or ``stream``) in the chosen format.

    level: debug, info, warning or error. fmt: ``text`` (``key=value`` extras) or
    ``json``. access_log: keep uvicorn's per-request lines (otherwise warnings only).
    """
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(JsonFormatter() if fmt == "json" else KeyValueFormatter())
    root = logging.getLogger()
    for old in list(root.handlers):
        root.removeHandler(old)
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper()))
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True
    logging.getLogger("uvicorn.access").setLevel(logging.INFO if access_log else logging.WARNING)


# --------------------------------------------------------------------------- shared helpers


def data_path(relative: str) -> Path | None:
    """A shipped data file (``config/...``): from the source tree (``locate/config``) or
    from the installed package (``petal_locate/data/config``); None when neither has it."""
    here = Path(__file__).resolve()
    for base in (here.parents[2] / "config", here.parent / "data" / "config"):
        candidate = base / relative
        if candidate.exists():
            return candidate
    return None


def _load_config(path: str | os.PathLike[str]) -> Any:
    """Load and validate a configuration file; ``UsageError`` with every problem."""
    from petal_locate.config import ConfigError, load_config

    try:
        return load_config(path)
    except ConfigError as exc:
        raise UsageError(str(exc)) from exc


def _load_scenario(path: str | os.PathLike[str], cfg: Any) -> Any:
    """Load a scenario file and check it against the configuration; ``UsageError``."""
    from petal_locate.sim.scenario import ScenarioError, load_scenario

    try:
        scenario = load_scenario(path)
        scenario.validate_against(cfg)
    except ScenarioError as exc:
        raise UsageError(f"scenario {path}: {exc}") from exc
    return scenario


def _time_ns(text: str | None) -> int:
    """``--time`` as integer ns (now when absent); ``UsageError`` for bad text."""
    from petal_locate.core.timebase import ns_from_iso

    if not text:
        return time.time_ns()
    try:
        return ns_from_iso(text)
    except ValueError as exc:
        raise UsageError(f"--time: not an ISO 8601 time ({exc})") from exc


def _number(value: Any, digits: int = 1) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            return "-"
        return f"{value:.{digits}f}" if isinstance(value, float) else str(value)
    return str(value)


def format_table(headers: Sequence[str], rows: Iterable[Sequence[Any]]) -> str:
    """A plain-text table: numbers right-aligned, text left-aligned."""
    text_rows = [[str(c) for c in row] for row in rows]
    widths = [len(h) for h in headers]
    for row in text_rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    def numeric(cell: str) -> bool:
        try:
            float(cell.replace("+", ""))
            return True
        except ValueError:
            return cell == "-"

    lines = ["  ".join(h.ljust(w) for h, w in zip(headers, widths)).rstrip()]
    lines.append("  ".join("-" * w for w in widths))
    for row in text_rows:
        cells = [c.rjust(w) if numeric(c) else c.ljust(w) for c, w in zip(row, widths)]
        lines.append("  ".join(cells).rstrip())
    return "\n".join(lines)


def _jsonable(value: Any) -> Any:
    """Plain JSON data: numpy scalars and arrays converted, NaN and infinities None."""
    try:
        import numpy as np
    except ImportError:  # pragma: no cover - numpy is a dependency
        np = None  # type: ignore[assignment]
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if np is not None and isinstance(value, np.ndarray):
        return [_jsonable(v) for v in value.tolist()]
    if np is not None and isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if hasattr(value, "model_dump"):
        return _jsonable(value.model_dump(mode="json"))
    return str(value)


def _write_output(text: str, out: str | None) -> None:
    if out is None or out == "-":
        sys.stdout.write(text)
        if not text.endswith("\n"):
            sys.stdout.write("\n")
        sys.stdout.flush()
        return
    try:
        Path(out).write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")
    except OSError as exc:
        raise UsageError(f"--out {out}: cannot be written ({exc.strerror or exc})") from exc


def _key_values(items: Sequence[str], flag: str) -> dict[str, Any]:
    """``KEY=VALUE`` arguments as a dict; values are read as YAML scalars (numbers and
    booleans become numbers and booleans)."""
    out: dict[str, Any] = {}
    for item in items:
        key, sep, raw = item.partition("=")
        if not sep or not key.strip():
            raise UsageError(f"{flag}: expected KEY=VALUE, got {item!r}")
        try:
            value = yaml.safe_load(raw) if raw.strip() else ""
        except yaml.YAMLError:
            value = raw
        out[key.strip().replace("-", "_")] = value
    return out


class _Progress:
    """A one-line progress display on standard error (a line per 10 % when not a TTY)."""

    def __init__(self, what: str, stream: IO[str] | None = None) -> None:
        self._what = what
        self._stream = stream or sys.stderr
        self._tty = bool(getattr(self._stream, "isatty", lambda: False)())
        self._start = time.monotonic()
        self._last_decile = -1
        self._lock = threading.Lock()

    def update(self, done: int, total: int | None = None) -> None:
        elapsed = time.monotonic() - self._start
        with self._lock:
            if total:
                pct = 100.0 * done / total
                text = f"{self._what}: {done}/{total} ({pct:.0f} %), {elapsed:.1f} s"
                decile = int(pct // 10)
                if not self._tty and decile == self._last_decile and done != total:
                    return
                self._last_decile = decile
            else:
                text = f"{self._what}: {elapsed:.0f} s elapsed"
                if not self._tty:
                    decile = int(elapsed // 10)
                    if decile == self._last_decile:
                        return
                    self._last_decile = decile
            if self._tty:
                self._stream.write("\r" + text.ljust(60))
            else:
                self._stream.write(text + "\n")
            self._stream.flush()

    def finish(self) -> None:
        if self._tty:
            self._stream.write("\n")
            self._stream.flush()


# --------------------------------------------------------------------------- serve


def _marking(args: argparse.Namespace) -> Any:
    from petal_locate.server.settings import Marking

    level, line = args.marking_level, args.marking_line
    if level is None and line is None:
        return None
    if level is None:
        raise UsageError("--marking-line needs --marking-level")
    try:
        return Marking(level=level, line=line if line is not None else level)
    except ValueError as exc:
        raise UsageError(f"--marking-level / --marking-line: {exc}") from exc


def serve_settings(args: argparse.Namespace) -> tuple[Any, Any]:
    """The ``Settings`` of ``petal-locate serve`` and the validated configuration.

    The configuration (and scenario) are loaded and checked here, so that a file that
    does not load is a usage error (exit 2) before anything starts. Node tokens come
    from ``--node-tokens`` and the environment; a site with neither is warned about.
    """
    from petal_locate.server.settings import Settings, load_node_tokens, node_tokens_from_env

    cfg = _load_config(args.config)
    if args.scenario is not None:
        _load_scenario(args.scenario, cfg)
    tokens: dict[str, str] = {}
    if args.node_tokens is not None:
        try:
            tokens = load_node_tokens(args.node_tokens)
        except ValueError as exc:
            raise UsageError(str(exc)) from exc
    site_ids = [site.id for site in cfg.sites]
    try:
        env_tokens = node_tokens_from_env(site_ids)
    except ValueError as exc:
        raise UsageError(str(exc)) from exc
    unknown = sorted(set(tokens) - set(site_ids))
    if unknown:
        logger.warning("node tokens given for sites the configuration does not have: %s", ", ".join(unknown))
    missing = [s for s in site_ids if s not in tokens and s not in env_tokens]
    if missing:
        logger.warning("no node token for site(s) %s: their nodes cannot connect", ", ".join(missing))
    api_token = args.api_token if args.api_token is not None else (os.environ.get(API_TOKEN_ENV) or None)
    try:
        settings = Settings(
            config_path=Path(args.config),
            scenario_path=Path(args.scenario) if args.scenario is not None else None,
            data_dir=Path(args.data_dir),
            host=args.host,
            port=args.port,
            node_tokens=tokens,
            api_token=api_token,
            marking=_marking(args),
        )
    except ValueError as exc:
        raise UsageError(f"settings: {exc}") from exc
    return settings, cfg


class _Terminated(Exception):
    """SIGTERM arrived (after the server has shut down gracefully)."""


def cmd_serve(args: argparse.Namespace) -> int:
    """``petal-locate serve``: run the server until SIGINT or SIGTERM."""
    from petal_locate.server.app import build_server, create_app

    settings, cfg = serve_settings(args)
    presets = Path(args.presets_dir) if args.presets_dir is not None else data_path("scenarios")
    app = create_app(settings, presets_dir=presets)
    server = build_server(app, settings, log_config=None)
    logger.info(
        "serving %r on http://%s:%d/ (%s)",
        cfg.name,
        settings.host,
        settings.port,
        "simulating" if settings.scenario_path is not None else "not simulating",
        extra={"api_token": settings.api_token is not None, "marking": settings.marking is not None},
    )

    def on_term(signum: int, frame: Any) -> None:
        raise _Terminated()

    previous = signal.signal(signal.SIGTERM, on_term)
    try:
        server.run()
    except (KeyboardInterrupt, _Terminated):
        pass  # uvicorn re-raises the signal after its graceful shutdown
    finally:
        signal.signal(signal.SIGTERM, previous)
    if not server.started:
        logger.error("the server did not start (see the messages above)")
        return EXIT_RUNTIME
    return EXIT_OK


# --------------------------------------------------------------------------- node


def _node_token(args: argparse.Namespace) -> str:
    from petal_locate.server.settings import node_token_env_var

    if args.token:
        return str(args.token)
    for name in (node_token_env_var(args.site), NODE_TOKEN_ENV):
        value = os.environ.get(name)
        if value:
            return value
    raise UsageError(
        f"no token for site {args.site!r}: pass --token or set {node_token_env_var(args.site)} (or {NODE_TOKEN_ENV})"
    )


def node_driver_options(args: argparse.Namespace) -> dict[str, Any]:
    """The driver options of ``petal-locate node``: ``--driver-option KEY=VALUE`` plus
    the shorthands ``--sigmf-path`` (sigmf ``path``) and ``--soapy-device`` (soapy
    ``device``)."""
    options = _key_values(args.driver_option or [], "--driver-option")
    if args.sigmf_path is not None:
        if args.driver != "sigmf":
            raise UsageError("--sigmf-path applies to --driver sigmf only")
        options["path"] = args.sigmf_path
    if args.soapy_device is not None:
        if args.driver != "soapy":
            raise UsageError("--soapy-device applies to --driver soapy only")
        options["device"] = args.soapy_device
    return options


async def _watch_parent(agent: Any, parent: int) -> None:
    """Stop the agent once process ``parent`` is no longer its parent (the demo that
    started it died without stopping it). Checked at once and then every second, so a
    parent that died while this process was still starting is noticed too."""
    while True:
        if os.getppid() != parent:
            logger.warning("parent process %d is gone; stopping", parent)
            agent.request_stop()
            return
        await asyncio.sleep(1.0)


async def _run_node(agent: Any, exit_with_parent: int | None) -> int:
    from petal_locate.node.agent import NodeRejected

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(sig, agent.request_stop)
    watcher = loop.create_task(_watch_parent(agent, exit_with_parent)) if exit_with_parent is not None else None
    try:
        await agent.run()
    except NodeRejected as exc:
        logger.error("%s", exc, extra={"site_id": agent.site_id})
        return EXIT_RUNTIME
    finally:
        if watcher is not None:
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watcher
    return EXIT_OK


def cmd_node(args: argparse.Namespace) -> int:
    """``petal-locate node``: run a node agent until SIGINT or SIGTERM."""
    from petal_locate.node.agent import NodeAgent
    from petal_locate.node.clock import SystemClock
    from petal_locate.node.drivers import CaptureError, get_driver

    token = _node_token(args)
    options = node_driver_options(args)
    try:
        get_driver(args.driver, **options)  # refuses unknown options and missing modules now
    except ValueError as exc:
        raise UsageError(f"--driver {args.driver}: {exc}") from exc
    except CaptureError as exc:
        raise UsageError(f"--driver {args.driver}: {exc}") from exc
    clock = None
    if args.clock is not None:
        clock = SystemClock(source=args.clock, locked=not args.clock_unlocked)
    try:
        agent = NodeAgent(
            args.server,
            args.site,
            token,
            driver=args.driver,
            driver_options=options,
            node_id=args.node_id,
            state_path=args.state_path,
            clock=clock,
            software=f"petal-locate/{__version__}",
        )
    except ValueError as exc:
        raise UsageError(str(exc)) from exc
    logger.info("node for site %s connecting to %s", args.site, agent.server_ws_url,
                extra={"site_id": args.site, "driver": args.driver})
    try:
        return asyncio.run(_run_node(agent, args.exit_with_parent))
    except ValueError as exc:  # an unusable node-id state file
        raise UsageError(str(exc)) from exc


# --------------------------------------------------------------------------- demo


def _bind(host: str, port: int) -> socket.socket:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((host, port))
    except OSError as exc:
        sock.close()
        raise UsageError(f"cannot listen on {host}:{port} ({exc.strerror or exc})") from exc
    sock.listen(128)
    sock.setblocking(False)
    return sock


class _Child:
    """One node process of the demo."""

    def __init__(self, site_id: str, argv: list[str], env: dict[str, str]) -> None:
        self.site_id = site_id
        self.argv = argv
        self.env = env
        self.process: asyncio.subprocess.Process | None = None
        self.restarts = 0

    async def start(self) -> None:
        self.process = await asyncio.create_subprocess_exec(
            *self.argv, env=self.env, stdin=asyncio.subprocess.DEVNULL, start_new_session=True
        )
        logger.info("node process for %s started (pid %d)", self.site_id, self.process.pid,
                    extra={"site_id": self.site_id, "pid": self.process.pid})

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.returncode is None

    async def stop(self, timeout_s: float) -> None:
        process = self.process
        if process is None or process.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError):
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=timeout_s)
        except TimeoutError:
            logger.warning("node process for %s did not stop within %.0f s; killing it", self.site_id, timeout_s,
                           extra={"site_id": self.site_id})
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.wait()


def _demo_children(cfg: Any, tokens: Mapping[str, str], url: str, driver: str, log_level: str) -> list[_Child]:
    from petal_locate.server.settings import node_token_env_var

    children = []
    base_env = {k: v for k, v in os.environ.items() if not k.startswith("PETAL_NODE_TOKEN")}
    for name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS"):
        base_env.setdefault(name, "1")
    for site in cfg.sites:
        env = dict(base_env)
        env[node_token_env_var(site.id)] = tokens[site.id]
        argv = [
            sys.executable, "-m", "petal_locate.cli", "node",
            "--server", url,
            "--site", site.id,
            "--driver", driver,
            "--node-id", f"demo-{site.id}-{uuid.uuid4().hex[:8]}",
            "--exit-with-parent", str(os.getpid()),
            "--log-level", log_level,
        ]
        children.append(_Child(site.id, argv, env))
    return children


def _print_banner(lines: Sequence[tuple[str, str]], stream: IO[str] | None = None) -> None:
    out = stream or sys.stdout
    width = max(len(k) for k, _ in lines)
    out.write("PETAL Locate demonstration (SIMULATED data)\n")
    for key, value in lines:
        out.write(f"  {key.ljust(width)}  {value}\n")
    out.write("Press Ctrl-C to stop.\n")
    out.flush()


async def _demo(args: argparse.Namespace) -> int:
    from petal_locate.server.app import build_server, create_app
    from petal_locate.server.settings import Marking, Settings, generate_node_tokens

    config_path = Path(args.config) if args.config else data_path(DEFAULT_CONFIG)
    scenario_path = Path(args.scenario) if args.scenario else data_path(DEFAULT_SCENARIO)
    if config_path is None or scenario_path is None:
        raise UsageError("the demo configuration is not installed; pass --config and --scenario")
    cfg = _load_config(config_path)
    _load_scenario(scenario_path, cfg)
    if not cfg.sites:
        raise UsageError(f"{config_path}: the configuration has no sites")
    tokens = generate_node_tokens([site.id for site in cfg.sites])
    temporary = args.data_dir is None
    data_dir = Path(tempfile.mkdtemp(prefix="petal-locate-demo-")) if temporary else Path(args.data_dir)
    sock = _bind(args.host, args.port)
    port = int(sock.getsockname()[1])
    connect_host = "127.0.0.1" if args.host in ("0.0.0.0", "") else ("::1" if args.host == "::" else args.host)
    url_host = f"[{connect_host}]" if ":" in connect_host else connect_host
    presets = Path(args.presets_dir) if args.presets_dir else scenario_path.parent
    settings = Settings(
        config_path=config_path,
        scenario_path=scenario_path,
        data_dir=data_dir,
        host=args.host,
        port=port,
        node_tokens=tokens,
        marking=Marking(level=DEMO_MARKING_LEVEL, line=DEMO_MARKING_LINE),
        api_token=None,
    )
    app = create_app(settings, presets_dir=presets)
    server = build_server(app, settings, log_config=None, capture_signals=False)
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(sig, stop.set)
    serve = loop.create_task(server.serve(sockets=[sock]), name="petal-demo-server")
    children: list[_Child] = []
    code = EXIT_OK
    try:
        deadline = loop.time() + DEMO_START_TIMEOUT_S
        while not server.started:
            if serve.done() or stop.is_set() or loop.time() > deadline:
                break
            await asyncio.sleep(0.05)
        if not server.started:
            if stop.is_set():
                return EXIT_OK
            logger.error("the demo server did not start (see the messages above)")
            return EXIT_RUNTIME
        children = _demo_children(cfg, tokens, f"ws://{url_host}:{port}", args.driver, args.node_log_level)
        for child in children:
            await child.start()
        _print_banner(
            [
                ("Console:", f"http://{url_host}:{port}/"),
                ("Marking:", DEMO_MARKING_LINE),
                ("Config:", str(config_path)),
                ("Scenario:", f"{scenario_path} (simulated)"),
                ("Data:", f"{data_dir}" + (" (removed on exit)" if temporary else "")),
                ("Nodes:", f"{len(children)} ({args.driver} driver): " + ", ".join(c.site_id for c in children)),
            ]
        )
        announced = False
        while not stop.is_set():
            if serve.done():
                logger.error("the demo server stopped unexpectedly; stopping the nodes")
                code = EXIT_RUNTIME
                break
            for child in children:
                if child.process is not None and child.process.returncode is not None and not stop.is_set():
                    returncode = child.process.returncode
                    if child.restarts >= DEMO_MAX_RESTARTS:
                        if child.restarts == DEMO_MAX_RESTARTS:
                            logger.error("node process for %s keeps exiting (code %d); giving up on it",
                                         child.site_id, returncode, extra={"site_id": child.site_id})
                            child.restarts += 1
                        continue
                    child.restarts += 1
                    logger.warning("node process for %s exited with code %d; restarting it", child.site_id,
                                   returncode, extra={"site_id": child.site_id})
                    await child.start()
            if not announced and len(app.state.hub.online_sites()) == len(children):
                announced = True
                sys.stdout.write(f"All {len(children)} nodes are online.\n")
                sys.stdout.flush()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=0.5)
    finally:
        if children:
            await asyncio.gather(*(child.stop(DEMO_CHILD_STOP_S) for child in children), return_exceptions=True)
        server.should_exit = True
        try:
            await asyncio.wait_for(asyncio.shield(serve), timeout=20.0)
        except TimeoutError:
            logger.error("the demo server did not stop in time; cancelling it")
            server.force_exit = True
            serve.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await serve
        except Exception:  # the server task failed: reported, the demo still cleans up
            logger.exception("the demo server failed")
            code = EXIT_RUNTIME
        sock.close()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError, RuntimeError):
                loop.remove_signal_handler(sig)
        if temporary:
            shutil.rmtree(data_dir, ignore_errors=True)
    sys.stdout.write("Demonstration stopped.\n")
    sys.stdout.flush()
    return code


def cmd_demo(args: argparse.Namespace) -> int:
    """``petal-locate demo``: server plus one node process per site, over real WebSockets."""
    return asyncio.run(_demo(args))


# --------------------------------------------------------------------------- check


def check_report(cfg: Any, t_ns: int, scenario: Any = None) -> str:
    """The text of ``petal-locate check``: summary, satellites, look angles, monitors,
    link budgets of each carrier with a location and an emitter model, and warnings."""
    from petal_locate.core.timebase import iso_from_ns
    from petal_locate.server.api.rest import compute_link_budget, compute_look_angles, compute_subpoints

    out: list[str] = []
    channels = [(site, channel) for site in cfg.sites for channel in site.channels]
    out.append(f"Configuration {cfg.name!r} (version {cfg.version}) is valid.")
    first = cfg.description.strip().splitlines()[0] if cfg.description.strip() else ""
    if first:
        out.append(f"  {first}")
    out.append(
        f"  {len(cfg.satellites)} satellite(s), {len(cfg.sites)} site(s), {len(channels)} receive channel(s), "
        f"{len(cfg.carriers)} carrier(s); geometry at {iso_from_ns(t_ns)}"
    )
    if scenario is not None:
        out.append(f"  Scenario {scenario.name!r} fits it ({len(scenario.emitters)} emitter(s); SIMULATED).")
    out.append("")
    subpoints = {p.sat_id: p for p in compute_subpoints(cfg, t_ns)}
    out.append("Satellites")
    out.append(format_table(
        ["satellite", "name", "orbit", "sub-point lat", "sub-point lon", "transponders"],
        [
            [s.id, s.name, s.orbit.type,
             _number(subpoints[s.id].lat_deg, 2) if s.id in subpoints else "-",
             _number(subpoints[s.id].lon_deg, 2) if s.id in subpoints else "-",
             len(s.transponders)]
            for s in cfg.satellites
        ],
    ))
    out.append("")
    out.append("Look angles (every channel by every satellite; gain at the satellite's first downlink centre)")
    rows = []
    for row in compute_look_angles(cfg, t_ns):
        notes = []
        if not row.visible:
            notes.append("BELOW HORIZON")
        if row.electrically_small:
            notes.append("electrically small dish")
        rows.append([row.site_id, row.channel_id, row.sat_id, "yes" if row.watched else "no",
                     _number(row.az_deg), _number(row.el_deg), _number(row.range_km, 0),
                     _number(row.off_axis_deg), _number(row.gain_dbi), _number(row.peak_gain_dbi),
                     _number(row.freq_hz / 1e6, 3), "; ".join(notes)])
    out.append(format_table(
        ["site", "channel", "satellite", "watched", "az deg", "el deg", "range km", "off-axis deg",
         "gain dBi", "peak dBi", "freq MHz", "note"],
        rows,
    ))
    out.append("")
    out.append("Monitor windows (downlink)")
    out.append(format_table(
        ["site", "channel", "monitor", "centre MHz", "span kHz", "nfft", "navg", "frames/s", "bin Hz", "source"],
        [
            [site.id, channel.id, m.id, _number(m.center_hz / 1e6, 4), _number(m.span_hz / 1e3, 2), m.nfft,
             m.navg, _number(m.frame_rate_hz, 2), _number(m.bin_hz, 2),
             "derived" if channel.monitors_derived else "configured"]
            for site, channel in channels
            for m in channel.monitors
        ],
    ))
    out.append("")
    out.append("Link budgets of the carriers")
    any_carrier = False
    for carrier in cfg.carriers:
        if carrier.location is None or carrier.emitter is None:
            out.append(f"  carrier {carrier.id}: no location or emitter model; not computed")
            continue
        any_carrier = True
        loc = carrier.location
        out.append(
            f"  carrier {carrier.id} ({carrier.name}): {carrier.uplink_center_hz / 1e6:.4f} MHz, "
            f"{carrier.emitter.bandwidth_hz / 1e3:g} kHz {carrier.emitter.waveform}, EIRP "
            f"{carrier.emitter.eirp_dbw:g} dBW, at {loc.lat_deg:.3f}, {loc.lon_deg:.3f}"
            + (" (reference)" if carrier.use_as_reference else "")
        )
        budget = compute_link_budget(cfg, carrier.emitter, carrier.uplink_center_hz, loc.lat_deg, loc.lon_deg,
                                     loc.alt_m + loc.antenna_height_m, t_ns)
        if not budget:
            out.append("    no path: no watched satellite above both horizons relays it")
            continue
        out.append("    " + format_table(
            ["satellite", "transponder", "site", "channel", "up el", "down el", "C/N0 dBHz", "SNR dB", "monitor",
             "detectable"],
            [[r.sat_id, r.transponder_id, r.site_id, r.channel_id, _number(r.uplink_elevation_deg),
              _number(r.downlink_elevation_deg), _number(r.cn0_total_dbhz), _number(r.snr_db),
              r.monitor_id or "-", "yes" if r.detectable else "no"] for r in budget],
        ).replace("\n", "\n    "))
    if not cfg.carriers:
        out.append("  (no carriers configured)")
    elif not any_carrier:
        out.append("  (no carrier has a location and an emitter model)")
    out.append("")
    warnings = cfg.warnings(t_ns / 1e9)
    out.append(f"Warnings ({len(warnings)})")
    for warning in warnings:
        out.append(f"  {warning['loc']}: {warning['msg']}")
    if not warnings:
        out.append("  none")
    return "\n".join(out) + "\n"


def cmd_check(args: argparse.Namespace) -> int:
    """``petal-locate check``: validate and describe a configuration."""
    cfg = _load_config(args.config)
    scenario = _load_scenario(args.scenario, cfg) if args.scenario else None
    t_ns = _time_ns(args.time)
    sys.stdout.write(check_report(cfg, t_ns, scenario))
    sys.stdout.flush()
    return EXIT_OK


# --------------------------------------------------------------------------- accuracy


def accuracy_request(args: argparse.Namespace) -> Any:
    """The ``AccuracyRequest`` of ``petal-locate accuracy``: from ``--request FILE``
    (YAML or JSON, the body of ``POST /api/v1/analysis/accuracy``), with any flag given
    on the command line overriding the file."""
    from pydantic import ValidationError

    from petal_locate.server.api.schemas import AccuracyRequest

    data: dict[str, Any] = {}
    if args.request is not None:
        try:
            loaded = yaml.safe_load(Path(args.request).read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise UsageError(f"--request {args.request}: cannot be read ({exc})") from exc
        if not isinstance(loaded, dict):
            raise UsageError(f"--request {args.request}: must be a mapping")
        data.update(loaded)
    if args.pair:
        pairs = []
        for text in args.pair:
            a, sep, b = text.partition(",")
            if not sep or not a.strip() or not b.strip():
                raise UsageError(f"--pair: expected CHANNEL_A,CHANNEL_B, got {text!r}")
            pairs.append([a.strip(), b.strip()])
        data["pairs"] = pairs
    if args.f_e_hz is not None:
        data["f_e_hz"] = args.f_e_hz
    emitter = dict(data.get("emitter") or {})
    if args.eirp_dbw is not None:
        emitter["eirp_dbw"] = args.eirp_dbw
    if args.bandwidth_hz is not None:
        emitter["bandwidth_hz"] = args.bandwidth_hz
    if args.waveform is not None:
        emitter["waveform"] = args.waveform
    if emitter:
        data["emitter"] = emitter
    if args.step_deg is not None:
        data["step_deg"] = args.step_deg
    if args.kinds is not None:
        data["kinds"] = [k.strip() for k in args.kinds.split(",") if k.strip()]
    if args.reference_carrier is not None:
        data["reference_carrier_id"] = args.reference_carrier
    if args.integration_s is not None:
        data["integration_s"] = args.integration_s
    if args.time is not None:
        data["t"] = args.time
    if args.region is not None:
        parts = [p.strip() for p in args.region.split(",")]
        if len(parts) != 4:
            raise UsageError("--region: expected LAT_MIN,LAT_MAX,LON_MIN,LON_MAX")
        try:
            data["region"] = dict(zip(("lat_min", "lat_max", "lon_min", "lon_max"), (float(p) for p in parts)))
        except ValueError as exc:
            raise UsageError(f"--region: {exc}") from exc
    try:
        return AccuracyRequest.model_validate(data)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or '(root)'}: {e['msg']}" for e in exc.errors(include_url=False)
        )
        raise UsageError(f"accuracy request: {problems}") from exc


def cmd_accuracy(args: argparse.Namespace) -> int:
    """``petal-locate accuracy``: an accuracy map as JSON (``AccuracyGrid``)."""
    from petal_locate.server.api.rest import accuracy_bands, accuracy_problems, compute_accuracy

    cfg = _load_config(args.config)
    request = accuracy_request(args)
    errors = accuracy_problems(cfg, request)
    if errors:
        raise UsageError("; ".join(f"{e['loc']}: {e['msg']}" for e in errors))
    region = request.region or cfg.processing.region
    lat, lon, _bands = accuracy_bands(region, request.step_deg)
    cells = int(lat.size * lon.size)
    if cells > args.max_cells:
        raise UsageError(f"the grid has {cells} cells, more than --max-cells {args.max_cells}; "
                         "choose a coarser --step-deg or a smaller --region")
    t_ns = _time_ns(request.t)
    try:
        from petal_locate.core.accuracy import accuracy_map
    except ImportError as exc:
        logger.error("accuracy prediction is not available in this build: %s", exc)
        return EXIT_RUNTIME
    progress = _Progress(f"accuracy map ({cells} cells)")
    try:
        grid = compute_accuracy(accuracy_map, cfg, request, t_ns, progress=progress.update)
    except ValueError as exc:
        progress.finish()
        raise UsageError(f"the accuracy map cannot be computed: {exc}") from exc
    progress.finish()
    _write_output(grid.model_dump_json(), args.out)
    return EXIT_OK


# --------------------------------------------------------------------------- montecarlo


_PROGRESS_NAMES: Final[tuple[str, ...]] = ("progress", "progress_cb", "on_progress", "callback")
_DRAW_NAMES: Final[tuple[str, ...]] = ("draw_errors", "draw_truth_errors", "random_errors")


def _default_emitter(scenario: Any) -> str:
    candidates = [e.id for e in scenario.emitters if e.kind == "jammer" and e.enabled]
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise UsageError("the scenario has no enabled jammer; pass --emitter ID")
    raise UsageError(f"the scenario has several jammers ({', '.join(candidates)}); pass --emitter ID")


def montecarlo_summary(result: Mapping[str, Any]) -> str:
    """The scalar entries of a ``run_trials`` result (one level of nesting flattened,
    the ``summary`` block first, text cut at 60 characters) as a two-column table."""
    first: list[list[str]] = []
    rest: list[list[str]] = []

    def cell(value: Any) -> str:
        text = _number(_jsonable(value), 3)
        return text if len(text) <= 60 else text[:57] + "..."

    for key, value in result.items():
        if key == "description":
            continue
        rows = first if key == "summary" else rest
        if isinstance(value, Mapping):
            for sub, inner in value.items():
                if isinstance(inner, (list, tuple)):
                    rows.append([f"{key}.{sub}", f"[{len(inner)} values]"])
                elif not isinstance(inner, Mapping):
                    rows.append([f"{key}.{sub}", cell(inner)])
        elif isinstance(value, (list, tuple)):
            rows.append([str(key), f"[{len(value)} values]"])
        else:
            rows.append([str(key), cell(value)])
    return format_table(["quantity", "value"], first + rest)


def cmd_montecarlo(args: argparse.Namespace) -> int:
    """``petal-locate montecarlo``: IQ -> CAF -> solve statistics of one emitter."""
    cfg = _load_config(args.config)
    scenario = _load_scenario(args.scenario, cfg)
    emitter_id = args.emitter or _default_emitter(scenario)
    if emitter_id not in {e.id for e in scenario.emitters} | {c.id for c in cfg.carriers}:
        raise UsageError(f"--emitter: {emitter_id!r} is not an emitter of scenario {scenario.name!r}")
    if args.trials < 1:
        raise UsageError("--trials: must be at least 1")
    try:
        from petal_locate.sim.montecarlo import run_trials
    except ImportError as exc:
        logger.error("Monte Carlo trials are not available in this build: %s", exc)
        return EXIT_RUNTIME
    kwargs = _key_values(args.param or [], "--param")
    parameters = inspect.signature(run_trials).parameters
    accepts_any = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values())
    unknown = [k for k in kwargs if k not in parameters and not accepts_any]
    if unknown:
        raise UsageError(f"--param: run_trials takes no {', '.join(unknown)} (it takes {', '.join(parameters)})")
    if args.seed is not None:
        if "seed" not in parameters and not accepts_any:
            raise UsageError("--seed: run_trials of this build takes no seed")
        kwargs["seed"] = args.seed
    if args.draw_errors:
        name = next((n for n in _DRAW_NAMES if n in parameters), None)
        if name is None:
            raise UsageError("--draw-errors: run_trials of this build cannot draw truth errors")
        kwargs[name] = True
    progress = _Progress(f"Monte Carlo ({args.trials} trials, SIMULATED)")
    callback_name = next((n for n in _PROGRESS_NAMES if n in parameters), None)
    done = threading.Event()
    ticker: threading.Thread | None = None
    if callback_name is not None:
        kwargs[callback_name] = lambda *a, **k: progress.update(int(a[0]) if a else 0, args.trials)
    else:
        def tick() -> None:
            while not done.wait(1.0):
                progress.update(0, None)

        ticker = threading.Thread(target=tick, name="petal-mc-progress", daemon=True)
        ticker.start()
    started = time.monotonic()
    try:
        result = run_trials(cfg, scenario, emitter_id, args.trials, **kwargs)
    except ValueError as exc:
        raise UsageError(f"Monte Carlo: {exc}") from exc
    finally:
        done.set()
        if ticker is not None:
            ticker.join(2.0)
        progress.finish()
    elapsed = time.monotonic() - started
    data = _jsonable(result if isinstance(result, Mapping) else {"result": result})
    sys.stdout.write(
        f"Monte Carlo of emitter {emitter_id!r} (scenario {scenario.name!r}, SIMULATED): "
        f"{args.trials} trials in {elapsed:.1f} s\n"
    )
    if isinstance(data.get("description"), str):
        sys.stdout.write(data["description"] + "\n")
    sys.stdout.write(montecarlo_summary(data) + "\n")
    sys.stdout.flush()
    if args.out is not None:
        _write_output(json.dumps(data, indent=2, allow_nan=False), args.out)
    return EXIT_OK


# --------------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    """The argument parser of ``petal-locate``."""
    parser = argparse.ArgumentParser(
        prog="petal-locate",
        description="PETAL Locate: satellite-uplink interference detection and geolocation.",
    )
    parser.add_argument("--version", action="version", version=f"petal-locate {__version__}")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--log-level", default="info", choices=["debug", "info", "warning", "error"])
    common.add_argument("--log-format", default="text", choices=["text", "json"])
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    serve = sub.add_parser("serve", parents=[common], help="run the server",
                           description="Run the server: node hub, event pipeline, console API and console.")
    serve.add_argument("--config", required=True, help="system configuration (YAML or JSON)")
    serve.add_argument("--scenario", help="simulation scenario (YAML); without it the server does not simulate")
    serve.add_argument("--host", default="127.0.0.1", help="address to listen on (default 127.0.0.1)")
    serve.add_argument("--port", type=int, default=8080, help="TCP port (default 8080; 0 = any free port)")
    serve.add_argument("--data-dir", default="data", help="directory of the store and IQ captures (default ./data)")
    serve.add_argument("--node-tokens", metavar="FILE", help="YAML map of site id to node token")
    serve.add_argument("--api-token", metavar="T", help=f"console API token (or set {API_TOKEN_ENV})")
    serve.add_argument("--marking-level", metavar="L", help="marking level of the console banner, e.g. UNCLASSIFIED")
    serve.add_argument("--marking-line", metavar="TEXT", help="full banner text (default: the level)")
    serve.add_argument("--presets-dir", metavar="DIR", help="directory of preset scenarios")
    serve.add_argument("--access-log", action="store_true", help="log every HTTP request")
    serve.set_defaults(handler=cmd_serve)

    node = sub.add_parser("node", parents=[common], help="run a node agent beside a radio",
                          description="Run a node agent: dial out to the server, stream spectra, serve captures.")
    node.add_argument("--server", required=True, help="server URL, e.g. ws://server:8080 or https://server")
    node.add_argument("--site", required=True, help="site id of this node")
    node.add_argument("--token", help=f"node token (better: set PETAL_NODE_TOKEN_<SITE_ID> or {NODE_TOKEN_ENV})")
    node.add_argument("--driver", required=True, choices=["sim", "sigmf", "soapy", "noise"])
    node.add_argument("--node-id", help="node id (default: generated once and kept in the state file)")
    node.add_argument("--state-path", help="file keeping the node id between runs")
    node.add_argument("--driver-option", action="append", metavar="KEY=VALUE",
                      help="driver option (repeatable), e.g. --driver-option lookback_s=10")
    node.add_argument("--sigmf-path", help="recording to replay (--driver sigmf)")
    node.add_argument("--soapy-device", help="SoapySDR device arguments (--driver soapy)")
    node.add_argument("--clock", choices=["ntp", "gpsdo", "free", "sim"],
                      help="clock source to report (default: the agent's own, ntp)")
    node.add_argument("--clock-unlocked", action="store_true", help="report the clock as not locked")
    node.add_argument("--exit-with-parent", type=int, metavar="PID", help=argparse.SUPPRESS)
    node.set_defaults(handler=cmd_node)

    demo = sub.add_parser("demo", parents=[common], help="server plus one simulated node per site",
                          description="Run the server with the example configuration and scenario and one "
                                      "'petal-locate node' process per site over real WebSockets (SIMULATED).")
    # The shipped files resolve to the source tree's config/ or the installed package's data/config/.
    demo.add_argument("--config", help=f"configuration (default {data_path(DEFAULT_CONFIG) or 'config/' + DEFAULT_CONFIG})")
    demo.add_argument("--scenario",
                      help=f"scenario (default {data_path(DEFAULT_SCENARIO) or 'config/' + DEFAULT_SCENARIO})")
    demo.add_argument("--host", default="127.0.0.1", help="address to listen on (default 127.0.0.1)")
    demo.add_argument("--port", type=int, default=8080, help="TCP port (default 8080; 0 = any free port)")
    demo.add_argument("--data-dir", help="keep the store here (default: a temporary directory removed on exit)")
    demo.add_argument("--presets-dir", metavar="DIR", help="preset scenarios (default: the scenario's directory)")
    demo.add_argument("--driver", default="sim", choices=["sim", "noise"],
                      help="node driver (default sim; noise only for plumbing checks)")
    demo.add_argument("--node-log-level", default="warning", choices=["debug", "info", "warning", "error"])
    demo.set_defaults(handler=cmd_demo)

    check = sub.add_parser("check", parents=[common], help="validate and describe a configuration",
                           description="Validate a configuration and print look angles, monitor windows, "
                                       "carrier link budgets and warnings.")
    check.add_argument("--config", required=True,
                       help=f"system configuration (examples in {data_path('.') or 'config/'})")
    check.add_argument("--scenario", help="also check a scenario against the configuration")
    check.add_argument("--time", help="ISO 8601 UTC time of the geometry (default now)")
    check.set_defaults(handler=cmd_check)

    acc = sub.add_parser("accuracy", parents=[common], help="accuracy map to JSON",
                         description="Predicted 95 %% error over a grid (MATH.md section 10) as JSON.")
    acc.add_argument("--config", required=True)
    acc.add_argument("--request", metavar="FILE", help="request document (YAML or JSON) as for the API")
    acc.add_argument("--pair", action="append", metavar="A,B", help="receive-channel pair (repeatable)")
    acc.add_argument("--f-e-hz", type=float, help="emitter uplink frequency, Hz")
    acc.add_argument("--eirp-dbw", type=float, help="emitter EIRP, dBW")
    acc.add_argument("--bandwidth-hz", type=float, help="emitter bandwidth, Hz")
    acc.add_argument("--waveform", choices=["cw", "noise", "chirp", "psk"])
    acc.add_argument("--step-deg", type=float, help="grid step, degrees (default 2)")
    acc.add_argument("--region", metavar="LAT_MIN,LAT_MAX,LON_MIN,LON_MAX", help="default: the configured region")
    acc.add_argument("--kinds", metavar="tdoa,fdoa", help="measurement kinds (default both)")
    acc.add_argument("--reference-carrier", metavar="ID", help="assume calibration against this reference carrier")
    acc.add_argument("--integration-s", type=float, help="capture duration, s (default: configured)")
    acc.add_argument("--time", help="ISO 8601 UTC (default now)")
    acc.add_argument("--max-cells", type=int, default=200_000, help="largest grid allowed (default 200000)")
    acc.add_argument("--out", help="output file (default standard output)")
    acc.set_defaults(handler=cmd_accuracy)

    mc = sub.add_parser("montecarlo", parents=[common], help="Monte Carlo IQ -> CAF -> solve statistics",
                        description="Run sim.montecarlo.run_trials (SIMULATED) and print the summary.")
    mc.add_argument("--config", required=True)
    mc.add_argument("--scenario", required=True)
    mc.add_argument("--trials", type=int, required=True)
    mc.add_argument("--emitter", help="emitter id (default: the scenario's only jammer)")
    mc.add_argument("--seed", type=int)
    mc.add_argument("--draw-errors", action="store_true", help="draw truth errors from the configured uncertainty")
    mc.add_argument("--param", action="append", metavar="KEY=VALUE", help="further run_trials argument")
    mc.add_argument("--out", help="write the full result as JSON here")
    mc.set_defaults(handler=cmd_montecarlo)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point of ``petal-locate``; returns the exit code."""
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # argparse: --help / --version (0) or a usage error (2)
        return exc.code if isinstance(exc.code, int) else EXIT_USAGE
    configure_logging(args.log_level, args.log_format, access_log=getattr(args, "access_log", False))
    handler: Callable[[argparse.Namespace], int] = args.handler
    try:
        return handler(args)
    except UsageError as exc:
        sys.stderr.write(f"petal-locate {args.command}: {exc}\n")
        return EXIT_USAGE
    except KeyboardInterrupt:
        return EXIT_OK
    except Exception:
        logger.exception("petal-locate %s failed", args.command)
        return EXIT_RUNTIME


if __name__ == "__main__":
    sys.exit(main())
