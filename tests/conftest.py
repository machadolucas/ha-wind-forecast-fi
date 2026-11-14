"""Shared pytest configuration.

Puts the repo root on ``sys.path`` so ``custom_components.wind_forecast_fi``
is importable (needed for the relative imports inside fingrid.py/fmi.py to
resolve when those modules are loaded standalone below). The HA test plugin is
enabled only when both HA and its plugin are installed. HA-only autouse
fixtures remain in ``tests/ha/conftest.py``.
"""

from __future__ import annotations

import importlib
import importlib.util
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

# Keep pure tests runnable with pytest alone. Only import HA/plugin when both
# are installed; import HA first so its aliases are present before respx loads.
if (
    importlib.util.find_spec("homeassistant") is not None
    and importlib.util.find_spec("pytest_homeassistant_custom_component") is not None
):
    importlib.import_module("homeassistant")
    logger = importlib.import_module("homeassistant.components.logger")

    def _unused_old_logger_helper(*args: object) -> None:
        raise AssertionError("Old plugin logger helper is unsupported on HA 2026.11")

    if not hasattr(logger, "_clear_logger_overwrites"):
        logger._clear_logger_overwrites = _unused_old_logger_helper

    pytest_plugins = ["pytest_homeassistant_custom_component.plugins"]
else:
    pytest_plugins = []
