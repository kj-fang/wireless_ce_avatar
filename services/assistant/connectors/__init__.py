"""
The assistant's connectors, one module per family.

`default_registry()` is the set a normal conversation gets. A family that
cannot load (an optional dependency missing on this PC) is left out and named
in `load_problems()` instead of taking the whole assistant down.
"""

from __future__ import annotations

from services.assistant.registry import Registry

# Module name under services.assistant.connectors -> loaded in this order,
# which is also the order the page lists them in.
_FAMILIES = ("cases", "mail", "echo", "tasks")

_problems: dict[str, str] = {}


def default_registry() -> Registry:
    """A fresh registry with every connector family that loads."""
    import importlib

    registry = Registry()
    _problems.clear()
    for family in _FAMILIES:
        try:
            module = importlib.import_module(f"{__name__}.{family}")
        except ModuleNotFoundError as e:
            # A family that does not exist yet is not a problem to report.
            if e.name != f"{__name__}.{family}":
                _problems[family] = f"{type(e).__name__}: {e}"
            continue
        except Exception as e:
            _problems[family] = f"{type(e).__name__}: {e}"
            continue
        for conn in module.connectors():
            registry.add(conn)
    return registry


def load_problems() -> dict[str, str]:
    """{family: why it did not load} from the last default_registry() call."""
    return dict(_problems)
