"""Async client for the HyCube "CubeConnect" local API.

Documented endpoints (API Manual v1.0, 2019), verified against HyWeb 2.065:
  GET /auth/        -> token (base64 JSON with "Permission" and "exp")
  GET /info/        -> controller / machine / serial / versions
  GET /get_values/  -> realtime power, voltage, current, SoC
Undocumented, used by the HyWeb UI:
  GET /data_row/    -> status flags (manual charging, EPS, wallboxes, ...)
  GET /db_today/?day=YYYY-MM-DD -> 5-minute average powers of one day
  GET /smartCharging/ManualChargingActivation/?value=<soc>|'' -> grid charging
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from datetime import date
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import aiohttp

from .const import REQUEST_SPACING, REQUEST_TIMEOUT, TOKEN_REFRESH_MARGIN

_LOGGER = logging.getLogger(__name__)

# Tokens per (base url, credentials), shared by all client instances so a
# reload or the config flow does not request a new one.
_TOKENS: dict[tuple[str, str], tuple[str, float]] = {}


class HyCubeError(Exception):
    """Base error."""


class HyCubeConnectionError(HyCubeError):
    """Device not reachable or returned garbage."""


class HyCubeAuthError(HyCubeError):
    """Credentials rejected."""


class HyCubeBusyError(HyCubeConnectionError):
    """Controller refuses connections ("Too many connections")."""


class HyCubeApi:
    """Minimal, token-caching client.

    The controller only copes with very few connections (its backend answers
    "Too many connections", the MySQL error). Every request therefore runs
    under one lock, so the pool reuses a single keep-alive socket instead of
    opening a new one per poll, requests are spaced out, and the token is
    shared across reloads instead of calling /auth/ again.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        host: str,
        username: str,
        password: str,
    ) -> None:
        self._session = session
        self._base = f"http://{host.strip().rstrip('/')}"
        self._auth_header = base64.b64encode(
            f"Basic {username}:{password}".encode()
        ).decode()
        self._lock = asyncio.Lock()
        self._last_request = 0.0
        self._cache_key = (self._base, self._auth_header)

    @property
    def _token(self) -> str | None:
        return _TOKENS.get(self._cache_key, (None, 0.0))[0]

    @_token.setter
    def _token(self, token: str | None) -> None:
        if token is None:
            _TOKENS.pop(self._cache_key, None)
        else:
            _TOKENS[self._cache_key] = (token, _token_expiry(token))

    @property
    def _token_exp(self) -> float:
        return _TOKENS.get(self._cache_key, (None, 0.0))[1]

    @property
    def host(self) -> str:
        """Base URL of the device."""
        return self._base

    # --- auth -----------------------------------------------------------------
    async def _ensure_token(self) -> str:
        if self._token and time.time() < self._token_exp - TOKEN_REFRESH_MARGIN:
            return self._token
        text = await self._raw_get("/auth/", {"Authorization": self._auth_header})
        token = text.strip().strip('"')
        if not token or token.lower() == "unauthorized":
            raise HyCubeAuthError("HyCube rejected the credentials")
        self._token = token
        return token

    # --- transport --------------------------------------------------------------
    async def _raw_get(
        self,
        path: str,
        headers: dict[str, str],
        params: dict[str, str] | None = None,
    ) -> str:
        for attempt in (1, 2):
            wait = self._last_request + REQUEST_SPACING - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request = time.monotonic()
            try:
                async with self._session.get(
                    self._base + path,
                    headers=headers,
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
                    allow_redirects=False,
                ) as resp:
                    text = await resp.text()
                    if "too many connections" in text.lower():
                        raise HyCubeBusyError(f"{path}: too many connections")
                    if resp.status in (401, 403):
                        raise HyCubeAuthError(f"{path}: HTTP {resp.status}")
                    if resp.status >= 400:
                        raise HyCubeConnectionError(f"{path}: HTTP {resp.status}")
                    return text
            except (aiohttp.ServerDisconnectedError, aiohttp.ClientOSError) as err:
                # A pooled keep-alive socket the controller already closed:
                # retry once on a fresh one before reporting an error.
                if attempt == 1:
                    continue
                raise HyCubeConnectionError(f"{path}: {err!r}") from err
            except (aiohttp.ClientError, asyncio.TimeoutError) as err:
                raise HyCubeConnectionError(f"{path}: {err!r}") from err
        raise HyCubeConnectionError(path)  # pragma: no cover

    async def _get_json(
        self, path: str, params: dict[str, str] | None = None
    ) -> Any:
        async with self._lock:
            for attempt in (1, 2):
                token = await self._ensure_token()
                try:
                    text = await self._raw_get(path, {"Authorization": token}, params)
                except HyCubeAuthError:
                    # Token expired early (e.g. device reboot) - retry once.
                    self._token = None
                    if attempt == 2:
                        raise
                    continue
                if text.strip().lower() == "unauthorized":
                    self._token = None
                    if attempt == 2:
                        raise HyCubeAuthError(f"{path}: unauthorized")
                    continue
                try:
                    return json.loads(text)
                except ValueError as err:
                    raise HyCubeConnectionError(
                        f"{path}: invalid JSON: {text[:120]!r}"
                    ) from err
        raise HyCubeConnectionError(path)  # pragma: no cover

    # --- endpoints --------------------------------------------------------------
    async def get_info(self) -> dict[str, Any]:
        """Device information."""
        return await self._get_json("/info/")

    async def get_values(self) -> dict[str, Any]:
        """Realtime values."""
        return await self._get_json("/get_values/")

    async def get_data_row(self) -> dict[str, Any]:
        """Status flags as used by the HyWeb dashboard."""
        return await self._get_json("/data_row/")

    async def get_day_stats(self, day: date) -> dict[str, Any]:
        """5-minute statistics of one day, as shown in the HyWeb charts."""
        data = await self._get_json("/db_today/", {"day": day.isoformat()})
        return data if isinstance(data, dict) else {}

    async def send_command(self, command: str) -> str:
        """Send a configured control request, e.g. '/path/?value=1'."""
        parts = urlsplit(command.strip())
        if parts.scheme or parts.netloc or not parts.path.startswith("/"):
            raise HyCubeError(f"Invalid command path: {command!r}")
        params = dict(parse_qsl(parts.query, keep_blank_values=True))
        async with self._lock:
            token = await self._ensure_token()
            text = await self._raw_get(parts.path, {"Authorization": token}, params)
        _LOGGER.debug("Command %s -> %s", command, text[:200])
        return text


def _token_expiry(token: str) -> float:
    """Read "exp" from the base64 JSON token; fall back to 10 minutes."""
    try:
        payload = json.loads(base64.b64decode(token + "=" * (-len(token) % 4)))
        return float(payload["exp"])
    except (ValueError, KeyError, TypeError):
        return time.time() + 600
