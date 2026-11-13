"""Reading and writing system configurations (ARCHITECTURE.md section 3).

``load_config`` reads YAML (``.yaml`` / ``.yml``) or JSON (``.json``) into a validated
``SystemConfig``; ``dump_config`` writes YAML that loads back to an equal model;
``config_json_schema`` is the JSON schema of ``SystemConfig``.

Every failure to read, parse or validate is a ``ConfigError`` (a ``ValueError``) that
names the file and, for validation failures, the path of each field at fault.
"""
from __future__ import annotations

import json
import os
from collections.abc import Hashable
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from .models import SystemConfig, field_errors

__all__ = ["ConfigError", "load_config", "loads_config", "dump_config", "config_json_schema"]


class ConfigError(ValueError):
    """A configuration could not be read, parsed or validated.

    errors: one ``{"loc": "sites.0.lat_deg", "msg": "..."}`` entry per problem (empty
    for failures that are not about a field, such as a syntax error).
    """

    def __init__(self, message: str, errors: list[dict[str, str]] | None = None) -> None:
        super().__init__(message)
        self.errors: list[dict[str, str]] = list(errors or [])


class _UniqueKeyLoader(yaml.SafeLoader):
    """``yaml.SafeLoader`` that refuses a mapping with the same key twice.

    Plain YAML loading keeps the last of two equal keys and silently drops the first,
    which would lose a whole satellite or site without a word.
    """

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Hashable, Any]:
        seen: set[Hashable] = set()
        for key_node, _ in node.value:
            if key_node.tag == "tag:yaml.org,2002:merge":
                continue  # "<<": keys merged from an anchor may be overridden
            key = self.construct_object(key_node, deep=True)
            if isinstance(key, Hashable):
                if key in seen:
                    raise yaml.constructor.ConstructorError(
                        "while constructing a mapping",
                        node.start_mark,
                        f"found duplicate key {key!r}",
                        key_node.start_mark,
                    )
                seen.add(key)
        return super().construct_mapping(node, deep=deep)


def load_config(path: str | os.PathLike[str]) -> SystemConfig:
    """Read and validate a system configuration file.

    path: a ``.yaml`` / ``.yml`` or ``.json`` file (chosen by extension, case
    insensitive). Returns the validated ``SystemConfig`` with monitors derived for
    every channel that configures none.

    Raises ``ConfigError`` naming the file when it cannot be read, is not valid YAML or
    JSON, has an unsupported extension or fails validation (the message then lists each
    field path with its problem, also available as ``ConfigError.errors``).
    """
    file = Path(path)
    suffix = file.suffix.lower()
    if suffix in (".yaml", ".yml"):
        fmt = "yaml"
    elif suffix == ".json":
        fmt = "json"
    else:
        raise ConfigError(f"path {str(file)!r}: unsupported extension {file.suffix!r} (use .yaml, .yml or .json)")
    try:
        text = file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"path {str(file)!r}: cannot be read ({exc})") from exc
    return loads_config(text, fmt=fmt, source=str(file))


def loads_config(text: str, fmt: str = "yaml", source: str = "<string>") -> SystemConfig:
    """Parse and validate a system configuration held in a string.

    text: the document. fmt: ``"yaml"`` or ``"json"``. source: what to call the
    document in error messages. Raises ``ConfigError`` like ``load_config``.
    """
    if fmt == "yaml":
        try:
            data = yaml.load(text, Loader=_UniqueKeyLoader)  # noqa: S506 - a SafeLoader subclass
        except (yaml.YAMLError, ValueError) as exc:  # ValueError: e.g. an impossible unquoted date
            raise ConfigError(f"{source}: not valid YAML: {exc}") from exc
    elif fmt == "json":
        try:
            data = json.loads(text, object_pairs_hook=_unique_object)
        except ValueError as exc:
            raise ConfigError(f"{source}: not valid JSON: {exc}") from exc
    else:
        raise ConfigError(f"fmt must be 'yaml' or 'json', got {fmt!r}")
    if not isinstance(data, dict):
        raise ConfigError(f"{source}: the top level must be a mapping, got {type(data).__name__}")
    try:
        return SystemConfig.model_validate(data)
    except ValidationError as exc:
        errors = field_errors(exc)
        lines = "\n".join(f"  {error['loc']}: {error['msg']}" for error in errors)
        raise ConfigError(
            f"{source}: {len(errors)} configuration error{'s' if len(errors) != 1 else ''}\n{lines}", errors
        ) from exc


def dump_config(cfg: SystemConfig) -> str:
    """Serialise a configuration to YAML that ``load_config`` reads back to an equal model.

    Every field is written, defaults included, in model order; unset optional fields
    and derived monitors are left out (the latter are derived again on load).
    """
    if not isinstance(cfg, SystemConfig):
        raise ValueError(f"cfg must be a SystemConfig, got {type(cfg).__name__}")
    return yaml.safe_dump(
        cfg.export_dict(),
        sort_keys=False,
        default_flow_style=None,
        allow_unicode=True,
        width=100,
    )


def config_json_schema() -> dict[str, Any]:
    """JSON schema (draft 2020-12) of ``SystemConfig``, as served by ``GET /config/schema``."""
    return SystemConfig.model_json_schema()


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"duplicate key {key!r}")
        out[key] = value
    return out
