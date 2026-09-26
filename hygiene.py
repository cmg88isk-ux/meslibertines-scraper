"""Per-session hygiene: fresh identity, clean cookie jar, lean network.

Adapted from the anti-ban model of the sibling project vc-login, to a setting
where the browser lives in someone else's cloud instead of behind a Webshare
proxy. The three rules that matter and how they map here:

  fresh profile per account   -> we build a brand-new browser context, so no
                                 cookie, localStorage or sessionStorage ever
                                 crosses from one account to the next
  fresh UA per check          -> a desktop UA pinned to the Chrome build the
                                 session actually runs, injected with matching
                                 Client Hints, so UA and sec-ch-ua cannot
                                 disagree the way a blind override does
  assets that give the game away-> analytics/fonts/media are blocked over CDP,
                                 which both shrinks the fingerprint surface and
                                 makes each walk noticeably faster

IP rotation needs no code here: every Browserbase session and every Kernel
browser already leaves from its own IP, and the pool hands out a *different*
key whenever an account comes back blocked, which is a different IP.
"""

from __future__ import annotations

import contextlib
import json
import random
from typing import Any

# Analytics, tag managers, fonts and media tell a bot from a human faster than
# any header does, and none of them carry account data.
BLOCKED_URL_PATTERNS = [
    "*google-analytics.com*",
    "*googletagmanager.com*",
    "*doubleclick.net*",
    "*facebook.net*",
    "*hotjar.com*",
    "*sentry.io*",
    "*segment.io*",
    "*criteo.com*",
    "*taboola.com*",
    "*fonts.googleapis.com*",
    "*fonts.gstatic.com*",
    "*.mp4",
    "*.webm",
    "*.woff",
    "*.woff2",
    "*.ttf",
]

# Desktop-only on purpose: a member portal has no mobile layout worth faking,
# and a mobile UA on a desktop viewport is an easy contradiction to spot.
PLATFORMS = [
    ("Windows", "Windows NT 10.0; Win64; x64"),
    ("macOS", "Macintosh; Intel Mac OS X 10_15_7"),
    ("Windows", "Windows NT 10.0; Win64; x64"),
    ("Linux", "X11; Linux x86_64"),
]
LOCALES = ("fr-FR", "fr-FR", "fr-FR", "fr-BE", "fr-CH")
VIEWPORTS = [(1920, 1080), (1536, 864), (1660, 900), (1440, 900)]
TIMEZONES = ("Europe/Paris", "Europe/Paris", "Europe/Paris", "Europe/Brussels")

# Makes navigator.webdriver undefined, which is the single most checked flag.
STEALTH_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => __LOCALES__});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
window.chrome = window.chrome || {runtime: {}};
"""

#: A plausible language stack, most-preferred first. Deduplicated on purpose:
#: repeating "fr-FR" three times in navigator.languages looks synthetic.
LANGUAGE_STACK = ("fr-FR", "fr", "en-US", "en")


async def chrome_version(browser: Any) -> str:
    """Major version of the Chrome the cloud provider actually launched."""
    ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
    page = ctx.pages[0] if ctx.pages else await ctx.new_page()
    cdp = await ctx.new_cdp_session(page)
    try:
        info = await cdp.send("Browser.getVersion")
        return str(info.get("product", "")).split("/")[-1]
    finally:
        with contextlib.suppress(Exception):
            await cdp.detach()


async def apply_identity(browser: Any) -> dict[str, Any]:
    """Give this session its own plausible desktop identity.

    The UA string and the Client Hints must agree: servers compare
    ``User-Agent`` against ``sec-ch-ua`` and a mismatched pair is a stronger
    signal than either value on its own. Pinning both to the real build is what
    makes the override invisible.
    """
    version = await chrome_version(browser)
    major = version.split(".")[0] if version else "140"
    platform_name, platform_token = random.choice(PLATFORMS)
    locale = random.choice(LOCALES)
    width, height = random.choice(VIEWPORTS)
    timezone = random.choice(TIMEZONES)

    full_version = version if "." in version else f"{major}.0.0.0"
    ua = (
        f"Mozilla/5.0 ({platform_token}) AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{full_version} Safari/537.36"
    )
    brands = [
        {"brand": "Chromium", "version": major},
        {"brand": "Google Chrome", "version": major},
        {"brand": "Not=A?Brand", "version": "24"},
    ]
    platform_hint = {
        "Windows": "Windows",
        "macOS": "macOS",
        "Linux": "Linux",
    }[platform_name]

    ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
    page = ctx.pages[0] if ctx.pages else await ctx.new_page()
    cdp = await ctx.new_cdp_session(page)
    with contextlib.suppress(Exception):
        await cdp.send(
            "Emulation.setUserAgentOverride",
            {
                "userAgent": ua,
                "acceptLanguage": locale,
                "platform": platform_name,
                "userAgentMetadata": {
                    "brands": brands,
                    "fullVersion": full_version,
                    "fullVersionList": brands,
                    "platform": platform_hint,
                    "platformVersion": "10.0.0",
                    "architecture": "x86",
                    "model": "",
                    "mobile": False,
                    "bitness": "64",
                    "wow64": False,
                },
            },
        )
    with contextlib.suppress(Exception):
        await cdp.send("Network.setBlockedURLs", {"urls": BLOCKED_URL_PATTERNS})
    with contextlib.suppress(Exception):
        await cdp.detach()
    return {"ua": ua, "version": full_version, "platform": platform_name, "locale": locale}


async def new_clean_context(browser: Any, locale: str = "fr-FR") -> Any:
    """A context with no inherited cookies: the flush between two accounts.

    Reusing the default context would carry the previous account's session
    cookies, ``remember me`` flag and localStorage straight into the next login,
    which both breaks isolation and is exactly the kind of stateful oddity a
    fraud engine scores on.
    """
    ctx = await browser.new_context(
        locale=locale,
        timezone_id=random.choice(TIMEZONES),
        viewport={"width": 1920, "height": 1080},
        java_script_enabled=True,
        bypass_csp=False,
        service_workers="block",
    )
    await ctx.clear_cookies()
    await ctx.add_init_script(STEALTH_SCRIPT.replace("__LOCALES__", json.dumps(list(LANGUAGE_STACK))))
    # Third-party storage is the other way a previous identity leaks forward.
    with contextlib.suppress(Exception):
        await ctx.clear_permissions()
    return ctx
