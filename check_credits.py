"""Report remaining credits/quota for every provider key in .env.

Endpoints used, all read-only and documented:
  Webshare    GET /api/v2/subscription/            free_credits, throttled, paused,
                                                  reactivation_date, end_date
              GET /api/v2/subscription/plan/<id>/  bandwidth_limit, proxy_type, proxy_count
              GET /api/v2/proxy/list/             do the DUCKAI_PROXY creds exist?
  Kernel      GET https://api.onkernel.com/org/limits
  Browserbase GET /v1/projects, GET /v1/projects/<id>/usage

Neither Kernel nor Browserbase publish a balance/credits endpoint (Kernel: 135
documented operations, none billing; Browserbase: the openapi spec has no such
path).  For those two a key can only be called ok / exhausted / dead, and
"exhausted" is inferred from a 402 or a billing-flavoured 401/403 body.
``--json`` dumps the raw findings.
"""

from __future__ import annotations

import calendar
import datetime
import json
import os
import re
import sys
from typing import Any

import httpx

from config import load_dotenv, provider_keys
from transports import BROWSERBASE_ROOT as API_ROOT

WEBSHARE_ROOT = "https://proxy.webshare.io/api/v2"
KERNEL_ROOT = "https://api.onkernel.com"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Out-of-credit is usually 402, or a 401/403 whose body talks about money.
_EXHAUSTED_HINTS = (
    "credit",
    "insufficient",
    "payment",
    "billing",
    "quota exceeded",
    "exceeded your",
    "no credit",
    "balance",
    "out of",
    "suspend",
    "limit reached",
    "plan limit",
)


def _mask(key: str, keep: int = 6) -> str:
    return f"***{key[-keep:]}" if len(key) > keep else "***"


def named_keys(prefix: str) -> list[tuple[str, str]]:
    """Same ordering as scrape_afer.provider_keys, but keeps the variable name.

    Needed to write findings back into .env / .env.bak, which is keyed by name.
    """
    found: list[tuple[int, str, str]] = []
    for name, value in os.environ.items():
        if name == prefix:
            index = 1
        elif name.startswith(f"{prefix}_") and name[len(prefix) + 1 :].isdigit():
            index = int(name[len(prefix) + 1 :])
        else:
            continue
        for key in (value or "").split(","):
            key = key.strip()
            if key:
                found.append((index, name, key))
    found.sort(key=lambda item: item[0])
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for _, name, key in found:
        if key not in seen:
            seen.add(key)
            out.append((name, key))
    return out


def monthly_anniversary(created: datetime.datetime, today: datetime.datetime) -> datetime.datetime:
    """Next monthly cycle boundary, clamping the day to the month's last day."""
    year, month = created.year, created.month
    while True:
        month += 1
        if month > 12:
            month = 1
            year += 1
        day = min(created.day, calendar.monthrange(year, month)[1])
        candidate = created.replace(year=year, month=month, day=day)
        if candidate > today:
            return candidate


def _reset_hint(provider: str, entry: dict[str, Any], today: datetime.datetime) -> tuple[str, str]:
    """Best available (date, source) for when this key works again.

    Only Webshare publishes real dates.  Browserbase and Kernel have no billing
    endpoint at all, so any date we produce there is an inference and is labelled
    as such.  An explicit <PREFIX>_RESET_DATE in the environment always wins.
    """
    override = os.getenv(f"{provider.upper()}_RESET_DATE")
    if override:
        return override, "manual override"

    if provider == "webshare":
        if entry.get("reactivation_date"):
            return entry["reactivation_date"], "api:subscription.reactivation_date"
        if entry.get("cycle_end"):
            return entry["cycle_end"], "api:subscription.end_date"

    if provider == "browserbase" and entry.get("project_created_at"):
        created = datetime.datetime.fromisoformat(entry["project_created_at"].replace("Z", "+00:00"))
        nxt = monthly_anniversary(created, today)
        return nxt.date().isoformat(), f"ESTIMATED from project createdAt {created.date()}"

    return "unknown", "no billing endpoint published by this provider"



