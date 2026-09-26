"""Yomoni member-space extraction, independent of the browser provider.

The entry point takes an already-connected Playwright ``browser`` rather than
creating a session itself, so the same login-and-walk code runs unchanged over
Browserbase, Kernel, or any future transport. Session lifecycle lives in
transports.py; this module only knows the site.

One-time codes are a problem in batch mode: there is nobody at the keyboard, so
an account that needs one is reported as its own outcome instead of being
muddled into a password failure.
"""

from __future__ import annotations

import contextlib
import datetime
import re
from dataclasses import dataclass, field
from typing import Any

ORIGIN = "https://my.yomoni.fr"
SIGN_IN = f"{ORIGIN}/sign-in"
CODE_SELECTOR = 'input[autocomplete="one-time-code"], input[inputmode="numeric"]'
EMAIL_SELECTOR = 'input[type="email"]'
PASSWORD_SELECTOR = 'input[type="password"]'
ROUTES = ["/home", "/profile", "/notifications", "/news"]
SUBSCRIPTION_RE = re.compile(r"/home/subscription-in-progress\?accountId=[0-9a-f-]+")

# The portal answers a rejected pair with one banner and never separates
# "unknown email" from "wrong password", so both collapse into one verdict.
BAD_CREDENTIALS_RE = re.compile(
    r"Identifiants\s+incorrects|combinaison\s+identifiant\s+et\s+mot\s+de\s+passe",
    re.IGNORECASE,
)

# Outcomes, split by whether re-attempting could ever change the answer.
OK = "ok"
BAD_CREDENTIALS = "bad_credentials"
NEEDS_2FA = "needs_2fa"
BLOCKED = "blocked"
UNKNOWN = "unknown"

#: Outcomes that must never be retried: the answer will not change on a new run,
#: so replaying them only burns browser minutes.
DEFINITIVE = {OK, BAD_CREDENTIALS, NEEDS_2FA}


@dataclass
class Outcome:
    status: str
    pages: list[dict[str, Any]] = field(default_factory=list)
    auth_text: str = ""
    note: str = ""

    @property
    def retryable(self) -> bool:
        return self.status not in DEFINITIVE

    @property
    def ok(self) -> bool:
        return self.status == OK


async def page_text(page: Any) -> str:
    with contextlib.suppress(Exception):
        return (await page.locator("body").inner_text()).strip()
    return ""


def is_blocked(page: Any, html: str, title: str) -> bool:
    blob = f"{html} {title}".lower()
    return any(
        m in blob
        for m in ("just a moment", "cf_chl_", "checking your browser", "attention required", "access denied")
    )


async def login(page: Any, email: str, password: str) -> tuple[str, str]:
    """Sign in and classify the result.

    Returns (status, banner_text). The portal needs ~8s to hydrate before the
    inputs exist, so this waits on the selector rather than sleeping.
    """
    await page.goto(SIGN_IN, wait_until="domcontentloaded", timeout=120_000)
    await page.wait_for_selector(EMAIL_SELECTOR, timeout=60_000)
    await page.locator(EMAIL_SELECTOR).first.fill(email)
    await page.wait_for_timeout(1_000)

    if await page.locator(PASSWORD_SELECTOR).count():
        await page.locator(PASSWORD_SELECTOR).first.fill(password)
        await page.wait_for_timeout(1_000)
        # The form has no usable submit button, so Enter is the way in.
        with contextlib.suppress(Exception):
            await page.locator(PASSWORD_SELECTOR).first.press("Enter")
    else:
        with contextlib.suppress(Exception):
            await page.locator("button[type=submit]").first.click(timeout=5_000)
        await page.wait_for_timeout(4_000)
        await page.wait_for_selector(PASSWORD_SELECTOR, timeout=30_000)
        await page.locator(PASSWORD_SELECTOR).first.fill(password)
        await page.wait_for_timeout(1_000)
        with contextlib.suppress(Exception):
            await page.locator(PASSWORD_SELECTOR).first.press("Enter")

    for _ in range(20):
        await page.wait_for_timeout(4_000)
        if "sign-in" not in page.url:
            return OK, ""
        if await page.locator(CODE_SELECTOR).count():
            # Nobody can type a code in batch mode.
            return NEEDS_2FA, await page_text(page)
        text = await page_text(page)
        if BAD_CREDENTIALS_RE.search(text):
            return BAD_CREDENTIALS, text
        with contextlib.suppress(Exception):
            html = await page.content()
            if is_blocked(page, html, await page.title()):
                return BLOCKED, text

    return UNKNOWN, await page_text(page)


