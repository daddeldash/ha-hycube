"""Run the integration inside a real Home Assistant.

Needs pytest-homeassistant-custom-component. Home Assistant looks for custom
integrations in the package `custom_components`; the repository root is the
integration itself, so it is linked into a temporary directory that is added
to that package's search path.
"""

import sys
import tempfile
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_PACKAGES = Path(tempfile.mkdtemp(prefix="hycube-test-"))
(_PACKAGES / "custom_components").mkdir()
(_PACKAGES / "custom_components" / "__init__.py").touch()
(_PACKAGES / "custom_components" / "hycube").symlink_to(_ROOT, target_is_directory=True)


@pytest.fixture(autouse=True)
def _custom_integrations(recorder_mock, enable_custom_integrations):
    """Let Home Assistant load the integration under test.

    The integration depends on the recorder, which has to be set up before
    the `hass` fixture.
    """
    try:
        # The test harness brings a `custom_components` package of its own.
        import custom_components
    except ModuleNotFoundError:
        sys.path.insert(0, str(_PACKAGES))
        return
    path = str(_PACKAGES / "custom_components")
    if path not in custom_components.__path__:
        custom_components.__path__.append(path)
