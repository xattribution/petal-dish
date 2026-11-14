"""Deployment settings of the server (ARCHITECTURE.md section 7).

`Settings` holds what belongs to one deployment rather than to the system
configuration: where the configuration and data live, where to listen, the bearer
tokens of the node sites, the optional console API token and the optional marking
banner. None of it is part of `SystemConfig` and none of it is ever inferred.

Node tokens come from three places, looked up per site in this order:

1. The node-tokens file `Settings.node_tokens_path` (`--node-tokens FILE`, a YAML map
   of site id to token, read by `load_node_tokens`). It is read again whenever it
   changes (its modification time, size or inode), at the next lookup, so a site added
   through `PUT /config` can be given a token by editing the file, without a restart.
   A file that no longer loads is logged and its last good content kept.
2. `Settings.node_tokens`, a map site_id -> token (what the file held at start-up when
   one is given; `generate_node_tokens` makes the random per-run tokens of
   `petal-locate demo`). Used when there is no file.
3. The environment variable `PETAL_NODE_TOKEN_<SITE_ID>`, where `<SITE_ID>` is the site
   id upper-cased with every character outside `A-Z`, `0-9` replaced by `_`
   (`node_token_env_var`). It is read at lookup time, but the environment of a running
   server cannot be changed, so it only serves sites known before the start.

A site with no token in any of them cannot connect. Tokens are compared in constant
time (`hmac.compare_digest` over SHA-256 digests, so that neither content nor length
shows in the timing) and are kept out of every `repr`.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import threading
import time
import re
import secrets
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, Final

import yaml  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator

from petal_locate.config.loader import safe_yaml_load
from petal_locate.protocol.messages import DEFAULT_HEARTBEAT_S, MAX_TIME_NS

__all__ = [
    "DEFAULT_CAPTURE_LIMIT_BYTES",
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "NODE_TOKEN_ENV_PREFIX",
    "Marking",
    "Settings",
    "generate_node_tokens",
    "load_node_tokens",
    "node_token_env_var",
    "node_tokens_from_env",
    "tokens_equal",
]

logger = logging.getLogger(__name__)

DEFAULT_CAPTURE_LIMIT_BYTES: Final[int] = 512 * 1024 * 1024
"""Default `Settings.capture_limit_bytes` (the store's own default as well)."""

LOCAL_HOST_NAMES: Final[tuple[str, ...]] = ("localhost", "127.0.0.1", "::1")
"""Host names every server accepts in `Host` besides the address it binds."""

DEFAULT_HOST: Final[str] = "127.0.0.1"
"""Address the server binds unless told otherwise (loopback only)."""

DEFAULT_PORT: Final[int] = 8080
"""TCP port the server binds unless told otherwise."""

NODE_TOKEN_ENV_PREFIX: Final[str] = "PETAL_NODE_TOKEN_"
"""Prefix of the environment variables that carry node tokens."""

_NOT_ENV_SAFE: Final[re.Pattern[str]] = re.compile(r"[^A-Za-z0-9]")
_ABSENT_TOKEN: Final[str] = secrets.token_urlsafe(32)
"""Stand-in compared against when a site has no token, so that an unknown site costs
the same time as a wrong token. Random per process and never accepted."""


def _digest(secret: str) -> bytes:
    """SHA-256 of a secret as UTF-8 (lone surrogates allowed: they arrive in JSON)."""
    return hashlib.sha256(secret.encode("utf-8", errors="surrogatepass")).digest()


def tokens_equal(expected: str, presented: str) -> bool:
    """Whether two secrets are equal, in time independent of where they differ.

    expected: the secret held by the server.
    presented: the secret a peer sent.
    Both are hashed first, so the comparison time depends on neither their content nor
    their length. Raises `TypeError` when either is not a string.
    """
    if not isinstance(expected, str) or not isinstance(presented, str):
        raise TypeError("tokens must be strings")
    return hmac.compare_digest(_digest(expected), _digest(presented))


def node_token_env_var(site_id: str) -> str:
    """Name of the environment variable that carries the token of a site.

    site_id: the configured site id (non-empty).
    Returns `PETAL_NODE_TOKEN_<SITE_ID>` with the site id upper-cased and every
    character outside `A-Z`, `0-9` replaced by an underscore: `tokyo-1` gives
    `PETAL_NODE_TOKEN_TOKYO_1`. Distinct site ids can share a variable (`a-b` and
    `a_b`); `node_tokens_from_env` refuses that.
    Raises `ValueError` for an empty or non-string site id.
    """
    if not isinstance(site_id, str) or not site_id:
        raise ValueError("site_id: must be a non-empty string")
    return NODE_TOKEN_ENV_PREFIX + _NOT_ENV_SAFE.sub("_", site_id).upper()


def node_tokens_from_env(
    site_ids: Iterable[str], environ: Mapping[str, str] | None = None
) -> dict[str, str]:
    """Collect the node tokens the environment holds for the given sites.

    site_ids: the configured site ids. The mapping from site id to variable name loses
        information, so the environment can only be searched for known sites.
    environ: the environment to read; `os.environ` when None.
    Returns site_id -> token for every site whose variable is set and not empty.
    Raises `ValueError` naming both sites when two of them with a token share one
    variable: they would silently share a secret.
    """
    env = os.environ if environ is None else environ
    found: dict[str, str] = {}
    owner: dict[str, str] = {}
    for site_id in site_ids:
        name = node_token_env_var(site_id)
        token = env.get(name)
        if not token:
            continue
        other = owner.get(name)
        if other is not None and other != site_id:
            raise ValueError(
                f"sites {other!r} and {site_id!r} both read their token from {name}; "
                "give them distinct tokens in the node-tokens file instead"
            )
        owner[name] = site_id
        found[site_id] = token
    return found


def load_node_tokens(path: str | os.PathLike[str]) -> dict[str, str]:
    """Read a node-tokens file: a YAML map of site id to token.

    path: the file given as `--node-tokens FILE`. An empty file is an empty map.
    Returns site_id -> token.
    Raises `ValueError` naming the file and the entry at fault when the file cannot be
    read, is not YAML, is not a map, or holds a site id or token that is not a
    non-empty string (quote tokens that look like numbers). Token values never appear
    in the error text.
    """
    file = Path(path)
    try:
        text = file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"node tokens file {str(file)!r}: cannot be read ({exc})") from exc
    try:
        data = safe_yaml_load(text)
    except (yaml.YAMLError, ValueError) as exc:
        # The parser's message can quote a line of the file, which holds secrets.
        mark = getattr(exc, "problem_mark", None)
        where = f" at line {mark.line + 1}" if mark is not None else ""
        raise ValueError(f"node tokens file {str(file)!r}: not valid YAML{where}") from None
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(
            f"node tokens file {str(file)!r}: must be a map of site id to token, "
            f"got {type(data).__name__}"
        )
    tokens: dict[str, str] = {}
    for site_id, token in data.items():
        if not isinstance(site_id, str) or not site_id:
            raise ValueError(
                f"node tokens file {str(file)!r}: site id {site_id!r} must be a non-empty string"
            )
        if not isinstance(token, str) or not token:
            raise ValueError(
                f"node tokens file {str(file)!r}: token of site {site_id!r} must be a "
                f"non-empty string, got {type(token).__name__} (quote it)"
            )
        tokens[site_id] = token
    return tokens


