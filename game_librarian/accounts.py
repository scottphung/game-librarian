"""Accounts on the hosted site: who is signed in, who may keep an account, and what keeps the sign-in safe.

Sign-in is Continue with Google through Supabase Auth (supabase_auth.py), which only the server talks to; the first
Google sign-in creates the account. There is no password and no email code. The session Supabase returns is kept in
two HttpOnly cookies (ACCESS_COOKIE, REFRESH_COOKIE), read again on every request (check_session): the access token is
verified with Supabase's published keys, and renewed with the refresh token when it has expired. A person is
identified by Supabase's id for them, never by an email address or anything else a browser sends; Supabase itself
links a Google sign-in to the existing user with the same verified address. GAME_LIBRARIAN_ALLOWED_USERS can limit
accounts to a small circle of addresses.

Protection, in short:
* Cookies: HttpOnly (page scripts cannot read them), SameSite=Lax, and on the hosted site Secure with the __Host-
  prefix, which browsers accept only from this exact host. That matters here: azurecontainerapps.io is not on the
  Public Suffix List, so other apps under it count as the same site and could otherwise plant cookies.
* Forms (sign-out): each carries this browser's form token, the value of its CSRF cookie (the double-submit pattern),
  and a browser's Sec-Fetch-Site must say the post came from this site (form_from_this_site). The wishlist API takes
  JSON only, which another site's page cannot send without a CORS preflight that this app never approves.
* Google: the PKCE code verifier waits in an HttpOnly cookie (OAUTH_COOKIE) for at most OAUTH_MAX_AGE, so only the
  browser that started a Google sign-in can finish it.
* Attempts: AttemptLimiter counts Google sign-ins per visitor address, because Supabase sees this server's address
  for every visitor (GOOGLE_SIGN_INS).
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
from urllib.parse import quote, unquote

from .supabase_auth import AuthError, Session, SupabaseAuth

PROVIDER = "supabase"  # accounts.provider of a Supabase account; rows from 0.20 say "github" (the `accounts` command)
WISHLIST_LIMIT = 500  # games one account can save
MAX_NAME_LENGTH = 200  # characters of a game name kept with a saved game
MAX_ALIASES = 10
SESSION_MAX_AGE = 30 * 24 * 3600  # the session cookies' lifetime in seconds; every renewal starts it again
OAUTH_MAX_AGE = 600  # seconds a Google sign-in may take before it has to start again
ACCESS_COOKIE = "gl_access"  # Supabase's access token (a signed JWT, valid one hour by default)
REFRESH_COOKIE = "gl_refresh"  # Supabase's refresh token, which gets the next access token
CSRF_COOKIE = "gl_csrf"  # this browser's form token
OAUTH_COOKIE = "gl_oauth"  # a Google sign-in under way: its PKCE code verifier, then "~" and the page to go back to
# A game key (identity.py), as static/wishlist.js accepts it: a Steam app id such as "271590", or "igdb-52189".
GAME_KEY = re.compile(r"(?:\d{1,10}|igdb-\d{1,10})")
# A form token as new_csrf_token makes it: 43 URL-safe characters (32 random bytes).
CSRF_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")
# The one-time code that Supabase adds to /auth/callback after Google (a UUID today): URL-safe characters only.
AUTH_CODE = re.compile(r"[A-Za-z0-9._~-]{8,256}")
# A PKCE code verifier as supabase_auth.new_code_verifier makes it: 43 to 128 URL-safe characters.
CODE_VERIFIER = re.compile(r"[A-Za-z0-9_-]{43,128}")

UNAVAILABLE = ("The sign-in service is not answering right now. Your wishlist in this browser still works; try again "
               "in a minute.")
TOO_MANY = "Too many attempts. Wait a few minutes, then try again."
GOOGLE_EXPIRED = ("That Google sign-in took too long, or it was started in another browser. Choose Continue with "
                  "Google again.")
GOOGLE_CANCELLED = "Google sign-in was cancelled. Choose Continue with Google to try again."
GOOGLE_UNFINISHED = "Google sign-in did not finish. Try again in a minute."


@dataclass(frozen=True)
class Identity:
    """A signed-in person, from a verified access token."""

    user_id: str  # Supabase's id for them (a UUID): it never changes, so it identifies the account (accounts.subject)
    email: str  # their verified email address, shown and matched by the allow list


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
    without case; the address is verified, because Supabase signs nobody in before)."""
    return not allowed_users or identity.email.casefold() in allowed_users


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


