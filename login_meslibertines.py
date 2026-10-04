"""Brute-force login for one personal meslibertines.com account.

Cloudflare fronts /users/login/ with an interactive Turnstile widget, so an
HTTP replay of the form is pointless: the challenge has to be answered by a
real browser with a non-flagged exit IP. Three ways to get there, chosen with
``--via``:

  local        a persistent Chromium on this machine, optionally behind
               ML_PROXY. Cheap, but a datacenter IP usually stays stuck on the
               challenge.
  browserbase  cloud Chrome with solveCaptchas, driven over CDP. One API key in
               BROWSERBASE_API_KEY is enough.
  kernel       stealth cloud Chrome with its own CAPTCHA solver, KERNEL_API_KEY.

The cloud paths reuse transports.py, so keys, retry and the CDP dance are the
same as the rest of the repo. Credentials come from ML_USERNAME / ML_PASSWORD in
.env, never hard-coded here: this is a personal tool, not the batch checker in
check_meslibertines.py.

Usage:
    python login_meslibertines.py --via kernel --tries 3     # most reliable
    python login_meslibertines.py --via browserbase
    python login_meslibertines.py                 # local, headed
    xvfb-run -a python login_meslibertines.py --headless   # local, no display

On a datacenter host, --via kernel is the reliable path: it reached the login
form on every session (6-9s), while Browserbase's solver cleared the challenge
only intermittently and then rate-limited. The browser's real user agent is
kept (no override) because forcing an old UA onto a newer Chrome makes the
Turnstile loop; the adult age-gate overlay is hidden so the form is reachable.

Exit codes: 0 logged in, 2 bad/absent credentials, 3 challenge unresolved.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import sys
from pathlib import Path
from typing import Any

from playwright.async_api import async_playwright

from config import load_dotenv, provider_keys
from transports import TransportError, build_transports, open_browser

ROOT = Path(__file__).resolve().parent
LOGIN_URL = "https://www.meslibertines.com/users/login/"

PROFILE_DIR = ROOT / "meslibertines-profile"
STATE_FILE = ROOT / "meslibertines-state.json"

# The username input is not stable across the site's templates, so the field is
# probed by every name it has carried rather than pinned to one selector. The
# labelled ones come first: an email box or a search box must not be mistaken
# for the login field.
USERNAME_SELECTORS = (
    'input#user',
    'input[name="data[user]"]',
    'input[name="username"]',
    'input[name="login"]',
    'input[name="user"]',
    'input[name="pseudo"]',
    'input[name="identifiant"]',
    'input[id="username"]',
    'input[id="login"]',
    'input[id="pseudo"]',
    'input[autocomplete="username"]',
    'input[name="email"]',
    'input[type="email"]',
    'input[type="text"]',
)

SUBMIT_TEXT = (
    "Se connecter",
    "Connexion",
    "Connexion à mon compte",
    "S'identifier",
    "Valider",
    "Login",
    "Sign in",
)

CHALLENGE_MARKERS = (
    "un instant",
    "just a moment",
    "vérification de sécurité",
    "checking your browser",
)

LOGGED_IN_MARKERS = (
    "déconnexion",
    "deconnexion",
    "log out",
    "logout",
    "se déconnecter",
    "mon compte",
    "mon profil",
)

# A Cloudflare interstitial on a residential IP usually resolves in a handful of
# seconds; on a flagged IP it never will, so the wait is capped rather than long.
CHALLENGE_SETTLE_S = 1
CHALLENGE_ROUNDS = 75

WEBSHARE_ROOT = "https://proxy.webshare.io/api/v2"


async def _title(page: Any) -> str:
    with contextlib.suppress(Exception):
        return (await page.title()) or ""
    return ""


async def _blocked(page: Any) -> bool:
    title = (await _title(page)).lower()
    return any(marker in title for marker in CHALLENGE_MARKERS)


async def clear_challenge(page: Any, *, manual: bool) -> bool:
    """Wait for the Turnstile interstitial to let the login form through.

    Returns True once the page is no longer the challenge. With ``manual`` the
    human solves the widget in the visible window; otherwise the solver is
    trusted to wave the browser through on its own.
    """
    if manual:
        print("Challenge visible: solve it in the browser window, then wait...")
        for _ in range(600):
            if not await _blocked(page):
                return True
            await page.wait_for_timeout(CHALLENGE_SETTLE_S * 1000)
        return not await _blocked(page)

    for round_index in range(CHALLENGE_ROUNDS):
        if not await _blocked(page):
            print(f"challenge cleared after {round_index}s")
            return True
        await page.wait_for_timeout(CHALLENGE_SETTLE_S * 1000)
        if round_index and round_index % 5 == 0:
            print(f"  waiting on Cloudflare... {round_index}s")
        if round_index == 4:
            for frame in page.frames:
                if "challenges.cloudflare.com" not in frame.url:
                    continue
                with contextlib.suppress(Exception):
                    await frame.locator("body").click(timeout=2_000)
    return not await _blocked(page)


async def dismiss_overlay(page: Any) -> None:
    """Hide the adult/age gate that covers the login form.

    MesLibertines renders a `#windiv-confirm` modal (plus a `.blurred-overlay`)
    over the page, so pointer actions on the form are intercepted. The gate only
    guards the visual layer here: hiding it lets the credentials be typed and
    the form submitted without answering the birthdate selector.
    """
    with contextlib.suppress(Exception):
        await page.evaluate(
            """() => {
                const gate = document.getElementById('windiv-confirm');
                if (gate) gate.style.display = 'none';
                document.querySelectorAll('.blurred-overlay').forEach((el) => {
                    el.style.display = 'none';
                    el.style.pointerEvents = 'none';
                });
            }"""
        )


async def find_username_field(page: Any) -> Any | None:
    for selector in USERNAME_SELECTORS:
        locator = page.locator(selector)
        if await locator.count():
            return locator.first
    return None


async def find_submit(page: Any) -> Any | None:
    for text in SUBMIT_TEXT:
        locator = page.get_by_role("button", name=text, exact=False)
        if await locator.count():
            return locator.first
    for selector in ('button[type="submit"]', 'input[type="submit"]'):
        locator = page.locator(selector)
        if await locator.count():
            return locator.first
    return None


async def logged_in(page: Any) -> bool:
    if "/users/login" not in page.url:
        return True
    for marker in LOGGED_IN_MARKERS:
        if await page.get_by_text(marker, exact=False).count():
            return True
    return False


async def rejected(page: Any) -> bool:
    """True once the site has printed its invalid-credentials message."""
    with contextlib.suppress(Exception):
        body = (await page.inner_text("body")).lower()
        return "invalide" in body or "incorrect" in body
    return False


async def attempt_login(page: Any, username: str, password: str) -> bool:
    """Type the credentials into whichever form template is live and submit."""
    await dismiss_overlay(page)
    field = await find_username_field(page)
    if field is None:
        print("no username field on the login page", file=sys.stderr)
        return False
    await field.fill(username)

    password_locator = page.locator('input[type="password"]')
    if await password_locator.count():
        await password_locator.first.fill(password)
        with contextlib.suppress(Exception):
            await password_locator.first.press("Enter")
    else:
        submit = await find_submit(page)
        if submit is None:
            print("no password field and no submit button", file=sys.stderr)
            return False
        await submit.click(force=True)
        await page.wait_for_selector('input[type="password"]', timeout=30_000)
        await page.locator('input[type="password"]').first.fill(password)
        with contextlib.suppress(Exception):
            await page.locator('input[type="password"]').first.press("Enter")

    # Click the real submit too: Enter sometimes only fires a hidden handler.
    submit = await find_submit(page)
    if submit is not None:
        with contextlib.suppress(Exception):
            await submit.click(force=True, timeout=5_000)

    for _ in range(30):
        await page.wait_for_timeout(1_500)
        if await logged_in(page):
            return True
        if "/users/login" not in page.url:
            return True
        # The site prints "Nom d'utilisateur ou mot de passe invalide!" on a
        # rejection. Bail out immediately instead of waiting the full minute:
        # a batch of wrong passwords would otherwise burn 45s each.
        if await rejected(page):
            return False
    return await logged_in(page)


async def fetch_webshare_proxy(token: str) -> dict[str, str] | None:
    """Pull the first working residential proxy off the Webshare account.

    The API does the picking: it returns the endpoint plus its own username and
    password, so only WEBSHARE_API_TOKEN has to be set. These are the residential
    exits Cloudflare expects, which is the whole point of paying for them.
    """
    import httpx

    async with httpx.AsyncClient(timeout=30) as http:
        try:
            response = await http.get(
                f"{WEBSHARE_ROOT}/proxy/list/",
                params={"mode": "direct", "page": 1, "page_size": 25},
                headers={"Authorization": f"Token {token}"},
            )
        except httpx.HTTPError as exc:
            print(f"webshare unreachable: {exc}", file=sys.stderr)
            return None
        if response.status_code != 200:
            print(f"webshare error HTTP {response.status_code}: {response.text[:160]}", file=sys.stderr)
            return None
        for proxy in response.json().get("results", []):
            if not proxy.get("valid", True):
                continue
            return {
                "server": f"http://{proxy['proxy_address']}:{proxy['port']}",
                "username": proxy["username"],
                "password": proxy["password"],
            }
    return None


def manual_proxy() -> dict[str, str] | None:
    """Build a proxy dict from the explicit env vars, or None if incomplete."""
    if os.environ.get("ML_PROXY"):
        return {"server": os.environ["ML_PROXY"]}
    user = os.environ.get("WEBSHARE_PROXY_USERNAME")
    password = os.environ.get("WEBSHARE_PROXY_PASSWORD")
    endpoint = os.environ.get("WEBSHARE_PROXY_ENDPOINT", "p.webshare.io:80")
    if user and password:
        return {"server": f"http://{endpoint}", "username": user, "password": password}
    return None


async def resolve_proxy() -> dict[str, str] | None:
    proxy = manual_proxy()
    if proxy:
        return proxy
    token = os.environ.get("WEBSHARE_API_TOKEN")
    if token:
        return await fetch_webshare_proxy(token)
    return None


async def login_on_page(page: Any, username: str, password: str, *, manual: bool, dump: bool) -> int:
    """Shared flow for both local and cloud browsers."""
    await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=120_000)
    if not await clear_challenge(page, manual=manual):
        print("challenge not cleared (flagged IP or headless)", file=sys.stderr)
        return 3

    ok = await attempt_login(page, username, password)
    if not ok:
        print("login rejected (wrong username/password?)", file=sys.stderr)
        return 2
    print(f"logged in as {username} -> {page.url}")
    if dump:
        print(f"session state saved to {STATE_FILE}")
    return 0


async def run_local(args: argparse.Namespace, username: str, password: str) -> int:
    headless = args.headless
    if not headless and not os.environ.get("DISPLAY"):
        print("no DISPLAY: run under xvfb-run, or pass --headless", file=sys.stderr)

    proxy = await resolve_proxy()
    # No user_agent override: the browser must report the UA it really is.
    # Forcing an older UA onto a newer Chrome is a fingerprint mismatch that
    # makes the Turnstile challenge loop instead of clearing.
    launch: dict[str, Any] = {
        "user_data_dir": str(PROFILE_DIR),
        "headless": headless,
        "locale": "fr-FR",
        "timezone_id": "Europe/Paris",
        "viewport": {"width": 1366, "height": 768},
        "args": ["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        "ignore_default_args": ["--enable-automation"],
    }
    if args.chrome:
        launch["channel"] = "chrome"
    if proxy:
        print(f"using proxy {proxy['server']}")
        launch["proxy"] = proxy

    async with async_playwright() as pw:
        context = await pw.chromium.launch_persistent_context(**launch)
        page = context.pages[0] if context.pages else await context.new_page()
        try:
            code = await login_on_page(
                page, username, password, manual=args.manual, dump=args.dump
            )
            await context.storage_state(path=str(STATE_FILE))
            if code == 0 and args.keep_open:
                print("browser held open; Ctrl-C to quit")
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.Event().wait()
            return code
        finally:
            with contextlib.suppress(Exception):
                await context.close()


async def run_cloud(args: argparse.Namespace, username: str, password: str) -> int:
    name = args.via
    prefix = {"browserbase": "BROWSERBASE_API_KEY", "kernel": "KERNEL_API_KEY"}[name]
    keys = provider_keys(prefix)
    if not keys:
        print(f"no {prefix} in .env", file=sys.stderr)
        return 2
    transports = build_transports({name: keys})

    # Cloudflare's challenge is state-dependent: the same key that clears in a
    # few seconds can sit on "Un instant..." for a full minute on the next
    # session. A fresh browser is cheap, so each key gets several tries before
    # the run gives up on it.
    tries = max(1, args.tries)
    last = 3
    for transport in transports:
        for key in transport.keys():
            label = f"{transport.name}:***{key[-6:]}"
            for attempt in range(1, tries + 1):
                try:
                    async with open_browser(transport, key) as browser:
                        # Reuse the provider's default context: creating a fresh
                        # one on top of a stealth CDP browser has been seen to
                        # crash the page on Kernel.
                        context = (
                            browser.contexts[0]
                            if browser.contexts
                            else await browser.new_context(
                                locale="fr-FR",
                                timezone_id="Europe/Paris",
                                viewport={"width": 1366, "height": 768},
                            )
                        )
                        page = context.pages[0] if context.pages else await context.new_page()
                        page.set_default_timeout(60_000)
                        # The cloud solver can help itself to a click, so manual is
                        # only meaningful for a window the user can actually see.
                        code = await login_on_page(
                            page,
                            username,
                            password,
                            manual=False,
                            dump=args.dump,
                        )
                        with contextlib.suppress(Exception):
                            await context.storage_state(path=str(STATE_FILE))
                        await context.close()
                        if code in (0, 2):
                            return code
                        last = code
                        print(f"  {label}: challenge not solved (try {attempt}/{tries})")
                except TransportError as exc:
                    print(f"  {label} unavailable ({exc})", file=sys.stderr)
                    last = 3
                    break
                except Exception as exc:  # noqa: BLE001 - one key must not kill the run
                    print(f"  {label} session lost: {type(exc).__name__} {exc}", file=sys.stderr)
                    last = 3
    return last


async def run(args: argparse.Namespace) -> int:
    load_dotenv(str(ROOT / ".env"))
    username = (os.environ.get("ML_USERNAME") or "").strip()
    password = os.environ.get("ML_PASSWORD") or ""
    if not username or not password:
        print("set ML_USERNAME and ML_PASSWORD in .env (see .env.example)", file=sys.stderr)
        return 2
    if args.via == "local":
        return await run_local(args, username, password)
    return await run_cloud(args, username, password)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--via",
        choices=("local", "browserbase", "kernel"),
        default=os.environ.get("ML_VIA", "local"),
        help="browser backend (default: $ML_VIA or local)",
    )
    parser.add_argument("--headless", action="store_true", help="hide the local window")
    parser.add_argument(
        "--chrome", action="store_true", help="local: drive the installed Google Chrome instead of Playwright Chromium"
    )
    parser.add_argument(
        "--manual", action="store_true", help="local only: pause so Turnstile can be solved by hand"
    )
    parser.add_argument("--dump", action="store_true", help="print where the session was saved")
    parser.add_argument(
        "--tries", type=int, default=3, help="fresh sessions per account before giving up (cloud)"
    )
    parser.add_argument("--keep-open", action="store_true", help="local only: stay open after login")
    args = parser.parse_args()
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