def _body(response: httpx.Response) -> str:
    try:
        return json.dumps(response.json())[:400]
    except Exception:  # noqa: BLE001 - non-JSON error page
        return (response.text or "")[:400]


def _classify(response: httpx.Response) -> str:
    """ok / exhausted / dead / unknown from a single response."""
    status = response.status_code
    if status in (200, 201):
        return "ok"
    if status == 402:
        return "exhausted"
    if status in (401, 403, 429):
        blob = _body(response).lower()
        if any(hint in blob for hint in _EXHAUSTED_HINTS):
            return "exhausted"
        return "dead" if status == 401 else "unknown"
    return "unknown"


def check_webshare() -> list[dict[str, Any]]:
    tokens = named_keys("WEBSHARE_API_TOKEN")
    if not tokens:
        return [{"provider": "webshare", "state": "unknown", "detail": "no WEBSHARE_API_TOKEN"}]

    out: list[dict[str, Any]] = []
    want_user = os.getenv("WEBSHARE_PROXY_USERNAME")

    for env_var, token in tokens:
        with httpx.Client(timeout=30.0) as http:
            def get(path: str, _token: str = token) -> httpx.Response:
                return http.get(
                    f"{WEBSHARE_ROOT}{path}", headers={"Authorization": f"Token {_token}"}
                )

            try:
                response = get("/subscription/")
            except Exception as exc:  # noqa: BLE001 - network hiccup
                out.append(
                    {
                        "provider": "webshare",
                        "env_var": env_var,
                        "token": _mask(token),
                        "state": "unknown",
                        "detail": str(exc),
                    }
                )
                continue

            if response.status_code != 200:
                out.append(
                    {
                        "provider": "webshare",
                        "env_var": env_var,
                        "token": _mask(token),
                        "endpoint": "/subscription/",
                        "state": _classify(response),
                        "status": response.status_code,
                        "detail": _body(response),
                    }
                )
                continue

            sub = response.json()
            entry: dict[str, Any] = {
                "provider": "webshare",
                "env_var": env_var,
                "token": _mask(token),
                "endpoint": "/subscription/",
                "state": "ok",
                "free_credits_usd": sub.get("free_credits"),
                "term": sub.get("term"),
                "throttled": sub.get("throttled"),
                "paused": sub.get("paused"),
                "reactivation_date": sub.get("reactivation_date"),
                "cycle_end": sub.get("end_date"),
                "cycle_start": sub.get("start_date"),
                "renewals_enabled": sub.get("renewals_enabled"),
                "renewals_paid": sub.get("renewals_paid"),
                "failed_payment_times": sub.get("failed_payment_times"),
                "payment_method": sub.get("payment_method"),
            }
            # A paused or throttled subscription is the out-of-credits state.
            if sub.get("paused") or sub.get("throttled"):
                entry["state"] = "exhausted"
            out.append(entry)

            # Active plan: bandwidth allowance and what kind of proxies we own.
            plan_id = sub.get("plan")
            if plan_id:
                try:
                    plan = get(f"/subscription/plan/{plan_id}/")
                except Exception as exc:  # noqa: BLE001 - network hiccup
                    out.append(
                        {
                            "provider": "webshare",
                            "env_var": env_var,
                            "token": _mask(token),
                            "endpoint": "plan",
                            "state": "unknown",
                            "detail": str(exc),
                        }
                    )
                else:
                    if plan.status_code == 200:
                        data = plan.json()
                        entry["plan"] = {
                            "id": data.get("id"),
                            "status": data.get("status"),
                            "bandwidth_limit_gb": data.get("bandwidth_limit"),
                            "proxy_type": data.get("proxy_type"),
                            "proxy_subtype": data.get("proxy_subtype"),
                            "proxy_count": data.get("proxy_count"),
                            "monthly_price": data.get("monthly_price"),
                            "on_demand_refreshes_available": data.get("on_demand_refreshes_available"),
                            "proxy_replacements_available": data.get("proxy_replacements_available"),
                        }

            # Does the proxy URL we actually use belong to this account?
            try:
                listing = get("/proxy/list/?mode=direct&page=1&page_size=100")
            except Exception as exc:  # noqa: BLE001 - network hiccup
                out.append(
                    {
                        "provider": "webshare",
                        "env_var": env_var,
                        "token": _mask(token),
                        "endpoint": "proxy/list",
                        "state": "unknown",
                        "detail": str(exc),
                    }
                )
                continue
            if listing.status_code == 200:
                results = (listing.json() or {}).get("results", [])
                users = sorted({r.get("username") for r in results if r.get("username")})
                entry["proxy_usernames"] = users
                entry["proxy_count_listed"] = len(results)
                if want_user:
                    entry["proxy_user_configured"] = want_user
                    entry["proxy_user_in_account"] = want_user in users
            else:
                out.append(
                    {
                        "provider": "webshare",
                        "env_var": env_var,
                        "token": _mask(token),
                        "endpoint": "/proxy/list/",
                        "state": _classify(listing),
                        "status": listing.status_code,
                        "detail": _body(listing),
                    }
                )
    return out


