"""Accounts on the hosted site: who is signed in, who may keep an account, and what keeps the sign-in safe.

Sign-in is by email address and password through Supabase Auth (supabase_auth.py), which only the server talks to.
The session it returns is kept in two HttpOnly cookies (ACCESS_COOKIE, REFRESH_COOKIE), read again on every request
(check_session): the access token is verified with Supabase's published keys, and renewed with the refresh token when
it has expired. A person is identified by Supabase's id for them, never by an email address or anything else a browser
sends; GAME_LIBRARIAN_ALLOWED_USERS can limit accounts to a small circle of confirmed addresses.

Protection, in short:
* Cookies: HttpOnly (page scripts cannot read them), SameSite=Lax, and on the hosted site Secure with the __Host-
  prefix, which browsers accept only from this exact host. That matters here: azurecontainerapps.io is not on the
  Public Suffix List, so other apps under it count as the same site and could otherwise plant cookies.
* Forms: each carries this browser's form token, the value of its CSRF cookie (the double-submit pattern), and a
  browser's Sec-Fetch-Site must say the post came from this site (form_from_this_site). The wishlist API takes JSON
  only, which another site's page cannot send without a CORS preflight that this app never approves.
* Attempts: AttemptLimiter counts sign-ins, sign-ups and emails per visitor address and per email address, because
  Supabase sees this server's address for every visitor.
* Redirects: only to a path on this site (safe_return_path).
"""

from __future__ import annotations

import ipaddress
import re
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable

from .config import EMAIL_ADDRESS
from .supabase_auth import AuthError, Session, SupabaseAuth

PROVIDER = "supabase"  # accounts.provider of an email account; rows from 0.20 say "github" (the `accounts` command)
WISHLIST_LIMIT = 500  # games one account can save
MAX_NAME_LENGTH = 200  # characters of a game name kept with a saved game
MAX_ALIASES = 10
MAX_EMAIL_LENGTH = 254
MIN_PASSWORD_LENGTH = 8  # Supabase's own minimum is 6 unless the project asks for more
MAX_PASSWORD_BYTES = 72  # Supabase keeps a bcrypt hash of the password, and bcrypt reads only 72 bytes
SESSION_MAX_AGE = 30 * 24 * 3600  # the session cookies' lifetime in seconds; every renewal starts it again
ACCESS_COOKIE = "gl_access"  # Supabase's access token (a signed JWT, valid one hour by default)
REFRESH_COOKIE = "gl_refresh"  # Supabase's refresh token, which gets the next access token
CSRF_COOKIE = "gl_csrf"  # this browser's form token
# A game key (identity.py), as static/wishlist.js accepts it: a Steam app id such as "271590", or "igdb-52189".
GAME_KEY = re.compile(r"(?:\d{1,10}|igdb-\d{1,10})")
# A form token as new_csrf_token makes it: 43 URL-safe characters (32 random bytes).
CSRF_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")
# The token_hash of an email link (Supabase's hashed one-time code, such as 56 hex digits): letters, digits, - and _.
TOKEN_HASH = re.compile(r"[A-Za-z0-9_-]{8,256}")

UNAVAILABLE = ("The sign-in service is not answering right now. Your wishlist in this browser still works; try again "
               "in a minute.")
TOO_MANY = "Too many attempts. Wait a few minutes, then try again."
LINK_PROBLEM = "This link has expired or was already used. Ask for a new one below."


@dataclass(frozen=True)
class Identity:
    """A signed-in person, from a verified access token."""

    user_id: str  # Supabase's id for them (a UUID): it never changes, so it identifies the account (accounts.subject)
    email: str  # their confirmed email address, shown and matched by the allow list


@dataclass(frozen=True)
class AccountState:
    """What a page needs to know about sign-in for one request."""

    enabled: bool  # the Supabase project is configured, so accounts are offered
    identity: Identity | None  # who is signed in, if anyone
    allowed: bool  # the signed-in person may keep an account (GAME_LIBRARIAN_ALLOWED_USERS)
    unavailable: bool = False  # the session could not be checked, because Supabase did not answer

    @property
    def signed_in(self) -> bool:
        """Signed in with an account they may use: their wishlist is kept on the server."""
        return self.identity is not None and self.allowed

    @property
    def mode(self) -> str:
        """For the page's scripts: "account" (server wishlist), "browser" (signed out) or "off" (no accounts)."""
        return "account" if self.signed_in else ("browser" if self.enabled else "off")


