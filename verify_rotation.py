"""Prove the checker rotates exit IP and fingerprint on every session.

The anti-detection model claims two things per account attempt: a brand-new
cloud session (different exit IP) and a fresh plausibly-rotated identity (UA,
platform, locale, timezone, viewport). This opens N sessions the same way
check_meslibertines does and reports each observed value, so the claim is checked
against reality rather than assumed.

    python verify_rotation.py --sessions 4
    python verify_rotation.py --provider browserbase --sessions 4
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import sys
from pathlib import Path

from config import load_dotenv, provider_keys
from hygiene import apply_identity
from transports import BrowserbaseTransport, KernelTransport, open_browser

ROOT = Path(__file__).resolve().parent
KEY_PREFIX = {"kernel": "KERNEL_API_KEY", "browserbase": "BROWSERBASE_API_KEY"}


async def probe(provider: str, key: str) -> dict[str, str]:
    transport = KernelTransport([key]) if provider == "kernel" else BrowserbaseTransport([key])
    observed: dict[str, str] = {"provider": provider, "key": f"***{key[-6:]}"}
    try:
        async with open_browser(transport, key) as browser:
            context = browser.contexts[0] if browser.contexts else await browser.new_context()
            page = context.pages[0] if context.pages else await context.new_page()
            page.set_default_timeout(60_000)
            identity = await apply_identity(browser)
            observed.update(
                {
                    "platform": str(identity.get("platform", "")),
                    "locale": str(identity.get("locale", "")),
                    "timezone": str(identity.get("timezone", "")),
                    "ua": str(identity.get("ua", "")),
                }
            )
            with contextlib.suppress(Exception):
                await page.goto("https://api.ipify.org?format=json", wait_until="domcontentloaded", timeout=30_000)
                observed["ip"] = json.loads((await page.inner_text("body")).strip()).get("ip", "")
            with contextlib.suppress(Exception):
                observed["webdriver"] = await page.evaluate("() => navigator.webdriver")
                observed["languages"] = ",".join(await page.evaluate("() => navigator.languages"))
    except Exception as exc:  # noqa: BLE001
        observed["error"] = f"{type(exc).__name__}: {str(exc)[:120]}"
    return observed


async def main_async(provider: str, sessions: int) -> int:
    keys = provider_keys(KEY_PREFIX[provider])
    if not keys:
        print(f"no {KEY_PREFIX[provider]} in .env", file=sys.stderr)
        return 2
    rows: list[dict[str, str]] = []
    for i in range(sessions):
        key = keys[i % len(keys)]
        row = await probe(provider, key)
        rows.append(row)
        print(
            f"[{i + 1}/{sessions}] ip={row.get('ip', '?'):15} "
            f"platform={row.get('platform', '?'):8} locale={row.get('locale', '?'):6} "
            f"tz={row.get('timezone', '?'):15} webdriver={row.get('webdriver', '?'):8} "
            f"{row.get('error', '')}"
        )
        print(f"        ua={row.get('ua', '?')}")

    ips = [r["ip"] for r in rows if r.get("ip")]
    fps = [(r.get("platform"), r.get("locale"), r.get("timezone")) for r in rows]
    print(f"\nIP distinctes     : {len(set(ips))}/{len(ips)} -> {sorted(set(ips))}")
    print(f"Fingerprints distincts: {len(set(fps))}/{len(fps)}")
    leaked = [r for r in rows if r.get("webdriver") not in (None, False)]
    print(f"navigator.webdriver absent partout: {not leaked}")
    ok = len(set(ips)) == len(ips) and len(set(fps)) == len(fps) and not leaked
    print("\nRESULTAT:", "OK" if ok else "A CORRIGER")
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", default="kernel", choices=("kernel", "browserbase"))
    parser.add_argument("--sessions", type=int, default=4)
    args = parser.parse_args()
    load_dotenv(str(ROOT / ".env"))
    with contextlib.suppress(KeyboardInterrupt):
        return asyncio.run(main_async(args.provider, args.sessions))


if __name__ == "__main__":
    sys.exit(main())
