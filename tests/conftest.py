"""Shared pytest configuration.

Puts the repo root on ``sys.path`` so ``custom_components.wind_forecast_fi``
is importable (needed for the relative imports inside fingrid.py/fmi.py to
resolve when those modules are loaded standalone below). No Home Assistant
plugin registered here — these are the pure tests; the HA-side test plugin is
registered in ``tests/ha/conftest.py`` once that suite exists (later phase).
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