def set_oauth_cookie(response, verifier: str, return_to: str, *, secure: bool) -> None:
    """Keep a Google sign-in's code verifier, and the page it returns to, for OAUTH_MAX_AGE. SameSite=Lax, so the
    browser sends it back when Supabase redirects to /auth/callback (a top-level navigation); HttpOnly, so no page
    script can read it."""
    response.set_cookie(cookie_name(OAUTH_COOKIE, secure), f"{verifier}~{quote(return_to, safe='')}",
                        max_age=OAUTH_MAX_AGE, path="/", secure=secure, httponly=True, samesite="lax")


def read_oauth_cookie(value: str | None) -> tuple[str, str] | None:
    """(code verifier, page to go back to) from the Google sign-in cookie, or None when it is missing or damaged."""
    verifier, _, return_to = (value or "").partition("~")
    if not CODE_VERIFIER.fullmatch(verifier):
        return None
    return verifier, safe_return_path(unquote(return_to))


def clear_oauth_cookie(response, *, secure: bool) -> None:
    """Delete the Google sign-in cookie: a verifier is used at most once."""
    response.delete_cookie(cookie_name(OAUTH_COOKIE, secure), path="/", secure=secure, httponly=True, samesite="lax")


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

# Google sign-ins finished at /auth/callback, per visitor address: (attempts, seconds). Supabase's own limits are per
# IP address, and every visitor reaches it from this server's: by default 150 token requests per 5 minutes for the
# whole site, Google sign-ins and session renewals together. So one visitor must not be able to use them up.
GOOGLE_SIGN_INS = (20, 600)


class AttemptLimiter:
    """Counts attempts per visitor address in a sliding window: at most `limit` in `seconds`.

    The counts live in this process: one replica of the website, as on the hosted site (0 to 1 replicas). A refused
    attempt is not counted, so waiting is always enough. `clock` is injectable for tests.
    """

    def __init__(self, limit: int, seconds: float, clock: Callable[[], float] = time.monotonic) -> None:
        self._limit = limit
        self._seconds = seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._attempts: dict[str, deque[float]] = {}
        self._calls = 0

    def allow(self, address: str) -> bool:
        """Count one attempt from `address`; False, without counting it, when the address has used up the limit."""
        with self._lock:
            now = self._clock()
            window = self._attempts.setdefault(address, deque())
            while window and now - window[0] >= self._seconds:
                window.popleft()
            if len(window) >= self._limit:
                return False
            window.append(now)
            self._calls += 1
            if self._calls % 1000 == 0:  # forget visitors whose attempts are all older than the window
                self._attempts = {key: kept for key, kept in self._attempts.items()
                                  if kept and now - kept[-1] < self._seconds}
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

SESSION_ENDED = ("session_not_found", "session_expired", "refresh_token_not_found", "refresh_token_already_used",
                 "bad_jwt", "user_not_found")
# A Google sign-in whose one-time code Supabase no longer accepts: used, expired, or not this browser's.
GOOGLE_CODE_REFUSED = ("flow_state_not_found", "flow_state_expired", "flow_state_already_used", "bad_code_verifier",
                       "bad_oauth_state", "bad_oauth_callback")


def auth_problem(exc: AuthError) -> tuple[int, str]:
    """The HTTP status and the sentence for people that answer a refused Supabase request, or a Google sign-in that
    Supabase sent back with its error code (Supabase's error codes,
    https://supabase.com/docs/guides/auth/debugging/error-codes). Supabase's own message is never shown."""
    code = exc.code
    if code == "over_request_rate_limit" or exc.status == 429:
        return 429, "Too many sign-ins at once on this site. Wait a minute, then try again."
    if code in GOOGLE_CODE_REFUSED:
        return 400, GOOGLE_EXPIRED
    if code == "signup_disabled":
        return 403, "New accounts cannot be created on this site right now. Accounts made before still sign in."
    if code == "provider_email_needs_verification":
        return 403, ("Google has not verified the email address of that Google account, so it cannot sign in here. "
                     "Verify the address with Google, then try again.")
    if code == "provider_disabled":
        return 503, "Google sign-in is not available on this site right now. Try again later."
    if code in SESSION_ENDED:
        return 401, "Your session has ended. Sign in again."
    if code == "user_banned":
        return 403, "This account cannot sign in."
    if exc.service_problem:
        return 503, UNAVAILABLE
    return 400, GOOGLE_UNFINISHED


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