def check_kernel() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for env_var, key in named_keys("KERNEL_API_KEY"):
        entry: dict[str, Any] = {"provider": "kernel", "env_var": env_var, "key": _mask(key)}
        try:
            with httpx.Client(timeout=30.0) as http:
                response = http.get(
                    f"{KERNEL_ROOT}/org/limits", headers={"Authorization": f"Bearer {key}"}
                )
        except Exception as exc:  # noqa: BLE001 - network hiccup
            entry.update(state="unknown", detail=str(exc))
            out.append(entry)
            continue

        entry["state"] = _classify(response)
        entry["status"] = response.status_code
        remaining = response.headers.get("X-RateLimit-Remaining")
        if remaining is not None:
            entry["rate_limit_remaining"] = remaining
        if response.status_code == 200:
            data = response.json()
            entry["max_concurrent_sessions"] = data.get("max_concurrent_sessions")
            entry["default_project_max_concurrent_sessions"] = data.get(
                "default_project_max_concurrent_sessions"
            )
            entry["credits_endpoint"] = "none (dashboard only)"
        else:
            entry["detail"] = _body(response)
        out.append(entry)
    return out


def check_browserbase() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for env_var, key in named_keys("BROWSERBASE_API_KEY"):
        entry: dict[str, Any] = {"provider": "browserbase", "env_var": env_var, "key": _mask(key)}
        try:
            with httpx.Client(timeout=30.0) as http:
                response = http.get(f"{API_ROOT}/v1/projects", headers={"X-BB-API-Key": key})
        except Exception as exc:  # noqa: BLE001 - network hiccup
            entry.update(state="unknown", detail=str(exc))
            out.append(entry)
            continue

        entry["state"] = _classify(response)
        entry["status"] = response.status_code
        if response.status_code == 200:
            projects = response.json()
            entry["projects"] = [p.get("id") for p in projects]
            if projects:
                entry["project_created_at"] = projects[0].get("createdAt")
            entry["credits_endpoint"] = "none (usage only)"
            for project in projects:
                try:
                    usage = http.get(
                        f"{API_ROOT}/v1/projects/{project['id']}/usage",
                        headers={"X-BB-API-Key": key},
                    )
                    if usage.status_code == 200:
                        data = usage.json()
                        entry["usage"] = {
                            "project": project.get("name") or project.get("id"),
                            "browser_minutes": data.get("browserMinutes"),
                            "proxy_mb": round((data.get("proxyBytes") or 0) / 1e6, 1),
                        }
                except Exception:  # noqa: BLE001 - usage is best-effort
                    pass
        else:
            entry["detail"] = _body(response)
        out.append(entry)
    return out


