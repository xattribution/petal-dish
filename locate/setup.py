"""Build step that pyproject.toml cannot express: ship the example configurations.

The example system configurations and simulation scenarios live in ``config/`` at the
project root, where the documentation and the tests refer to them. A built wheel
carries a copy under ``petal_locate/data/config/``, which is where
``petal_locate.cli.data_path`` looks when the package is installed (``petal-locate
demo`` with no arguments, the console's preset scenarios). An editable install reads
``config/`` from the source tree and needs no copy. Everything else is in
pyproject.toml.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py

ROOT = Path(__file__).resolve().parent
CONFIG_DIR = ROOT / "config"
PACKAGE = "petal_locate"
CONFIG_TARGET = ("data", "config")


class BuildPyWithConfig(build_py):
    """``build_py`` that starts from an empty package directory and adds ``config/``."""

    def run(self) -> None:
        editable = bool(getattr(self, "editable_mode", False))
        package_dir = Path(self.build_lib) / PACKAGE
        if not editable and package_dir.exists():
            # build/lib survives between in-tree builds; without this, console assets
            # (hashed names that change on every UI build) and removed modules from an
            # earlier build would be packed into the new wheel.
            shutil.rmtree(package_dir)
        super().run()
        if editable:
            return
        sources = sorted(p for p in CONFIG_DIR.rglob("*") if p.is_file() and p.suffix in (".yaml", ".yml", ".json"))
        if not sources:
            raise RuntimeError(f"no example configuration found under {CONFIG_DIR}")
        target = package_dir.joinpath(*CONFIG_TARGET)
        for source in sources:
            dest = target / source.relative_to(CONFIG_DIR)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, dest)


setup(cmdclass={"build_py": BuildPyWithConfig})