@dataclass(frozen=True)
class SessionCheck:
    """What the session cookies of one request said (check_session)."""

    identity: Identity | None
    renewed: Session | None = None  # a new session from the refresh token: the response sets the cookies to it
    ended: bool = False  # the cookies hold no usable session any more: the response deletes them
    unavailable: bool = False  # Supabase did not answer, so nobody is signed in for now; the cookies stay


NO_SESSION = SessionCheck(None)


def is_allowed(identity: Identity, allowed_users: frozenset[str]) -> bool:
    """Whether the person may keep an account: anyone when the list is empty, else a listed email address (compared
    without case; the address is confirmed, because Supabase signs nobody in before)."""
    return not allowed_users or identity.email.casefold() in allowed_users


def clean_email(value: str | None) -> str | None:
    """The email address a visitor typed, trimmed, or None when it cannot be one."""
    text = (value or "").strip()
    return text if len(text) <= MAX_EMAIL_LENGTH and EMAIL_ADDRESS.fullmatch(text) else None


def password_problem(password: str, confirmation: str | None = None) -> str | None:
    """What is wrong with a new password, for people, or None. Supabase applies its own rules afterwards."""
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"Choose a password of at least {MIN_PASSWORD_LENGTH} characters."
    if len(password.encode("utf-8")) > MAX_PASSWORD_BYTES:
        return f"Choose a password of at most {MAX_PASSWORD_BYTES} characters (fewer if it has accents or emoji)."
    if confirmation is not None and password != confirmation:
        return "The two passwords are not the same."
    return None


def safe_return_path(value: str | None) -> str:
    """`value` when it is a path on this site ("/wishlist?x=1"), else "/": a sign-in never returns to another site."""
    if not value or not value.startswith("/") or value.startswith("//") or "\\" in value or len(value) > 512:
        return "/"
    return value


# ---------------------------------------------------------------------- cookies


def cookie_name(name: str, secure: bool) -> str:
    """A cookie's name: with the __Host- prefix on the hosted site (`secure`), so only this host can set it."""
    return f"__Host-{name}" if secure else name


def set_session_cookies(response, session: Session, *, secure: bool) -> None:
    """Keep Supabase's session in the two HttpOnly cookies for SESSION_MAX_AGE."""
    for name, value in ((ACCESS_COOKIE, session.access_token), (REFRESH_COOKIE, session.refresh_token)):
        response.set_cookie(cookie_name(name, secure), value, max_age=SESSION_MAX_AGE, path="/", secure=secure,
                            httponly=True, samesite="lax")


def clear_session_cookies(response, *, secure: bool) -> None:
    """Delete the session cookies (signing out, or a session Supabase has ended)."""
    for name in (ACCESS_COOKIE, REFRESH_COOKIE):
        response.delete_cookie(cookie_name(name, secure), path="/", secure=secure, httponly=True, samesite="lax")


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def set_csrf_cookie(response, token: str, *, secure: bool) -> None:
    """The form token's cookie: for this browser session only, HttpOnly (the server writes the token into forms)."""
    response.set_cookie(cookie_name(CSRF_COOKIE, secure), token, path="/", secure=secure, httponly=True,
                        samesite="lax")


def form_from_this_site(fetch_site: str | None, cookie_token: str | None, form_token: str | None) -> bool:
    """Whether a posted form may be acted on: the browser does not say it came from elsewhere (Sec-Fetch-Site, which
    every current browser sends), and it carries this browser's form token, the value of its form-token cookie. A page
    on another site can neither read that cookie nor, with the __Host- name, set it."""
    if fetch_site not in (None, "same-origin"):
        return False
    return bool(cookie_token and form_token and CSRF_TOKEN.fullmatch(cookie_token)
                and secrets.compare_digest(cookie_token, form_token))