def probe_kernel(key: str) -> dict[str, Any]:
    """Create a throwaway browser to see whether the plan still has credits.

    Kernel signals "no credits left" with 403 ("insufficient permissions or
    plan") on POST /browsers; there is no 402 and no balance endpoint.  The
    session is deleted immediately so the meter barely moves.
    """
    out: dict[str, Any] = {"provider": "kernel", "probe": True}
    session_id = ""
    with httpx.Client(timeout=90.0) as http:
        headers = {"Authorization": f"Bearer {key}"}
        try:
            created = http.post(
                f"{KERNEL_ROOT}/browsers",
                headers=headers,
                json={"headless": True, "stealth": False, "timeout_seconds": 15},
            )
        except Exception as exc:  # noqa: BLE001 - network hiccup
            out.update(state="unknown", detail=str(exc))
            return out

        out["status"] = created.status_code
        if created.status_code in (200, 201):
            body = created.json()
            session_id = body.get("session_id", "")
            out["state"] = "ok"
            out["session_id"] = session_id
            out["memory"] = body.get("memory")
            out["region"] = body.get("region")
            out["uptime_ms_at_create"] = (body.get("usage") or {}).get("uptime_ms")
            out["detail"] = "browser created => plan has credits"
        else:
            out["state"] = _classify(created)
            out["detail"] = _body(created)

        if session_id:
            try:
                killed = http.delete(f"{KERNEL_ROOT}/browsers/{session_id}", headers=headers)
                out["released"] = f"HTTP {killed.status_code}"
            except Exception as exc:  # noqa: BLE001 - best-effort cleanup
                out["released"] = f"FAILED {exc}"
    return out


def probe_browserbase(key: str, project_id: str) -> dict[str, Any]:
    """Create a session then REQUEST_RELEASE it; 402/billing body means broke."""
    out: dict[str, Any] = {"provider": "browserbase", "probe": True}
    session_id = ""
    with httpx.Client(timeout=90.0) as http:
        headers = {"X-BB-API-Key": key, "Content-Type": "application/json"}
        try:
            created = http.post(
                f"{API_ROOT}/v1/sessions",
                headers=headers,
                json={
                    "projectId": project_id,
                    "browserSettings": {"solveCaptchas": False, "recordSession": False, "logSession": False},
                    "timeout": 60,
                },
            )
        except Exception as exc:  # noqa: BLE001 - network hiccup
            out.update(state="unknown", detail=str(exc))
            return out

        out["status"] = created.status_code
        if created.status_code in (200, 201):
            session_id = created.json().get("id", "")
            out["state"] = "ok"
            out["session_id"] = session_id
            out["detail"] = "session created => plan has credits"
        else:
            out["state"] = _classify(created)
            out["detail"] = _body(created)

        if session_id:
            try:
                released = http.post(
                    f"{API_ROOT}/v1/sessions/{session_id}",
                    headers=headers,
                    json={"status": "REQUEST_RELEASE"},
                )
                out["released"] = f"HTTP {released.status_code}"
            except Exception as exc:  # noqa: BLE001 - best-effort cleanup
                out["released"] = f"FAILED {exc}"
    return out


BAK_HEADER = """\
# .env.bak - provider keys that are currently out of credits, parked until they reset.
# Generated by check_credits.py --write-bak. Safe to delete: .env is the source of truth.
#
# Each block records WHY the key was parked and WHEN it is expected back.
# Dates marked ESTIMATED are inferred, not returned by an API: Browserbase and
# Kernel publish no billing/credits endpoint, so the monthly cycle is derived
# from the project createdAt. Only Webshare reports real dates.
# Set BROWSERBASE_RESET_DATE / KERNEL_RESET_DATE in .env to override with the
# exact date from the provider dashboard.
"""


def read_bak(path: str) -> dict[str, tuple[str, str, str]]:
    """Parse an existing .env.bak back into {var: (value, resets_at, source)}.

    Lets --write-bak be idempotent: a key that is parked stays parked with its
    date, instead of the file being rewritten empty on the next run.
    """
    if not os.path.exists(path):
        return {}
    entries: dict[str, tuple[str, str, str]] = {}
    resets_at, source = "unknown", "carried over"
    for line in open(path):
        stripped = line.strip()
        if stripped.startswith("#") and "|" in stripped:
            parts = [p.strip() for p in stripped.lstrip("#").strip().split("|")]
            if len(parts) == 3 and parts[2].startswith("resets "):
                # "resets <when> (<source>)" -> keep the inner source verbatim so
                # re-rendering the file does not nest the phrase again.
                match = re.match(r"resets\s+(.+?)\s+\((.+)\)$", parts[2])
                if match:
                    resets_at, source = match.group(1), match.group(2)
                else:
                    resets_at = parts[2][len("resets ") :]
            continue
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, _, value = stripped.partition("=")
        entries[name.strip()] = (value.strip(), resets_at, source)
    return entries


