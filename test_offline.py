"""Tests that need no browser and no API credits.

Covers the shared transport error classification, the meslibertines account
type/premium parser, and the target dedup / resume / output logic.

    python test_offline.py
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from datetime import date
from pathlib import Path

import check_meslibertines as cm
import meslibertines_profile as mp
from transports import TransportError, _classify

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name} {detail}")
        FAILURES.append(name)


def test_transport_error_classification() -> None:
    check("classement: 402 non reessayable", not _classify(402, "quota reached").retryable)
    check("classement: 401 credits non reessayable", not _classify(401, "insufficient credits").retryable)
    check("classement: 401 invalide non reessayable", not _classify(401, "invalid api key").retryable)
    check("classement: 429 reessayable", _classify(429, "slow down").retryable)
    check("classement: 500 reessayable", _classify(500, "oops").retryable)
    check("classement: erreur transporte", isinstance(_classify(500, "x"), TransportError))


def test_meslibertines_account_types() -> None:
    """Every account nature and sub-type must be told apart.

    Fixtures are real captures, kept short. URLs decide member vs escort; the
    profile ``Sexe:`` panel decides f/m/c/t.
    """
    femme = "NOTIONS | PERSONNELS | Sexe: | Femme | Ethnique: | Caucasien | Âge: | 22 | Ville de base: | Tarbes"
    trans = "Sexe: | Transexuelle | Ethnique: | Latin | Âge: | 28"
    couple = "Sexe: | Couple | Ethnique: | Latin | Âge: | 20"
    homme = "Sexe: | Homme | Ethnique: | Latin | Âge: | 35"
    member = "Inscrit: 11/01/2023 | La dernière connexion: 04/10/2026 16:07 | Âge:0 | Sexe:L'homme | Ville:Évreux"

    check("membre: url", mp.detect_kind("https://www.meslibertines.com/member/eudesfre/") == mp.KIND_MEMBER)
    check("escort: url", mp.detect_kind("https://www.meslibertines.com/escort/Adriana-965519/") == mp.KIND_ESCORT)
    check("nature: url inconnue -> vide", mp.detect_kind("https://www.meslibertines.com/") == "")

    check("type: femme -> f", mp.detect_type(femme) == mp.TYPE_FEMME)
    check("type: trans -> t", mp.detect_type(trans) == mp.TYPE_TRANS)
    check("type: couple -> c", mp.detect_type(couple) == mp.TYPE_COUPLE)
    check("type: homme -> m", mp.detect_type(homme) == mp.TYPE_HOMME)
    check("type: membre L'homme -> m", mp.detect_type(member) == mp.TYPE_HOMME)
    check("type: absent -> vide", mp.detect_type("aucune mention") == "")

    # The meta description calls a trans profile a "femme"; it must not win.
    deceptive = "est une latin femme à Metz | Sexe: | Transexuelle"
    check("type: la meta femme ne gagne pas sur Sexe", mp.detect_type(deceptive) == mp.TYPE_TRANS)

    femme_p = mp.parse_profile("https://www.meslibertines.com/escort/Adriana-965519/", femme)
    check("parse: escort id", femme_p["id"] == "965519", f"-> {femme_p['id']}")
    check("parse: escort type", femme_p["type"] == "f")
    check("parse: label femme", femme_p["label"] == "femme")
    check("parse: ville", femme_p["city"] == "Tarbes", f"-> {femme_p['city']}")

    member_p = mp.parse_profile(
        "https://www.meslibertines.com/member/eudesfre/ "
        "/member/rankings/1419361/",
        member,
    )
    check("parse: membre kind", member_p["kind"] == mp.KIND_MEMBER)
    check("parse: membre inscrit", member_p["inscrit"] == "11/01/2023", f"-> {member_p['inscrit']}")
    check("parse: membre derniere connexion", member_p["last_seen"] == "04/10/2026 16:07")
    check("parse: membre age", member_p["age"] == "0")


def test_premium_detection() -> None:
    """Premium is read from the advertiser dashboard's Abonnement block."""
    non_premium = (
        '<div class="package-holder no-icons-list"><div class="list-header">Abonnement</div>'
        '<div class="free-package bordered-medium"><p class="title">'
        "(vous ne disposez pas d'un paquet)</p>"
        '<a class="go-premium" href="/orders/step1/">go premium maintenant</a></div></div>'
    )
    premium = (
        '<div class="package-holder no-icons-list"><div class="list-header">Abonnement</div>'
        '<div class="premium-package"><p class="title">Pack Gold</p>'
        "<p>Expire le 01/01/2027</p></div></div>"
    )
    member = "<div class='member_dashes'>Bienvenue dans votre espace prive</div>"

    check("premium: paquet absent -> non", mp.detect_premium(non_premium) == mp.PREMIUM_NO)
    check("premium: paquet actif -> oui", mp.detect_premium(premium) == mp.PREMIUM_YES)
    check("premium: page membre -> vide", mp.detect_premium(member) == "")


