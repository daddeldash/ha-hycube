"""Run the integration inside a real Home Assistant.

Needs pytest-homeassistant-custom-component. Home Assistant looks for custom
integrations in the package `custom_components`; the repository root is the
integration itself, so it is linked into a temporary directory that is added
to that package's search path.
"""

import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse

from common import CHARGE, DAY_STATS, SERIAL, STATUS, VALUES, prices

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


@pytest.fixture
def api():
    """The device."""
    with (
        patch("custom_components.hycube.HyCubeApi") as cls,
        patch("custom_components.hycube.coordinator.HISTORY_REQUEST_SPACING", 0),
    ):
        device = cls.return_value
        device.host = "http://192.0.2.1"
        device.get_info = AsyncMock(return_value={"HYCUBE_SERIAL": SERIAL})
        device.get_values = AsyncMock(return_value=VALUES)
        status = dict(STATUS)

        async def send_command(command: str) -> str:
            # The device reports grid charging the way it was last set.
            if command.startswith(CHARGE):
                target = command.removeprefix(CHARGE)
                status["manualChargingActivation"] = "1" if target else "0"
                if target:
                    status["manualChargingSoc"] = target
            return "ok"

        device.get_data_row = AsyncMock(side_effect=lambda: dict(status))
        device.get_day_stats = AsyncMock(return_value=DAY_STATS)
        device.send_command = AsyncMock(side_effect=send_command)
        yield device


@pytest.fixture
def tibber(hass: HomeAssistant):
    """The Tibber service; the returned dict is what it answers."""
    answer = prices(cheap_hours=3)

    async def get_prices(call: ServiceCall) -> dict:
        return answer

    hass.services.async_register(
        "tibber", "get_prices", get_prices, supports_response=SupportsResponse.ONLY
    )
    return answer