def write_bak(findings: list[dict[str, Any]]) -> None:
    """Park exhausted keys in .env.bak and disable them in .env.

    A parked key is kept until it is seen working again, at which point it is
    dropped from .env.bak. Re-running with no change is a no-op.
    """
    today = datetime.datetime.now(datetime.timezone.utc)
    env_path = os.path.join(BASE_DIR, ".env")
    bak_path = os.path.join(BASE_DIR, ".env.bak")

    with open(env_path) as handle:
        env_lines = handle.read().splitlines()

    # Variables that are still live (uncommented) in .env.
    active = {
        line.split("=", 1)[0].strip()
        for line in env_lines
        if "=" in line and not line.lstrip().startswith("#")
    }

    exhausted_now: dict[str, tuple[str, str, str]] = {}
    for entry in findings:
        if entry.get("state") != "exhausted" or not entry.get("env_var"):
            continue
        env_var = entry["env_var"]
        raw = os.getenv(env_var)
        if not raw:
            continue
        resets_at, source = _reset_hint(entry["provider"], entry, today)
        exhausted_now[env_var] = (raw, resets_at, source)

    # Previously parked entries: keep them unless the key is live again.
    merged: dict[str, tuple[str, str, str]] = {}
    for env_var, info in read_bak(bak_path).items():
        if env_var in active and env_var not in exhausted_now:
            print(f"  {env_var} is live again -> dropping from .env.bak (uncomment it in .env if wanted)")
            continue
        merged[env_var] = info
    merged.update(exhausted_now)

    # Comment newly parked keys out of .env so rotation stops wasting a call.
    to_comment = set(exhausted_now) - {
        line.split("=", 1)[0].strip()
        for line in env_lines
        if "=" in line and line.lstrip().startswith("#")
    }
    rewritten: list[str] = []
    for line in env_lines:
        name = line.split("=", 1)[0].strip()
        if name in to_comment and not line.lstrip().startswith("#"):
            _, resets_at, source = exhausted_now[name]
            rewritten.append(
                f"# {name} | out of credits, parked in .env.bak | resets {resets_at} ({source})"
            )
            rewritten.append(f"# {line}")
        else:
            rewritten.append(line)
    with open(env_path, "w") as handle:
        handle.write("\n".join(rewritten) + "\n")

    lines = [BAK_HEADER]
    for env_var, (raw, resets_at, source) in sorted(merged.items()):
        lines.append(
            f"\n# {env_var} | exhausted {today.date().isoformat()} | resets {resets_at} ({source})"
        )
        lines.append(f"{env_var}={raw}")
    with open(bak_path, "w") as handle:
        handle.write("\n".join(lines) + "\n")
    os.chmod(bak_path, 0o600)

    print(f"\nwrote {bak_path} ({len(merged)} parked key(s))")
    for env_var, (_, resets_at, source) in sorted(merged.items()):
        print(f"  parked {env_var} -> resets {resets_at} ({source})")
    if not merged:
        print("  nothing parked: no key is out of credits")


