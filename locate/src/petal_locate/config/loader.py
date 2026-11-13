"""Reading and writing system configurations (ARCHITECTURE.md section 3).

``load_config`` reads YAML (``.yaml`` / ``.yml``) or JSON (``.json``) into a validated
``SystemConfig``; ``dump_config`` writes YAML that loads back to an equal model;
``config_json_schema`` is the JSON schema of ``SystemConfig``.

Every failure to read, parse or validate is a ``ConfigError`` (a ``ValueError``) that
names the file and, for validation failures, the path of each field at fault.

``safe_yaml_load`` is the YAML reader of every document the package accepts from
outside (configurations, scenarios, node-token files, request and node files of the
command line): a ``yaml.SafeLoader`` that refuses a mapping with the same key twice,
a document that nests deeper than the interpreter can follow, and one whose anchors
and aliases expand to more than ``MAX_YAML_NODES`` nodes (a few kilobytes of aliases
can otherwise stand for billions of nodes, each validated in turn).
"""
from __future__ import annotations

import json
import os
from collections.abc import Hashable
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from ..protocol.messages import unencodable_path
from .models import SystemConfig, field_errors

__all__ = [
    "MAX_YAML_NODES",
    "ConfigError",
    "YamlLimitError",
    "config_json_schema",
    "dump_config",
    "load_config",
    "loads_config",
    "safe_yaml_load",
    "unicode_problem",
]

MAX_YAML_NODES: int = 1_000_000
"""Most nodes a YAML document may stand for, every use of an alias counted with the
whole subtree it names (a module variable, so that a deployment can tune it). The
shipped configurations have about a thousand."""


class YamlLimitError(yaml.YAMLError):
    """A YAML document refused for its shape rather than its syntax: nested too deeply,
    a recursive alias, or more than ``MAX_YAML_NODES`` nodes once aliases are expanded."""


def unicode_problem(data: Any) -> dict[str, str] | None:
    """Why decoded YAML or JSON data cannot be accepted as text, as ``{"loc", "msg"}``,
    or None: a string or key holding an unpaired UTF-16 surrogate (an escape such as
    ``"\\ud800"``), which no reply, log or store could write out again. ``loc`` is the
    dotted path (unencodable characters escaped)."""
    where = unencodable_path(data)
    if where is None:
        return None
    safe = where.encode("utf-8", "backslashreplace").decode("utf-8")[:120]
    return {"loc": safe, "msg": "text that is not valid Unicode (an unpaired surrogate)"}


def _children(node: yaml.Node) -> list[yaml.Node]:
    if isinstance(node, yaml.MappingNode):
        return [part for pair in node.value for part in pair]
    if isinstance(node, yaml.SequenceNode):
        return list(node.value)
    return []


def _check_expansion(root: yaml.Node, limit: int) -> None:
    """Raise ``YamlLimitError`` when the node graph (aliases share nodes) expands to more
    than ``limit`` nodes, or holds a cycle (a recursive alias). Iterative: any depth."""
    sizes: dict[int, int] = {}
    open_nodes: set[int] = set()
    stack: list[tuple[yaml.Node, bool]] = [(root, False)]
    while stack:
        node, finished = stack.pop()
        key = id(node)
        if finished:
            open_nodes.discard(key)
            total = 1 + sum(sizes[id(child)] for child in _children(node))
            if total > limit:
                raise YamlLimitError(
                    f"the document expands to more than {limit} nodes once its aliases are followed "
                    f"(near line {node.start_mark.line + 1}); write the repeated parts out or use fewer aliases"
                )
            sizes[key] = total
            continue
        if key in sizes:
            continue
        open_nodes.add(key)
        stack.append((node, True))
        for child in _children(node):
            child_key = id(child)
            if child_key in open_nodes:
                raise YamlLimitError(f"recursive alias near line {child.start_mark.line + 1}")
            if child_key not in sizes:
                stack.append((child, False))


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

    def compose_document(self) -> yaml.Node:  # type: ignore[override]
        node = super().compose_document()
        _check_expansion(node, MAX_YAML_NODES)
        return node


def safe_yaml_load(text: str) -> Any:
    """Parse one YAML document safely (see the module docstring).

    Raises ``yaml.YAMLError`` for invalid YAML, a duplicate key, and (as
    ``YamlLimitError``) a document nested too deeply, with a recursive alias or with
    more than ``MAX_YAML_NODES`` nodes once aliases are followed. ``ValueError`` comes
    through for scalars PyYAML cannot build (an impossible unquoted date).
    """
    loader = _UniqueKeyLoader(text)
    try:
        return loader.get_single_data()
    except RecursionError:
        raise YamlLimitError("the document is nested too deeply") from None
    finally:
        loader.dispose()


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
            data = safe_yaml_load(text)
        except YamlLimitError as exc:
            raise ConfigError(f"{source}: refused: {exc}", [{"loc": "(root)", "msg": str(exc)}]) from None
        except (yaml.YAMLError, ValueError) as exc:  # ValueError: e.g. an impossible unquoted date
            raise ConfigError(f"{source}: not valid YAML: {exc}") from exc
    elif fmt == "json":
        try:
            data = json.loads(text, object_pairs_hook=_unique_object)
        except RecursionError:
            message = "the document is nested too deeply"
            raise ConfigError(f"{source}: refused: {message}", [{"loc": "(root)", "msg": message}]) from None
        except ValueError as exc:
            raise ConfigError(f"{source}: not valid JSON: {exc}") from exc
    else:
        raise ConfigError(f"fmt must be 'yaml' or 'json', got {fmt!r}")
    if not isinstance(data, dict):
        raise ConfigError(f"{source}: the top level must be a mapping, got {type(data).__name__}")
    problem = unicode_problem(data)
    if problem is not None:
        raise ConfigError(f"{source}: 1 configuration error\n  {problem['loc']}: {problem['msg']}", [problem])
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