async def force_click(page: Any, text: str) -> bool:
    """Click by visible text, bypassing overlay interception.

    The member space is a React Native-Web app whose clickable rows are divs
    without a button role, so a normal click is often intercepted by an
    overlay; these fallbacks reach the underlying handler.
    """
    loc = page.get_by_text(text, exact=False).first
    for attempt in ("click", "force"):
        try:
            await loc.click(timeout=5_000, **({"force": True} if attempt == "force" else {}))
            return True
        except Exception:
            pass
    try:
        count = await loc.count()
        for i in range(count):
            handle = await loc.nth(i).element_handle()
            clicked = await handle.evaluate(
                "(el) => { const r = el.getBoundingClientRect();"
                " const el2 = document.elementFromPoint(r.x + r.width/2, r.y + r.height/2);"
                " if (el2 && el2.closest('button,a,[role=button]')) { el2.click(); return true; }"
                " el.click(); return true; }"
            )
            if clicked:
                return True
    except Exception:
        pass
    return False


async def capture(page: Any, url: str) -> dict[str, Any]:
    await page.goto(url, wait_until="domcontentloaded", timeout=120_000)
    await page.wait_for_timeout(10_000)
    return {
        "url": page.url,
        "requested": url,
        "title": (await page.title()) or "",
        "captured_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "text": (await page.locator("body").inner_text()).strip(),
    }


async def dismiss_cookies(page: Any) -> None:
    for label in ("Continuer sans accepter", "Accepter & Fermer", "Refuser"):
        with contextlib.suppress(Exception):
            loc = page.get_by_text(label, exact=True).first
            if await loc.count():
                await loc.click(timeout=3_000)
                await page.wait_for_timeout(2_000)
                return


async def capture_form(page: Any, url: str) -> dict[str, Any]:
    """Capture text plus the pre-filled field values of a wizard step."""
    try:
        field_values = await page.evaluate(
            "() => [...document.querySelectorAll('input, textarea, select')]"
            ".map(e => ({tag: e.tagName, type: e.type || '', name: e.name || '', "
            "placeholder: e.placeholder || '', value: e.value || ''}))"
        )
    except Exception:
        field_values = []
    return {
        "url": page.url,
        "requested": url,
        "title": (await page.title()) or "",
        "captured_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "text": (await page.locator("body").inner_text()).strip(),
        "field_values": field_values,
    }


async def walk(browser: Any, email: str, password: str) -> Outcome:
    """Log in and tour the member space with an already-open browser.

    A brand-new context is used rather than the cloud browser's default one, so
    no cookie or storage entry from a previous account can leak in. The context
    is torn down whatever happens, which is also what keeps concurrent workers
    from sharing any state.
    """
    from hygiene import apply_identity, new_clean_context

    identity = await apply_identity(browser)
    ctx = await new_clean_context(browser, locale=identity.get("locale", "fr-FR"))
    try:
        return await _tour(ctx, email, password)
    finally:
        with contextlib.suppress(Exception):
            await ctx.close()


async def _tour(ctx: Any, email: str, password: str) -> Outcome:
    page = await ctx.new_page()
    page.set_default_timeout(60_000)

    status, text = await login(page, email, password)
    if status != OK:
        return Outcome(status, [], text)

    pages = [await capture(page, f"{ORIGIN}/home")]

    # The subscription wizard is clickable text on /home, not an <a> link.
    sub = ""
    for label in ("Souscription en cours", "Assurance-vie", "Votre futur projet"):
        with contextlib.suppress(Exception):
            await page.get_by_text(label, exact=False).first.click(timeout=4_000)
            await page.wait_for_timeout(6_000)
            if SUBSCRIPTION_RE.search(page.url):
                sub = page.url
                break

    for route in ROUTES[1:]:
        pages.append(await capture(page, f"{ORIGIN}{route}"))

    if sub:
        pages.append(await capture(page, sub))
        # "Poursuivre ma souscription" exposes the pre-filled personal details.
        await force_click(page, "Poursuivre ma souscription")
        await page.wait_for_timeout(10_000)
        await dismiss_cookies(page)
        await page.wait_for_timeout(3_000)
        pages.append(await capture_form(page, sub))
        # The "Projet" step holds the address, but may need its tab clicked first.
        await force_click(page, "Projet")
        await page.wait_for_timeout(8_000)
        pages.append(await capture_form(page, sub))

    # Profile settings is where the address usually lives.
    await capture(page, f"{ORIGIN}/profile")
    for label in ("Paramètres", "Données personnelles", "Comptes et préférences"):
        if await force_click(page, label):
            await page.wait_for_timeout(6_000)
            pages.append(await capture_form(page, f"{ORIGIN}/profile"))
            break

    return Outcome(OK, pages)


def summarize(outcome: Outcome, email: str) -> str:
    """Reduce a full walk to the one results line per account."""
    if not outcome.ok:
        note = (outcome.auth_text or outcome.note or "").replace("\n", " ")[:160]
        return f"{email}|mot de passe changé|||inconnu|login refuse: {note}"

    def find(needle: str) -> str:
        for page in outcome.pages:
            if needle.lower() in page.get("text", "").lower():
                return page
        return {}

    home = find("Solde")
    sub = find("Souscription")
    form = next(
        (p for p in reversed(outcome.pages) if p.get("field_values")),
        {},
    )
    return "|".join([email, "<mdp>", "<data>", "<address>", "<flag>", _short(sub or home, 400)])
