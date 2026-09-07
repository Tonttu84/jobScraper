"""Source adapters. Each module registers itself via ``register()`` on import."""

from __future__ import annotations

import importlib
import logging
import pkgutil

log = logging.getLogger(__name__)
_loaded = False


def _load_all() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    for mod in pkgutil.iter_modules(__path__):
        if mod.name.startswith("_") or mod.name == "base":
            continue
        try:
            importlib.import_module(f"{__name__}.{mod.name}")
        except Exception as exc:  # noqa: BLE001
            log.warning("source module %s failed to import: %s", mod.name, exc)
