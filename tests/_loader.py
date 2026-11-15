"""Import the integration's pure modules without Home Assistant.

The repository root is the integration package itself and holds modules named
like standard-library ones (select, profile), so it must not end up on
sys.path. It is registered as a package under another name instead; its
__init__.py, which imports Home Assistant, is never executed.
"""

import importlib
import sys
import types
from pathlib import Path

_PACKAGE = "hycube_under_test"

if _PACKAGE not in sys.modules:
    package = types.ModuleType(_PACKAGE)
    package.__path__ = [str(Path(__file__).resolve().parent.parent)]
    sys.modules[_PACKAGE] = package


def load(name: str) -> types.ModuleType:
    """Return the integration module `name`, e.g. "optimizer"."""
    return importlib.import_module(f"{_PACKAGE}.{name}")