def test_vip_days() -> None:
    """Remaining VIP days come from the countdown, else the expiry date."""
    # Real dashboard capture: the site wraps the count as "(10 jours) en attendant".
    real = (
        '<div class="package-holder no-icons-list"><div class="list-header">Abonnement</div>'
        '<ul><li><strong>Premium VIP</strong> <span class="green">(10 jours) en attendant</span>'
        '<div class="clearfix"></div></li></ul></div>'
    )
    countdown = '<div class="premium-package"><p>12 jours restants</p></div>'
    expiry = '<div class="premium-package"><p>Expire le 31/12/2026</p></div>'
    free = "<div class='free-package'>vous ne disposez pas d'un paquet</div>"
    loyalty = "<span>198</span> points manquants pour obtenir 5 jours VIP gratuitement!"

    check("vip: libelle reel '(10 jours) en attendant'", mp.detect_vip_days(real) == "10")
    check("vip: compte a rebours direct", mp.detect_vip_days(countdown) == "12")
    check(
        "vip: date de fin convertie en jours",
        mp.detect_vip_days(expiry, today=date(2026, 12, 1)) == "30",
        f"-> {mp.detect_vip_days(expiry, today=date(2026, 12, 1))}",
    )
    check("vip: paquet expire -> 0", mp.detect_vip_days(expiry, today=date(2027, 1, 5)) == "0")
    check("vip: fin extraite", mp.detect_vip_expiry(expiry) == "31/12/2026")
    check("vip: sans paquet -> vide", mp.detect_vip_days(free) == "")
    check("vip: 'jours VIP' de fidelite ignore", mp.detect_vip_days(loyalty) == "")
    check("vip: date sans mot-cle -> vide", mp.detect_vip_days("Inscrit: 01/02/2020") == "")
    check("vip: date invalide -> vide", mp.detect_vip_days("Expire le 99/99/2027") == "")


def test_targets_dedup_keeps_other_passwords() -> None:
    """An exact duplicate is dropped; the same user with another password stays."""
    directory = Path(tempfile.mkdtemp(prefix="ml-targets-"))
    (directory / "a.txt").write_text(
        "# commentaire\n"
        "\n"
        "a@x.fr:p1\n"
        "a@x.fr:p1\n"          # doublon exact -> ecarte
        "a@x.fr:p2\n"          # meme pseudo, autre mdp -> garde
        "b:p1\n"
        "ligne-sans-separateur\n"
        ":orphelin\n"
        "vide:\n",
        encoding="utf-8",
    )
    accounts = cm.read_targets(directory)
    check("targets: doublon exact ecarte", len(accounts) == len(set(accounts)), f"-> {accounts}")
    check("targets: meme pseudo autre mdp garde", ("a@x.fr", "p1") in accounts and ("a@x.fr", "p2") in accounts)
    check(
        "targets: ordre et lignes ignorees",
        accounts == [("a@x.fr", "p1"), ("a@x.fr", "p2"), ("b", "p1")],
        f"-> {accounts}",
    )


