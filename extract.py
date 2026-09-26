"""Turning captured page text into one result line per account.

Pure functions only: no browser, no network, no I/O. Kept verbatim from the
original scraper because the patterns were tuned against real captures - the
product detection in particular is line-anchored so marketing copy in a footer
can never be mistaken for a contract label.
"""

from __future__ import annotations

import re
from typing import Any

from yomoni import BAD_CREDENTIALS, NEEDS_2FA, OK

def _field(text: str, label: str, until: str | None = None) -> str:
    match = re.search(re.escape(label) + r"\n(.+?)(?:\n|$)", text, re.DOTALL)
    if not match:
        return ""
    value = match.group(1).strip()
    if until:
        value = value.split(until)[0].strip()
    return value


def last4(text: str) -> str:
    """Keep only the last 4 digits of a (possibly masked) phone number."""
    digits = re.findall(r"\d", text or "")
    return "".join(digits[-4:]) if digits else ""


PASSWORD_CHANGED = "mot de passe changé"
TWO_FA_REQUIRED = "2FA requise"
# Markers written in the mdp column when the account could not be read. They can
# never equal a real password, which is what makes them recognisable.
FAILURE_MARKERS = (PASSWORD_CHANGED, TWO_FA_REQUIRED)


def attempt_key(mail: str, mdp: str) -> str:
    """Key the attempt state by (mail, mdp) so a second password for the same
    mail does not erase the record of the first one."""
    return f"{mail}\t{mdp}"


def is_success_line(mdp: str) -> bool:
    return bool(mdp) and mdp not in FAILURE_MARKERS

# Product families, kept apart on purpose: an epargne immobiliere is NOT an
# assurance-vie. Patterns are line-anchored on purpose too, so a sentence of
# marketing copy in a footer can never be read as a contract label. Only the
# account's own pages (/home, /profile) are scanned, never the /news feed.
PRODUCT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "assurance-vie",
        re.compile(
            r"^[ \t]*(?:[A-Za-zÀ-ÿ]+(?:[ \t]+[A-Za-zÀ-ÿ]+)?[ \t]+-[ \t]+)?"
            r"Assurance[-\s]?vie[ \t]*$",
            re.MULTILINE | re.IGNORECASE,
        ),
    ),
    (
        "epargne-immo",
        re.compile(
            r"^[ \t]*(?:[A-Za-zÀ-ÿ]+(?:[ \t]+[A-Za-zÀ-ÿ]+)?[ \t]+-[ \t]+)?"
            r"(?:[ÉE]pargne[ \t]+immobili[èe]re?|SCPI|Pinel)[ \t]*$",
            re.MULTILINE | re.IGNORECASE,
        ),
    ),
)

# Any short line that smells like a product label. Used to surface a label we
# do not recognise instead of silently reporting "aucun produit". Lines holding
# an email or a URL are excluded: "immo@x.com" is an address, not a product.
CANDIDATE_LABEL_RE = re.compile(
    r"^[ \t]*(?=[^\n]*(?:[ée]pargne|assurance|\bimmo\b|SCPI|Pinel))[^\n]{0,60}$",
    re.MULTILINE | re.IGNORECASE,
)
NOT_A_LABEL_RE = re.compile(r"@|https?://|www\.")

ACCOUNT_ROUTES = ("/home", "/profile")


def detect_product(pages: list[dict[str, Any]]) -> tuple[str, str, list[str]]:
    """Return (product, raw_label, unknown_labels) for the account.

    product is "assurance-vie", "epargne-immo" or "" when nothing was found.
    """
    texts = [
        p.get("text", "")
        for p in pages
        if any(p.get("url", "").rstrip("/").endswith(r) for r in ACCOUNT_ROUTES)
    ]
    joined = "\n".join(t for t in texts if t)

    product = ""
    raw = ""
    for name, pattern in PRODUCT_PATTERNS:
        m = pattern.search(joined)
        if m:
            product = name
            raw = re.sub(r"\s+", " ", m.group(0)).strip()
            break

    unknown: list[str] = []
    for line in CANDIDATE_LABEL_RE.findall(joined):
        line = re.sub(r"\s+", " ", line).strip()
        if not line or NOT_A_LABEL_RE.search(line):
            continue
        if any(p.search(line) for _, p in PRODUCT_PATTERNS):
            continue
        if line not in unknown:
            unknown.append(line)
    return product, raw, unknown


