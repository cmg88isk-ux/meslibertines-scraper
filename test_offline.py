"""Tests that need no browser and no API credits.

The pool logic is the part that quietly loses work: a key that is not returned
blocks every other worker, and a verdict that is cached as final can never be
retried. Both failures are invisible in a normal run and expensive in a real
one, so they are pinned down here.

    python test_offline.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile

import scrape_multi as sm
from extract import attempt_key, is_success_line
from transports import TransportError
from yomoni import BAD_CREDENTIALS, BLOCKED, OK, Outcome

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name} {detail}")
        FAILURES.append(name)


def temp_store() -> sm.Store:
    """A store writing to a throwaway directory.

    The real Store defaults to result/; a test that used it directly would
    overwrite an actual scrape, which is exactly what happened once already.
    """
    return sm.Store(result_dir=tempfile.mkdtemp(prefix="yomoni-test-"))


class FakeTransport:
    def __init__(self, name: str, keys: list[str]) -> None:
        self.name = name
        self._keys = keys

    def keys(self) -> list[str]:
        return list(self._keys)


async def test_pool_returns_every_key() -> None:
    """Every key must come back after use, or the pool starves."""
    pool = sm.SlotPool([FakeTransport("t", ["k1", "k2", "k3"])])
    slots = []
    for _ in range(3):
        slot = await pool.take()
        assert slot is not None
        slots.append(slot)
    check("pool: 3 cles prises", len(slots) == 3)

    for slot in slots:
        await pool.give_back(slot)
    recovered = []
    for _ in range(3):
        slot = await pool.take(avoid=set())
        assert slot is not None
        recovered.append(slot[1])
    check("pool: toutes les cles rendues", sorted(recovered) == ["k1", "k2", "k3"], f"-> {recovered}")


async def test_pool_avoids_and_waits() -> None:
    """A retry must skip the key that just failed, and wait for a busy one."""
    pool = sm.SlotPool([FakeTransport("t", ["k1", "k2"])])
    first = await pool.take()
    check("pool: premiere cle", first is not None and first[1] == "k1")

    # k1 is held elsewhere; ask for something that is not k2.
    task = asyncio.create_task(pool.take(avoid={"k2"}))
    await asyncio.sleep(0.1)
    check("pool: attend une cle occupee", not task.done())
    await pool.give_back(first)  # type: ignore[arg-type]
    second = await asyncio.wait_for(task, timeout=5)
    check("pool: evite la cle interdite", second is not None and second[1] == "k1", f"-> {second}")


async def test_pool_gives_none_when_all_avoided() -> None:
    pool = sm.SlotPool([FakeTransport("t", ["k1", "k2"])])
    result = await pool.take(avoid={"k1", "k2"})
    check("pool: None si tout evite", result is None)


async def test_pool_discards_dead_key() -> None:
    pool = sm.SlotPool([FakeTransport("t", ["k1", "k2"])])
    slot = await pool.take()
    assert slot is not None
    pool.discard(slot)
    await pool.give_back(slot)
    got = await pool.take()
    check("pool: cle morte retiree", got is not None and got[1] == "k2", f"-> {got}")
    await pool.give_back(got)  # type: ignore[arg-type]


async def test_transient_outcome_is_not_cached() -> None:
    """A blocked account must stay retryable; a rejection must not."""
    store = temp_store()
    await store.record("a@b.fr", "pwd", Outcome(BLOCKED, [], "cf challenge"), "ignored")
    check(
        "etat: transitoire non memorise",
        store.known("a@b.fr", "pwd") is None,
        f"-> {store.known('a@b.fr', 'pwd')}",
    )
    check("etat: aucune ligne ecrite", store.lines == [], f"-> {store.lines}")

    await store.record("a@b.fr", "pwd", Outcome(BAD_CREDENTIALS, [], "Identifiants incorrects"), "a@b.fr|mdp|||inconnu|x")
    check("etat: rejet memorise", store.known("a@b.fr", "pwd") == BAD_CREDENTIALS)
    check("etat: ligne de rejet ecrite", len(store.lines) == 1)


async def test_success_is_never_overwritten_by_failure() -> None:
    """Real account data must outlive a later failed attempt on the same mail."""
    store = temp_store()
    good = "good@g.fr|secret|||oui|montant 12,00 €"
    store.lines, store.entries["good@g.fr"] = [good], "secret"

    await store.record("good@g.fr", "secret", Outcome(BAD_CREDENTIALS, [], "nope"), "good@g.fr|mdp|||inconnu|x")
    check("donnees: succes preserve", store.lines == [good], f"-> {store.lines}")

    await store.record("good@g.fr", "secret", Outcome(OK, [{"text": "nouveau"}]), "good@g.fr|secret|||oui|nouveau")
    check("donnees: relu ecrase l'ancien", store.lines[0].endswith("nouveau"))


def test_failure_markers_never_look_like_passwords() -> None:
    check("marqueur: mdp change", not is_success_line("mot de passe changé"))
    check("marqueur: 2FA", not is_success_line("2FA requise"))
    check("marqueur: vrai mdp", is_success_line("VraiMotDePasse1!"))


def test_credentials_parser() -> None:
    """Parser checks run on a fixture, not on the real credentials.txt.

    credentials.txt is gitignored on purpose, so a fresh clone has none: a test
    that reads it fails everywhere except the machine that produced it. That
    is exactly what a clone test caught.
    """
    fixture = os.path.join(tempfile.mkdtemp(prefix="yomoni-creds-"), "credentials.txt")
    with open(fixture, "w", encoding="utf-8") as f:
        f.write(
            "# commentaire\n"
            "\n"
            "dup@example.com:motdepasse1\n"
            "DUP@example.com:motdepasse1\n"      # doublon exact, meme casse differente
            "AUTRE@Example.com:motdepasse2\n"
            "sans-separateur\n"
            "vide@example.com:\n"
            ":orphelin\n"
            "deux@example.com:p1\n"
            "deux@example.com:p2\n"              # meme mail, mdp differents
        )
    accounts = sm.read_all_credentials(fixture)
    emails = [e for e, _ in accounts]
    pairs = set(accounts)
    check("parser: doublon exact ecarte", len(pairs) == len(accounts), f"-> {accounts}")
    check("parser: email normalise", all(e == e.lower() for e in emails), f"-> {emails}")
    check("parser: ligne sans ':' ignoree", "sans-separateur" not in emails)
    check("parser: champ vide ignore", "vide@example.com" not in emails)
    check("parser: orphelin ignore", "" not in emails)
    check("parser: commentaire ignore", len(accounts) == 4, f"-> {len(accounts)}")
    # Both passwords for one address are kept: that is how a corrected password
    # gets tested without erasing the history of the previous one.
    check(
        "parser: 2 mdp pour un meme mail conserves",
        sum(1 for e, _ in accounts if e == "deux@example.com") == 2,
    )


def test_real_credentials_if_present() -> None:
    """Extra check on the live file, skipped when there is none."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "credentials.txt")
    if not os.path.exists(path):
        print("  SKIP  fichier credentials.txt absent (normal sur un clone)")
        return
    accounts = sm.read_all_credentials(path)
    check("credentials: aucun doublon exact", len(accounts) == len(set(accounts)))
    check("credentials: emails normalises", all(e == e.lower() for e, _ in accounts))
    check("credentials: aucun champ vide", all(e and p for e, p in accounts))