def test_resume_is_keyed_by_pair() -> None:
    """Resume skips a resolved couple, never a new password for the same user."""
    accounts = [("a", "p1"), ("a", "p2"), ("b", "p1")]
    previous = [
        {"user": "a", "password": "p1", "status": cm.STATUS_PASSWORD_FALSE},
        {"user": "a", "password": "p2", "status": cm.STATUS_CHALLENGE},  # transitoire -> revient
        {"user": "c", "password": "p9", "status": cm.STATUS_OK},
    ]
    todo = cm.pending_accounts(accounts, previous)
    check("reprise: couple refuse saute", ("a", "p1") not in todo)
    check("reprise: meme pseudo autre mdp retente", ("a", "p2") in todo)
    check("reprise: compte neuf garde", ("b", "p1") in todo)
    check("reprise: transitoire non saute", todo == [("a", "p2"), ("b", "p1")], f"-> {todo}")
    check("redo: tout retente", cm.pending_accounts(accounts, previous, redo=True) == accounts)


def test_outputs_do_not_overwrite_previous() -> None:
    """A later run must keep earlier verdicts, and history is append-only."""
    directory = Path(tempfile.mkdtemp(prefix="ml-out-"))
    first = {
        "user": "a", "password": "p1", "status": cm.STATUS_OK, "type": "m", "label": "homme",
        "inscrit": "11/01/2023", "last_seen": "", "ip": "1.2.3.4", "provider": "kernel:***abc", "detail": "",
    }
    cm.write_results([first], directory)
    previous = cm.load_previous(directory)
    check("sorties: results.json relu", bool(previous) and previous[0]["user"] == "a", f"-> {previous}")
    valid_text = (directory / "valid.txt").read_text().splitlines()
    check("sorties: valid.txt a un en-tete", valid_text[0] == cm.VALID_HEADER, f"-> {valid_text[0]}")
    check("sorties: valid.txt porte le type", any("|m|homme|" in line for line in valid_text))

    second = dict(first)
    second.update(
        user="b", password="p2", status=cm.STATUS_PASSWORD_FALSE, type="", label="",
        inscrit="", detail="identifiants invalides (site)",
    )
    cm.write_results(previous + [second], directory)
    final = cm.load_previous(directory)
    check(
        "sorties: ancien verdict conserve",
        {r["user"] for r in final} == {"a", "b"},
        f"-> {[r['user'] for r in final]}",
    )
    invalid_text = (directory / "invalids.txt").read_text()
    check("sorties: invalids.txt porte l'en-tete", invalid_text.splitlines()[0] == cm.INVALID_HEADER)
    check("sorties: invalids.txt porte la cause", "b:p2|password=false|identifiants invalides (site)" in invalid_text)
    check(
        "sorties: results.txt porte l'en-tete",
        (directory / "results.txt").read_text().splitlines()[0] == cm.RESULTS_HEADER,
    )

    # premium.txt ne garde que les annonceurs a paquet actif.
    premium = dict(first)
    premium.update(
        user="vip", password="pv", status=cm.STATUS_OK, kind="escort", type="f",
        label="femme", premium=mp.PREMIUM_YES, jours_vip="10", inscrit="", last_seen="",
    )
    notpremium = dict(first)
    notpremium.update(user="free", password="pf", status=cm.STATUS_OK, kind="escort", premium=mp.PREMIUM_NO)
    cm.write_results([premium, notpremium], directory)
    premium_text = (directory / "premium.txt").read_text().splitlines()
    check("sorties: premium.txt en-tete", premium_text[0] == cm.PREMIUM_HEADER, f"-> {premium_text}")
    check("sorties: premium.txt garde le vip", any("vip:pv|escort|f|femme|oui|10" in l for l in premium_text))
    check("sorties: premium.txt ecarte le non-premium", not any(l.startswith("free:pf") for l in premium_text))

    cm.append_history(first, directory)
    cm.append_history(second, directory)
    lines = (directory / "history.txt").read_text().strip().splitlines()
    check("sorties: history append-only", len(lines) == 3 and lines[0] == cm.HISTORY_HEADER, f"-> {lines}")