def extract_fields(pages: list[dict[str, Any]], password: str = "") -> dict[str, str]:
    profile = next((p.get("text", "") for p in pages if "/profile" in p.get("url", "")), "")
    home = next((p.get("text", "") for p in pages if p.get("url", "").rstrip("/").endswith("/home")), "")

    fields: dict[str, str] = {}

    fields["mail"] = _field(profile, "Adresse email")
    # /profile masks the password as "********************": the only usable
    # value is the one that just logged the session in, so reuse it.
    fields["mdp"] = password if password else _field(profile, "Mot de passe")
    fields["tel4"] = last4(_field(profile, "Numéro de téléphone"))
    fields["adresse"] = _field(profile, "Adresse")

    product, raw_label, unknown = detect_product(pages)
    # An epargne immobiliere must not be reported as an assurance-vie.
    fields["assurance_vie"] = "oui" if product == "assurance-vie" else "non"

    details: list[str] = []
    if product:
        details.append(f"type: {product}" + (f" ({raw_label})" if raw_label else ""))
    elif not unknown:
        details.append("type: aucun produit (ni assurance-vie, ni epargne immobiliere)")

    m = re.search(r"(\d[\d ]*)\n,(\d{2})\n€", home)
    amount = f"{m.group(1).replace(' ', '')},{m.group(2)} €" if m else ""
    if not amount:
        m = re.search(r"([\d ]+),\s*(\d{2})\s*€", home)
        amount = f"{m.group(1).strip()},{m.group(2)} €" if m else ""
    if amount:
        details.append(("montant " if product else "solde ") + amount)

    m = re.search(r"\+\s*([\d ]+),\s*(\d{2})\s*€", home)
    if m:
        details.append(f"versement +{m.group(1).strip()},{m.group(2)} €")

    m = re.search(r"Souscription en cours\s*\n\s*(\d/5\s*\w+(?:\s+\w+)?)", home)
    if m:
        details.append("souscription " + re.sub(r"\s+", " ", m.group(1)).strip())

    m = re.search(r"Mis à jour le\s*\n?([^\n]+)", home)
    if m:
        details.append("maj " + m.group(1).strip())

    for label in unknown:
        details.append("libelle inconnu: " + label)

    fields["info_assurance_vie"] = "; ".join(details) if details else "aucun detail"
    return fields


def failed_fields(email: str, auth: str, auth_text: str = "") -> dict[str, str]:
    """Line for an account we could not log into."""
    if auth == BAD_CREDENTIALS:
        return {
            "mail": email,
            "mdp": PASSWORD_CHANGED,
            "tel4": "",
            "adresse": "",
            "assurance_vie": "inconnu",
            "info_assurance_vie": (
                "login refuse: identifiants incorrects "
                "(mot de passe changed ou identifiant inconnu)"
            ),
        }
    if auth == NEEDS_2FA:
        return {
            "mail": email,
            "mdp": TWO_FA_REQUIRED,
            "tel4": "",
            "adresse": "",
            "assurance_vie": "inconnu",
            "info_assurance_vie": "compte existant, code a temps de saisir pour finir le login",
        }
    reason = auth or "inconnu"
    if auth_text:
        snippet = re.sub(r"\s+", " ", auth_text)[:120]
        reason = f"{reason} ({snippet})"
    return {
        "mail": email,
        "mdp": "",
        "tel4": "",
        "adresse": "",
        "assurance_vie": "inconnu",
        "info_assurance_vie": f"login echoue: {reason}",
    }


def to_line(fields: dict[str, str]) -> str:
    order = ["mail", "mdp", "tel4", "adresse", "assurance_vie", "info_assurance_vie"]
    return "|".join(fields.get(k, "").replace("|", "/").replace("\n", " ") for k in order)
