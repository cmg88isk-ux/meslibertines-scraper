"""Cloud browser transports, behind one interface.

Every transport does the same thing: trade an API key for a live Chrome, and
expose it as a CDP endpoint Playwright can drive. The account walker therefore
does not know or care which provider a session came from, which is what makes
the provider set swappable and the fan-out trivially concurrent.

  Browserbase  POST /v1/sessions            -> connectUrl
               POST /v1/sessions/{id}       {status: REQUEST_RELEASE}
  Kernel       POST /browsers               -> cdp_ws_url
               DELETE /browsers/{id}

Both are plain REST calls, so neither needs a vendor SDK at runtime.

Credentials rotate through the *_2, *_3, ... naming that config.provider_keys
understands, so adding a key to .env is enough to widen the pool.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

BROWSERBASE_ROOT = "https://api.browserbase.com"
KERNEL_ROOT = "https://api.onkernel.com"

# The Kernel stealth plan and the Browserbase proxies both cost money per GB, so
# every session is created with recording and logging off.
_BROWSER_SETTINGS = {"solveCaptchas": True, "recordSession": False, "logSession": False}

DEFAULT_HEADLESS = True
DEFAULT_TIMEOUT_S = 900


class TransportError(RuntimeError):
    """A transport could not produce a session.

    ``retryable`` separates "this key is out of credits / blocked" from "the
    network glitched", which is what decides whether an account may be retried.
    """

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass
class Session:
    """A live browser, plus everything needed to shut it down."""

    provider: str
    key: str
    session_id: str
    cdp_url: str
    _release: Any = field(default=None, repr=False)

    async def close(self) -> None:
        if self._release is None:
            return
        with contextlib.suppress(Exception):
            await self._release()
        self._release = None


class Transport(Protocol):
    """What the walker needs from a provider."""

    name: str

    def keys(self) -> list[str]:
        """Usable keys, most-recently-added last."""
        ...

    async def open(self, key: str) -> Session:
        """Start a browser, or raise TransportError."""
        ...


def _classify(status: int, body: str) -> TransportError:
    """Turn an HTTP failure into a TransportError with the right retry semantics.

    402 and billing-flavoured 401/403 mean the key is out of credits: retrying
    the same account on the same key is pointless until the plan resets, so the
    error is not retryable. Everything else (5xx, timeouts, 429) is transient.
    """
    blob = (body or "").lower()
    billing = (
        "credit",
        "payment required",
        "insufficient",
        "billing",
        "quota exceeded",
        "limit reached",
        "plan",
        "suspend",
    )
    if status == 402 or (status in (401, 403) and any(h in blob for h in billing)):
        return TransportError(f"out of credits: HTTP {status} {body[:160]}", retryable=False)
    if status in (401, 403):
        return TransportError(f"key rejected: HTTP {status} {body[:160]}", retryable=False)
    if status == 429:
        return TransportError(f"rate limited: HTTP {status} {body[:160]}", retryable=True)
    return TransportError(f"HTTP {status} {body[:160]}", retryable=True)


class BrowserbaseTransport:
    """Browserbase cloud Chrome."""

    name = "browserbase"

    def __init__(
        self, keys: list[str], *, timeout_s: int = DEFAULT_TIMEOUT_S, settings: dict[str, Any] | None = None
    ) -> None:
        self._keys = keys
        self.timeout_s = timeout_s
        # Per-session overrides merged into browserSettings, e.g. a random
        # fingerprint so each attempt does not reuse the same one.
        self.settings = settings or {}

    def keys(self) -> list[str]:
        return list(self._keys)

    async def open(self, key: str) -> Session:
        async with httpx.AsyncClient(timeout=60.0) as http:
            try:
                listing = await http.get(
                    f"{BROWSERBASE_ROOT}/v1/projects", headers={"X-BB-API-Key": key}
                )
                if listing.status_code != 200:
                    raise _classify(listing.status_code, listing.text)
                projects = listing.json()
                if not projects:
                    raise TransportError("no project on this account", retryable=False)
                project_id = projects[0]["id"]

                created = await http.post(
                    f"{BROWSERBASE_ROOT}/v1/sessions",
                    headers={"X-BB-API-Key": key, "Content-Type": "application/json"},
                    json={
                        "projectId": project_id,
                        # Only the keys Browserbase understands go into
                        # browserSettings; identity hints (platform/locale/...)
                        # ride alongside for apply_identity and must not be sent
                        # as unknown fields here.
                        "browserSettings": {
                            **_BROWSER_SETTINGS,
                            **{k: v for k, v in self.settings.items() if k in ("fingerprint", "viewport")},
                        },
                        "timeout": self.timeout_s,
                    },
                )
            except httpx.HTTPError as exc:
                raise TransportError(f"network error: {exc}", retryable=True) from exc

            if created.status_code not in (200, 201):
                raise _classify(created.status_code, created.text)

            body = created.json()
            session_id = body["id"]

            async def release() -> None:
                async with httpx.AsyncClient(timeout=30.0) as rel:
                    await rel.post(
                        f"{BROWSERBASE_ROOT}/v1/sessions/{session_id}",
                        headers={"X-BB-API-Key": key},
                        json={"status": "REQUEST_RELEASE"},
                    )

            return Session(self.name, key, session_id, body["connectUrl"], release)


class KernelTransport:
    """Kernel stealth Chrome, driven over CDP.

    Uses the REST API directly rather than the ``kernel`` SDK: the SDK is only
    needed for Kernel's raw HTTP-replay transport, which cannot run a login form.
    """

    name = "kernel"

    def __init__(
        self, keys: list[str], *, timeout_s: int = DEFAULT_TIMEOUT_S, settings: dict[str, Any] | None = None
    ) -> None:
        self._keys = keys
        self.timeout_s = timeout_s
        # Extra fields merged into the create payload (locale, timezone, ...).
        self.settings = settings or {}

    def keys(self) -> list[str]:
        return list(self._keys)

    async def open(self, key: str) -> Session:
        async with httpx.AsyncClient(timeout=60.0) as http:
            try:
                created = await http.post(
                    f"{KERNEL_ROOT}/browsers",
                    headers={"Authorization": f"Bearer {key}"},
                    json={
                        "headless": DEFAULT_HEADLESS,
                        # Stealth is what makes the exit IP and TLS fingerprint
                        # survive the target's bot checks; the CAPTCHA solver
                        # comes with it.
                        "stealth": True,
                        "timeout_seconds": self.timeout_s,
                        **self.settings,
                    },
                )
            except httpx.HTTPError as exc:
                raise TransportError(f"network error: {exc}", retryable=True) from exc

            if created.status_code not in (200, 201):
                raise _classify(created.status_code, created.text)

            body = created.json()
            session_id = body["session_id"]
            cdp_url = body.get("cdp_ws_url")
            if not cdp_url:
                raise TransportError("kernel returned no cdp_ws_url", retryable=True)

            async def release() -> None:
                async with httpx.AsyncClient(timeout=30.0) as rel:
                    await rel.delete(
                        f"{KERNEL_ROOT}/browsers/{session_id}",
                        headers={"Authorization": f"Bearer {key}"},
                    )

            return Session(self.name, key, session_id, cdp_url, release)


def build_transports(key_sets: dict[str, list[str]]) -> list[Transport]:
    """Instantiate one transport per provider that has at least one key.

    Providers are tried in the given order, so put the cheapest/most reliable
    first; the walker rotates within a provider before moving to the next.
    """
    built: list[Transport] = []
    for name, keys in key_sets.items():
        if not keys:
            continue
        if name == "browserbase":
            built.append(BrowserbaseTransport(keys))
        elif name == "kernel":
            built.append(KernelTransport(keys))
    return built


@contextlib.asynccontextmanager
async def open_browser(transport: Transport, key: str) -> Any:
    """Yield a Playwright browser connected over CDP, always releasing it."""
    from playwright.async_api import async_playwright

    session = await transport.open(key)
    pw = await async_playwright().start()
    browser = None
    try:
        browser = await pw.chromium.connect_over_cdp(session.cdp_url, timeout=120_000)
        yield browser
    finally:
        if browser is not None:
            with contextlib.suppress(Exception):
                await browser.close()
        with contextlib.suppress(Exception):
            await pw.stop()
        await session.close()