# ---------------------------------------------------------------------- the session of a request


def check_session(auth: SupabaseAuth, access_token: str | None, refresh_token: str | None) -> SessionCheck:
    """Who the session cookies stand for.

    A valid access token is enough. An expired or unreadable one is replaced through the refresh token: the response
    then carries the new cookies. A session that Supabase has ended (signed out, revoked, a refresh token used twice)
    deletes the cookies. When Supabase cannot be reached, nobody is signed in for this request, but the cookies stay, so
    the next request can try again.
    """
    if not access_token and not refresh_token:
        return NO_SESSION
    try:
        user = auth.verify_access_token(access_token)
        if user is not None:
            return SessionCheck(Identity(user.user_id, user.email))
        if not refresh_token:
            return SessionCheck(None, ended=True)
        session = auth.refresh(refresh_token)
        user = auth.verify_access_token(session.access_token)
    except AuthError as exc:
        return SessionCheck(None, unavailable=True) if exc.service_problem else SessionCheck(None, ended=True)
    if user is None:
        return SessionCheck(None, ended=True)
    return SessionCheck(Identity(user.user_id, user.email), renewed=session)


# ---------------------------------------------------------------------- attempts per visitor

# Each action's limits: (attempts, seconds) per visitor address, and per email address (None: not counted by address).
# Supabase's own limits are per IP address, and every visitor reaches it from this server's: 30 sign-ups or emails and
# 150 sign-ins per 5 minutes for the whole site, and one email a minute per address.
LIMITS = {
    "sign_in": ((20, 600), (8, 600)),
    "sign_up": ((5, 3600), (3, 3600)),
    "email_link": ((6, 3600), (3, 3600)),  # a new confirmation link, or a password-reset link
    "confirm": ((20, 600), None),  # opening an email link
    "password": ((10, 600), None),  # choosing a new password
}


class AttemptLimiter:
    """Counts each action's attempts per visitor address and per email address, in a sliding window (LIMITS).

    The counts live in this process: one replica of the website, as on the hosted site (0 to 1 replicas). A refused
    attempt is not counted, so waiting is always enough. `clock` is injectable for tests.
    """

    def __init__(self, limits: dict = LIMITS, clock: Callable[[], float] = time.monotonic) -> None:
        self._limits = limits
        self._clock = clock
        self._lock = threading.Lock()
        self._attempts: dict[tuple[str, str], deque[float]] = {}
        self._calls = 0

    def allow(self, action: str, address: str, email: str | None = None) -> bool:
        """Count one attempt; False, without counting it, when the address or the email has used up the limit."""
        per_address, per_email = self._limits[action]
        counted = [((f"{action}:address", address), per_address)]
        if email and per_email:
            counted.append(((f"{action}:email", email.casefold()), per_email))
        with self._lock:
            now = self._clock()
            windows = []
            for key, (limit, seconds) in counted:
                window = self._attempts.setdefault(key, deque())
                while window and now - window[0] >= seconds:
                    window.popleft()
                if len(window) >= limit:
                    return False
                windows.append(window)
            for window in windows:
                window.append(now)
            self._calls += 1
            if self._calls % 1000 == 0:  # forget visitors whose attempts are all older than every window
                longest = max(limit[1] for pair in self._limits.values() for limit in pair if limit)
                self._attempts = {key: window for key, window in self._attempts.items()
                                  if window and now - window[-1] < longest}
        return True


def client_address(request, *, hosted: bool) -> str:
    """The visitor's IP address, for counting attempts only, never for deciding who someone is.

    On the hosted site the connection comes from Azure Container Apps' ingress proxy, which adds the visitor's address
    to the end of X-Forwarded-For: a visitor can put anything before it, never after. So the last public address there
    is the visitor's. Elsewhere the connection's own address is used and the header ignored.
    """
    if hosted:
        for part in reversed(request.headers.get("x-forwarded-for", "").split(",")):
            candidate = part.strip()
            if candidate.startswith("[") and "]" in candidate:  # [2001:db8::1]:443
                candidate = candidate[1:candidate.index("]")]
            elif candidate.count(":") == 1:  # 203.0.113.9:443
                candidate = candidate.split(":")[0]
            try:
                address = ipaddress.ip_address(candidate)
            except ValueError:
                continue
            if address.is_global:
                return str(address)
    return request.client.host if request.client else "unknown"


