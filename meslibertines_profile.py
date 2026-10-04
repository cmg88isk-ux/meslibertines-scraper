"""Classify a meslibertines.com account/profile the same way the site does.

Two account natures are told apart by the profile URL:

    /member/<slug>/            a member, i.e. a client account
    /escort/<slug>-<id>/       an advertisement (annonceur/escort)

Within an advertisement the profile panel carries a ``Sexe:`` line whose value
is the sub-type. The site's own edit form encodes those same sub-types as
``data[gender]`` codes (m/f/c/t), so those codes are the canonical return value:
``f`` femme, ``m`` homme, ``c`` couple, ``t`` trans. Returning the site's codes
rather than free text means a caller never has to normalise "Transexuelle" vs
"trans" itself.

The meta description is deliberately ignored: for a trans profile it still reads
"est une ... femme", so only the profile ``Sexe:`` panel is authoritative.

Pure functions: no browser, no network, no I/O.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

KIND_MEMBER = "membre"
KIND_ESCORT = "escort"

# Site codes, matching the `data[gender]` radios on /member_dashes/edit_profile/.
TYPE_FEMME = "f"
TYPE_HOMME = "m"
TYPE_COUPLE = "c"
TYPE_TRANS = "t"

TYPE_LABELS: dict[str, str] = {
    TYPE_FEMME: "femme",
    TYPE_HOMME: "homme",
    TYPE_COUPLE: "couple",
    TYPE_TRANS: "trans",
}

# French label on the profile panel -> site code. The keys are the words the
# site actually prints after "Sexe:".
_GENDER_CODES: dict[str, str] = {
    "femme": TYPE_FEMME,
    "homme": TYPE_HOMME,
    "couple": TYPE_COUPLE,
    "transexuelle": TYPE_TRANS,
    "transsexuelle": TYPE_TRANS,
    "transgenre": TYPE_TRANS,
    "trans": TYPE_TRANS,
    "ts": TYPE_TRANS,
}

_SEXE_RE = re.compile(
    r"Sexe\s*:[\s|]*(?:l'|le\s+|la\s+)?([A-Za-zÀ-ÿ]+)",
    re.IGNORECASE,
)
_INSCRIT_RE = re.compile(r"Inscrit\s*:\s*(\d{2}/\d{2}/\d{4})")
_LAST_SEEN_RE = re.compile(
    r"derni[eè]re\s+connexion\s*:\s*(\d{2}/\d{2}/\d{4}(?:\s+\d{1,2}:\d{2})?)",
    re.IGNORECASE,
)
_AGE_RE = re.compile(r"Âge\s*:\s*(\d+)", re.IGNORECASE)
_CITY_RE = re.compile(r"Ville(?:\s+de\s+base)?\s*:[\s|]*([^\n|]+)", re.IGNORECASE)
_ESCORT_ID_RE = re.compile(r"/escort/[^/]*-(\d+)/?")
_MEMBER_RANK_RE = re.compile(r"/member/rankings/(\d+)/")


def detect_kind(url: str) -> str:
    """Return KIND_MEMBER, KIND_ESCORT, or "" when the URL is neither."""
    path = urlparse(url or "").path
    if path.startswith("/member/") or path.startswith("/member_dashes/"):
        return KIND_MEMBER
    if path.startswith("/escort/") or path.startswith("/escorts/"):
        return KIND_ESCORT
    return ""


# The advertiser dashboard has a "Abonnement" block. When no paid package is
# active it renders a `.free-package` box ("vous ne disposez pas d'un paquet")
# plus a `.go-premium` call to action. When a package is active that box is
# gone and the block holds the subscription instead. Members have no such block.
_PREMIUM_NO_MARKERS = (
    "ne disposez pas d'un paquet",
    "free-package",
    "go-premium",
)
_PREMIUM_BLOCK_MARKERS = ("package-holder", "abonnement")

PREMIUM_YES = "oui"
PREMIUM_NO = "non"


def detect_premium(html: str, text: str = "") -> str:
    """Return "oui", "non", or "" (no subscription concept / unknown).

    Only the advertiser dashboard carries the subscription block, so a member
    page (or any page without it) yields "".
    """
    blob = (html or "").lower()
    body = (" ".join([blob, (text or "").lower()])).lower()
    if any(marker in body for marker in _PREMIUM_NO_MARKERS):
        return PREMIUM_NO
    if any(marker in body for marker in _PREMIUM_BLOCK_MARKERS):
        return PREMIUM_YES
    return ""


def detect_type(text: str) -> str:
    """Return the site gender code from the ``Sexe:`` line, or ""."""
    match = _SEXE_RE.search(text or "")
    if not match:
        return ""
    return _GENDER_CODES.get(match.group(1).strip().lower(), "")


def parse_profile(url: str, text: str) -> dict[str, str]:
    """Extract the account nature, sub-type and key profile fields.

    ``type`` is the site gender code (f/m/c/t). ``label`` is its French name.
    Callers can map to a display string with ``TYPE_LABELS``.
    """
    text = text or ""
    kind = detect_kind(url)
    code = detect_type(text)
    city_match = _CITY_RE.search(text)
    escort_id = _ESCORT_ID_RE.search(url or "")
    rank_id = _MEMBER_RANK_RE.search(text or "") or _MEMBER_RANK_RE.search(url or "")
    return {
        "kind": kind,
        "type": code,
        "label": TYPE_LABELS.get(code, ""),
        "inscrit": (_INSCRIT_RE.search(text) or [None, ""])[1] if _INSCRIT_RE.search(text) else "",
        "last_seen": (_LAST_SEEN_RE.search(text) or [None, ""])[1] if _LAST_SEEN_RE.search(text) else "",
        "age": (_AGE_RE.search(text) or [None, ""])[1] if _AGE_RE.search(text) else "",
        "city": city_match.group(1).strip() if city_match else "",
        "id": escort_id.group(1) if escort_id else (rank_id.group(1) if rank_id else ""),
    }