def generate_node_tokens(site_ids: Iterable[str]) -> dict[str, str]:
    """Random per-run tokens, one per site (what `petal-locate demo` uses).

    site_ids: the configured site ids.
    Returns site_id -> a fresh URL-safe token of 192 bits of entropy.
    """
    return {site_id: secrets.token_urlsafe(24) for site_id in site_ids}


class Marking(BaseModel):
    """Marking banner of the console; deployment configuration, never inferred.

    level: short marking level shown in the banner (decides its color in the console).
    line: the full banner text.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    level: str = Field(min_length=1)
    line: str = Field(min_length=1)


class Settings(BaseModel):
    """Everything the server needs to know about its deployment.

    config_path: system configuration file (YAML or JSON).
    scenario_path: simulation scenario file; None runs without the simulator.
    data_dir: directory for the SQLite store and the IQ captures.
    host: address to bind (default loopback).
    port: TCP port to bind, 0 to 65535; 0 lets the operating system choose.
    node_tokens: site_id -> bearer token of the node at that site. Sites absent here
        fall back to the environment (see the module docstring); a site with no token
        anywhere cannot connect.
    node_tokens_path: the node-tokens file; when set, its current content replaces
        `node_tokens` and is re-read whenever the file changes (no restart needed).
    api_token: when set, every `/api` request and the `/ws/ui` handshake must carry it.
        Without it the console interface is open to anything that can reach the
        server; the Host, Origin and request-type checks below still apply.
    allowed_hosts: further names the `Host` header of a request may carry, besides the
        bound address, `localhost`, `127.0.0.1` and `::1` (and, when the server binds
        every interface, this machine's host name and addresses). `*` allows any.
        Requests naming another host are answered 400 (DNS-rebinding defense).
    allowed_origins: further browser origins (`scheme://host[:port]`) that may open the
        console WebSocket `/ws/ui`, besides the server's own origin. `*` allows any.
    marking: banner for the console, or None for no banner.
    heartbeat_s: heartbeat period announced to nodes in `welcome`, seconds. A node
        silent for three periods is offline.
    hello_timeout_s: how long an accepted node socket may take to send a valid
        `hello`, seconds.
    capture_grace_s: how long past a capture's `deadline_ns` the server keeps waiting
        for the last chunk, seconds (network transit and clock difference).
    capture_limit_bytes: total size of the IQ capture files kept in
        `data_dir/captures`, bytes; the oldest are deleted beyond it (default 512 MiB).
    history_max_age_s: events not updated for longer than this (seconds) are deleted
        from the store with their history rows; None keeps them for ever.
    history_max_events: at most this many events are kept in the store (the most
        recently updated), with their history rows; None for no limit.
    sim_time_offset_ns: integer ns added to the host clock to make the server's time
        (0 by default). Only for simulation: `petal-locate demo --start` runs the
        simulated world from a fixed epoch by giving the server and every simulated
        node the same offset. A deployment with real nodes keeps 0.

    Frozen; tokens are left out of `repr`.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    config_path: Path
    scenario_path: Path | None = None
    data_dir: Path = Path("data")
    host: str = Field(default=DEFAULT_HOST, min_length=1)
    port: int = Field(default=DEFAULT_PORT, ge=0, le=65535)
    node_tokens: dict[str, str] = Field(default_factory=dict, repr=False)
    node_tokens_path: Path | None = None
    api_token: str | None = Field(default=None, repr=False)
    allowed_hosts: tuple[str, ...] = ()
    allowed_origins: tuple[str, ...] = ()
    marking: Marking | None = None
    heartbeat_s: float = Field(default=DEFAULT_HEARTBEAT_S, gt=0, allow_inf_nan=False)
    hello_timeout_s: float = Field(default=10.0, gt=0, allow_inf_nan=False)
    capture_grace_s: float = Field(default=1.0, ge=0, allow_inf_nan=False)
    capture_limit_bytes: int = Field(default=DEFAULT_CAPTURE_LIMIT_BYTES, gt=0)
    history_max_age_s: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    history_max_events: int | None = Field(default=None, ge=1)
    sim_time_offset_ns: int = 0

    _file_tokens: Any = PrivateAttr(default=None)
    """(stat key, tokens) of the node-tokens file as last read."""
    _file_lock: Any = PrivateAttr(default_factory=threading.Lock)

    @field_validator("node_tokens")
    @classmethod
    def _tokens_usable(cls, value: dict[str, str]) -> dict[str, str]:
        for site_id, token in value.items():
            if not site_id:
                raise ValueError("node_tokens: a site id is empty")
            if not token:
                raise ValueError(f"node_tokens: the token of site {site_id!r} is empty")
        return value

    @field_validator("api_token")
    @classmethod
    def _api_token_usable(cls, value: str | None) -> str | None:
        if value is not None and not value:
            raise ValueError("api_token: must not be empty; leave it out to disable the check")
        return value

    @field_validator("allowed_hosts", "allowed_origins", mode="before")
    @classmethod
    def _names(cls, value: Any) -> Any:
        if isinstance(value, str):
            value = [value]
        if isinstance(value, (list, tuple)):
            out = []
            for item in value:
                if not isinstance(item, str) or not item.strip():
                    raise ValueError("must be non-empty names")
                out.append(item.strip().lower().rstrip("/"))
            return tuple(out)
        return value

    @field_validator("sim_time_offset_ns")
    @classmethod
    def _offset_usable(cls, value: int) -> int:
        if not 0 <= time.time_ns() + value <= MAX_TIME_NS:
            raise ValueError("sim_time_offset_ns: the server time would fall outside 1970..2262")
        return value

    def now_ns(self) -> int:
        """The server's time: the host clock plus `sim_time_offset_ns`, integer UTC ns."""
        return time.time_ns() + self.sim_time_offset_ns

    def clock(self) -> Any:
        """A callable returning `now_ns()`: `time.time_ns` itself when there is no
        offset (what every component uses by default)."""
        if self.sim_time_offset_ns == 0:
            return time.time_ns
        offset = self.sim_time_offset_ns
        return lambda: time.time_ns() + offset

    def _tokens(self) -> Mapping[str, str]:
        """The site -> token map in force: the node-tokens file (re-read when it has
        changed since the last lookup) or `node_tokens`."""
        path = self.node_tokens_path
        if path is None:
            return self.node_tokens
        try:
            stat = os.stat(path)
            key: Any = (stat.st_mtime_ns, stat.st_size, stat.st_ino)
        except OSError as exc:
            key = ("missing", str(exc))
        with self._file_lock:
            cached = self._file_tokens
            if cached is not None and cached[0] == key:
                return cached[1]
            previous = cached[1] if cached is not None else dict(self.node_tokens)
            try:
                tokens = load_node_tokens(path)
            except ValueError as exc:
                logger.warning("node tokens file not reloaded, keeping the tokens it held before: %s", exc)
                tokens = previous
            else:
                if cached is not None:
                    logger.info("node tokens file reloaded: %d site(s)", len(tokens), extra={"path": str(path)})
            self._file_tokens = (key, tokens)
            return tokens

    def node_token(self, site_id: str, environ: Mapping[str, str] | None = None) -> str | None:
        """The token the node of a site must present, or None when the site has none.

        site_id: the site id a `hello` claims.
        environ: the environment to fall back on; `os.environ` when None.
        The node-tokens file (`node_tokens_path`, re-read when it changed) or
        `node_tokens` wins over the environment; an empty environment variable counts
        as unset.
        """
        if not isinstance(site_id, str) or not site_id:
            return None
        token = self._tokens().get(site_id)
        if token:
            return token
        env = os.environ if environ is None else environ
        return env.get(node_token_env_var(site_id)) or None

    def sites_without_token(
        self, site_ids: Iterable[str], environ: Mapping[str, str] | None = None
    ) -> list[str]:
        """The site ids of `site_ids` whose node has no token anywhere (it could not
        connect), in the order given."""
        return [site_id for site_id in site_ids if self.node_token(site_id, environ) is None]

    def check_node_token(
        self, site_id: str, presented: str, environ: Mapping[str, str] | None = None
    ) -> bool:
        """Whether `presented` is the token of `site_id`, compared in constant time.

        site_id: the site id a `hello` claims.
        presented: the token it carries.
        environ: the environment to fall back on; `os.environ` when None.
        False when the token is wrong and when the server holds no token for the site;
        the two cases take the same time.
        """
        if not isinstance(presented, str):
            return False
        expected = self.node_token(site_id, environ)
        if expected is None:
            tokens_equal(_ABSENT_TOKEN, presented)
            return False
        return tokens_equal(expected, presented)

    def check_api_token(self, presented: str | None) -> bool:
        """Whether a console request may proceed.

        presented: the bearer token of the request (or the `?token=` of the `/ws/ui`
            handshake); None when it carries none.
        True when no `api_token` is configured; otherwise only for the exact token,
        compared in constant time.
        """
        if self.api_token is None:
            return True
        if not isinstance(presented, str):
            tokens_equal(self.api_token, _ABSENT_TOKEN)
            return False
        return tokens_equal(self.api_token, presented)

    def with_node_tokens(self, tokens: Mapping[str, str]) -> Settings:
        """A copy whose `node_tokens` are replaced by `tokens` (validated)."""
        data: dict[str, Any] = {name: getattr(self, name) for name in type(self).model_fields}
        data["node_tokens"] = dict(tokens)
        return type(self)(**data)
