"""Check which provider keys can actually reach the target site.

A key that returns 200 on /v1/projects proves nothing: free-tier accounts answer
free reads even at zero balance, and a cloud browser can start while the target
still blocks its exit IP. The only honest test is to open a real browser, load
the page, and look at what came back.

    python probe_reach.py                 # every transport, every key
    python probe_reach.py --transport kernel
    python probe_reach.py --json
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
from typing import Any

from config import load_dotenv, provider_keys
from transports import TransportError, build_transports, open_browser

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

TARGET = os.getenv("PROBE_TARGET") or "https://my.yomoni.fr/sign-in"
# The sign-in page is only "reached" if the form actually rendered: a Cloudflare
# interstitial or an error page also returns HTTP 200.
READY_SELECTOR = 'input[type="email"], input[type="password"]'
CHALLENGE_MARKERS = ("just a moment", "cf_chl_", "checking your browser", "attention required")


async def probe_once(transport: Any, key: str) -> dict[str, Any]:
    out: dict[str, Any] = {
        "transport": transport.name,
        "key": f"***{key[-6:]}",
        "target": TARGET,
    }
    try:
        async with open_browser(transport, key) as browser:
            ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            page.set_default_timeout(45_000)

            response = await page.goto(TARGET, wait_until="domcontentloaded", timeout=90_000)
            out["status"] = response.status if response else None

            # The sign-in form only exists once React hydrates, which takes
            # ~8s: sampling the DOM earlier reports a blank page that is really
            # a healthy one. Wait for the signal instead of sleeping.
            with contextlib.suppress(Exception):
                await page.wait_for_selector(READY_SELECTOR, timeout=45_000)
            await page.wait_for_timeout(2_000)

            html = await page.content()
            title = await page.title()
            out["title"] = title
            out["final_url"] = page.url
            out["challenge"] = any(m in html.lower() or m in title.lower() for m in CHALLENGE_MARKERS)
            out["form_present"] = await page.locator(READY_SELECTOR).count() > 0

            if out["challenge"]:
                out["state"] = "blocked"
            elif out["form_present"]:
                out["state"] = "reaches"
            else:
                out["state"] = "unclear"
    except TransportError as exc:
        out["state"] = "exhausted" if not exc.retryable else "transient_error"
        out["error"] = str(exc)
    except Exception as exc:  # noqa: BLE001 - a probe must never abort the sweep
        out["state"] = "transient_error"
        out["error"] = f"{type(exc).__name__}: {exc}"[:200]
    return out


def _line(entry: dict[str, Any]) -> str:
    mark = {
        "reaches": "REACHES",
        "blocked": "BLOCKED",
        "exhausted": "NO CREDIT",
        "transient_error": "ERROR   ",
        "unclear": "UNCLEAR ",
    }.get(entry["state"], "UNKNOWN ")
    bits = [f"[{mark}] {entry['transport']:<11} {entry['key']}"]
    if entry.get("status"):
        bits.append(f"http={entry['status']}")
    if entry.get("final_url"):
        bits.append(f"url={entry['final_url'][:60]}")
    if entry.get("title"):
        bits.append(f"title={entry['title'][:40]!r}")
    if entry.get("form_present"):
        bits.append("form=ok")
    if entry.get("error"):
        bits.append(f"| {entry['error']}")
    return " ".join(bits)


async def main() -> int:
    load_dotenv(os.path.join(BASE_DIR, ".env"))

    wanted = None
    if "--transport" in sys.argv:
        wanted = sys.argv[sys.argv.index("--transport") + 1]

    key_sets = {
        "browserbase": provider_keys("BROWSERBASE_API_KEY"),
        "kernel": provider_keys("KERNEL_API_KEY"),
    }
    if wanted:
        key_sets = {k: v for k, v in key_sets.items() if k == wanted}

    transports = build_transports(key_sets)
    if not transports:
        print("no keys configured for the requested transport(s)")
        return 1

    print(f"target: {TARGET}\n")
    findings: list[dict[str, Any]] = []
    for transport in transports:
        for key in transport.keys():
            entry = await probe_once(transport, key)
            findings.append(entry)
            if "--json" not in sys.argv:
                print(_line(entry), flush=True)

    if "--json" in sys.argv:
        print(json.dumps(findings, indent=2))
    else:
        reaching = [f for f in findings if f["state"] == "reaches"]
        blocked = [f for f in findings if f["state"] == "blocked"]
        print()
        print(f"reaches: {len(reaching)}  blocked: {len(blocked)}  total: {len(findings)}")
        by_transport: dict[str, list[str]] = {}
        for entry in findings:
            by_transport.setdefault(entry["transport"], []).append(entry["state"])
        for name, states in by_transport.items():
            good = states.count("reaches")
            print(f"  {name}: {good}/{len(states)} keys reach the site")

    # Non-zero only when nothing works at all, so this is usable as a gate.
    return 0 if any(f["state"] == "reaches" for f in findings) else 1


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        sys.exit(asyncio.run(main()))