# ---------------------------------------------------------------------- what to tell people

WEAK_PASSWORD_REASONS = {
    "length": "make it longer",
    "characters": "mix in the kinds of characters this site asks for (such as lowercase and uppercase letters, digits "
                  "and symbols)",
    "pwned": "it has appeared in a known data breach, so choose another one",
}
SESSION_ENDED = ("session_not_found", "session_expired", "refresh_token_not_found", "refresh_token_already_used",
                 "bad_jwt", "user_not_found")


def auth_problem(exc: AuthError) -> tuple[int, str]:
    """The HTTP status and the sentence for people that answer a refused Supabase request (Supabase's error codes,
    https://supabase.com/docs/guides/auth/debugging/error-codes). Supabase's own message is never shown."""
    code = exc.code
    if exc.service_problem and exc.status != 429:
        return 503, UNAVAILABLE
    if code in ("over_request_rate_limit", "over_email_send_rate_limit") or exc.status == 429:
        return 429, "Too many attempts or emails for now. Wait a few minutes, then try again."
    if code == "invalid_credentials":
        return 400, "The email address or password is not right."
    if code == "email_not_confirmed":
        return 400, ("Confirm your email address first: open the link in the email we sent. You can ask for a new "
                     "link below.")
    if code == "weak_password":
        reasons = [WEAK_PASSWORD_REASONS[reason] for reason in exc.reasons if reason in WEAK_PASSWORD_REASONS]
        return 400, "Choose a stronger password" + (": " + "; ".join(reasons) if reasons else "") + "."
    if code == "same_password":
        return 400, "Choose a password that is different from your current one."
    if code in ("email_address_invalid", "validation_failed"):
        return 400, "This email address cannot be used. Check it, or try another one."
    if code in ("signup_disabled", "email_provider_disabled"):
        return 403, "New accounts cannot be created on this site right now."
    if code == "email_address_not_authorized":
        return 503, "This site cannot send email yet, so the link could not be sent. Try again later."
    if code == "otp_expired" or (exc.status == 403 and code.startswith("http_")):
        return 400, LINK_PROBLEM
    if code == "reauthentication_needed":
        return 403, ("For your security, choose a new password through “Forgot your password?” on the sign-in page "
                     "instead.")
    if code in SESSION_ENDED:
        return 401, "Your session has ended. Sign in again."
    if code == "user_banned":
        return 403, "This account cannot sign in."
    return 400, "That did not work. Check what you entered, then try again."


# ---------------------------------------------------------------------- wishlist requests


def wishlist_entry(payload) -> tuple[str, str, list[str]] | None:
    """A game to save from a request body {"id", "name", "aliases"}: (key, name, aliases), or None when malformed."""
    if not isinstance(payload, dict):
        return None
    key, name, aliases = payload.get("id"), payload.get("name"), payload.get("aliases", [])
    if not isinstance(key, str) or not GAME_KEY.fullmatch(key) or not isinstance(name, str) or not name.strip():
        return None
    if not isinstance(aliases, list) or len(aliases) > MAX_ALIASES:
        return None
    if not all(isinstance(alias, str) and GAME_KEY.fullmatch(alias) for alias in aliases):
        return None
    return key, name.strip()[:MAX_NAME_LENGTH], aliases


def import_entries(payload) -> list[tuple[str, str]] | None:
    """The games of a browser wishlist to import, from {"items": [{"id", "name"}, ...]}: (key, name) pairs in their
    order, each key once; None when the body is malformed or longer than a wishlist can be."""
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list) or len(items) > WISHLIST_LIMIT:
        return None
    entries: dict[str, str] = {}
    for item in items:
        entry = wishlist_entry({"id": item.get("id"), "name": item.get("name")} if isinstance(item, dict) else None)
        if entry is None:
            return None
        entries.setdefault(entry[0], entry[1])
    return list(entries.items())