def _line(entry: dict[str, Any]) -> str:
    state = entry.get("state", "?")
    mark = {"ok": "OK     ", "exhausted": "SPENT  ", "dead": "DEAD   "}.get(state, "UNKNOWN")
    bits = [f"[{mark}] {entry['provider']}"]
    if entry.get("env_var"):
        bits.append(entry["env_var"])
    if entry.get("key"):
        bits.append(entry["key"])
    if entry.get("token"):
        bits.append(entry["token"])
    if entry.get("endpoint"):
        bits.append(entry["endpoint"])

    if "free_credits_usd" in entry:
        bits.append(f"free_credits=${entry['free_credits_usd']}")
    for flag in ("throttled", "paused"):
        if entry.get(flag):
            bits.append(flag.upper())
    if entry.get("reactivation_date"):
        bits.append(f"reactivates {entry['reactivation_date']}")
    if entry.get("cycle_end"):
        bits.append(f"cycle ends {entry['cycle_end']}")
    if entry.get("renewals_enabled") is False:
        bits.append("auto-renew OFF")
    if entry.get("failed_payment_times"):
        bits.append(f"failed payments x{entry['failed_payment_times']}")
    if entry.get("payment_method") is None and "free_credits_usd" in entry:
        bits.append("no payment method")
    plan = entry.get("plan")
    if plan:
        bits.append(
            f"plan {plan['proxy_type']}/{plan['proxy_subtype']} "
            f"{plan['proxy_count']}proxys {plan['bandwidth_limit_gb']}GB "
            f"status={plan['status']}"
        )
    if "proxy_user_in_account" in entry:
        bits.append(
            f"DUCKAI_PROXY user {entry['proxy_user_configured']} "
            f"in_account={entry['proxy_user_in_account']} "
            f"(listed users: {','.join(entry.get('proxy_usernames') or []) or 'none'})"
        )
    if entry.get("rate_limit_remaining") is not None:
        bits.append(f"ratelimit_remaining={entry['rate_limit_remaining']}")
    if entry.get("max_concurrent_sessions") is not None:
        bits.append(f"max_concurrent={entry['max_concurrent_sessions']}")
    for usage in entry.get("usage") or []:
        bits.append(f"usage[{usage['project']}]={usage['browser_minutes']}min/{usage['proxy_mb']}MB")
    if entry.get("email"):
        bits.append(entry["email"])
    if entry.get("status"):
        bits.append(f"http={entry['status']}")
    if entry.get("detail"):
        bits.append(f"| {entry['detail'][:200]}")
    return " ".join(bits)


def main() -> int:
    load_dotenv(os.path.join(BASE_DIR, ".env"))

    probe = "--probe" in sys.argv or "--write-bak" in sys.argv

    findings = check_webshare() + check_kernel() + check_browserbase()

    if probe:
        # Replace the cheap "key is valid" read with a real plan check per key.
        findings = [f for f in findings if f.get("provider") == "webshare"]
        for env_var, key in named_keys("KERNEL_API_KEY"):
            entry = probe_kernel(key)
            entry["env_var"] = env_var
            entry["key"] = _mask(key)
            findings.append(entry)
        for env_var, key in named_keys("BROWSERBASE_API_KEY"):
            with httpx.Client(timeout=30.0) as http:
                listing = http.get(f"{API_ROOT}/v1/projects", headers={"X-BB-API-Key": key})
            if listing.status_code != 200:
                findings.append(
                    {
                        "provider": "browserbase",
                        "env_var": env_var,
                        "key": _mask(key),
                        "state": _classify(listing),
                        "status": listing.status_code,
                        "detail": _body(listing),
                    }
                )
                continue
            project = listing.json()[0]
            entry = probe_browserbase(key, project["id"])
            entry["env_var"] = env_var
            entry["key"] = _mask(key)
            entry["project_created_at"] = project.get("createdAt")
            findings.append(entry)

    if "--json" in sys.argv:
        print(json.dumps(findings, indent=2))
        return 0

    for entry in findings:
        print(_line(entry))

    spent = [e for e in findings if e.get("state") == "exhausted"]
    dead = [e for e in findings if e.get("state") == "dead"]
    print()
    print(f"exhausted: {len(spent)}  dead: {len(dead)}  entries: {len(findings)}")
    if spent:
        today = datetime.datetime.now(datetime.timezone.utc)
        print("EXHAUSTED:")
        for entry in spent:
            resets_at, source = _reset_hint(entry["provider"], entry, today)
            label = entry.get("env_var") or entry.get("token")
            print(f"  {entry['provider']:<11} {label:<28} resets {resets_at} ({source})")
    if "--write-bak" in sys.argv:
        write_bak(findings)
    return 1 if (spent or dead) else 0


if __name__ == "__main__":
    sys.exit(main())