def test_attempt_key_distinguishes_passwords() -> None:
    """Two passwords for one mail must not overwrite each other's record."""
    a = attempt_key("x@y.fr", "p1")
    b = attempt_key("x@y.fr", "p2")
    check("cle: 2 mdp -> 2 entrees", a != b)


def test_transport_error_classification() -> None:
    from transports import _classify

    check("classement: 402 non reessayable", not _classify(402, "quota reached").retryable)
    check("classement: 401 credits non reessayable", not _classify(401, "insufficient credits").retryable)
    check("classement: 401 invalide non reessayable", not _classify(401, "invalid api key").retryable)
    check("classement: 429 reessayable", _classify(429, "slow down").retryable)
    check("classement: 500 reessayable", _classify(500, "oops").retryable)
    check("classement: erreur transporte", isinstance(_classify(500, "x"), TransportError))


async def main() -> int:
    print("pool de cles")
    await test_pool_returns_every_key()
    await test_pool_avoids_and_waits()
    await test_pool_gives_none_when_all_avoided()
    await test_pool_discards_dead_key()
    print("persistance et dedup")
    await test_transient_outcome_is_not_cached()
    await test_success_is_never_overwritten_by_failure()
    test_failure_markers_never_look_like_passwords()
    test_attempt_key_distinguishes_passwords()
    test_credentials_parser()
    test_real_credentials_if_present()
    print("transports")
    test_transport_error_classification()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} test(s) en echec: {', '.join(FAILURES)}")
        return 1
    print("tous les tests passent")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
