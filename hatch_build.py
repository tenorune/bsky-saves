"""Hatch custom build hook that vendors the GUI tarball before packaging.

Wires scripts/fetch_gui.py into `python -m build` and `pip install .` from
sdist. The wheel includes the populated src/bsky_saves/_gui/ tree via
[tool.hatch.build] artifacts (see pyproject.toml).
"""
from __future__ import annotations

import sys
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class GuiBuildHook(BuildHookInterface):
    PLUGIN_NAME = "custom"

    def initialize(self, version: str, build_data: dict) -> None:
        # Make scripts/ importable when this hook runs.
        sys.path.insert(0, str(Path(self.root)))
        from scripts.fetch_gui import fetch_gui

        fetch_gui(Path(self.root))