def test_keypool_and_no_keys() -> None:
    """A dead key leaves the pool; an empty pool stops the account cleanly."""
    pool = cm.KeyPool({"kernel": ["a", "b"], "browserbase": []}, ["kernel", "browserbase"])
    check("pool: cles presentes", pool.any() and pool.counts()["kernel"] == 2, f"-> {pool.counts()}")
    check("pool: ordre kernel", pool.ordered(0) == [("kernel", "a"), ("kernel", "b")], f"-> {pool.ordered(0)}")
    check("pool: slot decale", pool.ordered(1)[0] == ("kernel", "b"), f"-> {pool.ordered(1)}")

    asyncio.run(pool.discard("kernel", "a"))
    asyncio.run(pool.discard("kernel", "b"))
    check("pool: vide apres retrait", not pool.any(), f"-> {pool.counts()}")

    result = asyncio.run(cm.check_account("u", "p", pool, 2))
    check("pool: compte sans cle -> erreur claire", result["status"] == cm.STATUS_ERROR)
    check(
        "pool: message 'plus aucune cle'",
        "plus aucune cle disponible" in result["detail"],
        f"-> {result['detail']}",
    )


def test_history_row_migration() -> None:
    """Older history rows (7 and 9 fields) are upgraded to the 10-field shape."""
    directory = Path(tempfile.mkdtemp(prefix="ml-hist-"))
    old_7 = "2026-01-01T00:00:00+00:00|a:p|ok|m|1.2.3.4|kernel:***x|detail"
    old_9 = "2026-01-02T00:00:00+00:00|b:p|ok|escort|f|oui|1.2.3.5|kernel:***y|detail"
    (directory / "history.txt").write_text(
        cm.HISTORY_HEADER + "\n" + old_7 + "\n" + old_9 + "\n", encoding="utf-8"
    )
    cm.ensure_history_header(directory)
    lines = (directory / "history.txt").read_text().splitlines()
    check("history: header intact", lines[0] == cm.HISTORY_HEADER, f"-> {lines[0]}")
    check("history: ancienne ligne 7 -> 10 champs", len(lines[1].split("|")) == 10, f"-> {lines[1]}")
    check("history: type conserve (7)", lines[1].split("|")[4] == "m", f"-> {lines[1]}")
    check("history: ancienne ligne 9 -> 10 champs", len(lines[2].split("|")) == 10, f"-> {lines[2]}")
    check("history: premium conserve (9)", lines[2].split("|")[5] == "oui", f"-> {lines[2]}")
    check("history: idempotent", cm.ensure_history_header(directory) and
          (directory / "history.txt").read_text().splitlines()[2] == lines[2])


def test_credit_exhaustion_discards_keys() -> None:
    """A 402 on every key empties the pool and leaves the account retryable."""
    async def fake_check_once(user, password, provider, key, *, hygiene=True):
        return {
            "user": user, "status": cm.STATUS_ERROR, "kind": "", "type": "", "label": "",
            "premium": "", "inscrit": "", "last_seen": "", "ip": "",
            "provider": f"{provider}:***{key[-6:]}",
            "detail": "transport: out of credits: HTTP 402", "key_dead": "1", "rate_limited": "",
        }

    original = cm.check_once
    cm.check_once = fake_check_once
    try:
        pool = cm.KeyPool({"kernel": ["k1", "k2"]}, ["kernel"])
        result = asyncio.run(cm.check_account("u", "p", pool, 2))
    finally:
        cm.check_once = original

    check("credit: toutes les cles retirees", not pool.any(), f"-> {pool.counts()}")
    check("credit: compte non definitif (sera repris)", result["status"] == cm.STATUS_ERROR)
    check(
        "credit: message d'arret explicite",
        "CREDIT" in cm.NO_KEYS_MESSAGE.upper() and "kernel.sh" in cm.NO_KEYS_MESSAGE,
    )
    check("credit: message indique les restants", "{remaining}" in cm.NO_KEYS_MESSAGE)


def main() -> int:
    print("transports")
    test_transport_error_classification()
    print("meslibertines: types de comptes")
    test_meslibertines_account_types()
    print("meslibertines: premium")
    test_premium_detection()
    test_vip_days()
    print("meslibertines: cibles, reprise, sorties")
    test_targets_dedup_keeps_other_passwords()
    test_resume_is_keyed_by_pair()
    test_outputs_do_not_overwrite_previous()
    test_keypool_and_no_keys()
    test_credit_exhaustion_discards_keys()
    test_history_row_migration()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} test(s) en echec: {', '.join(FAILURES)}")
        return 1
    print("tous les tests passent")
    return 0


if __name__ == "__main__":
    sys.exit(main())
