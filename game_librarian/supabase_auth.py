"""Email-and-password accounts through Supabase Auth, which only this site's server talks to.

Supabase Auth keeps the sign-in side of an account: the email address, the password (Supabase stores only a hash),
email confirmation and password resets. Everything else stays in the site's own database (store.py): a person's
account row there is keyed by Supabase's id for them (the access token's `sub`), never by an email address.

Who calls Supabase: only this module, on the server, with the project's publishable key. Supabase calls that key
safe to expose: it names the app, not a person, and grants nothing on its own. No secret or service-role key is used
anywhere. The browser never calls Supabase and never receives a Supabase token: the session that Supabase returns (an
access token and a refresh token) is kept in HttpOnly cookies (accounts.py), which page scripts cannot read.

How a request is trusted (verify_access_token): the access token is a JSON Web Token that Supabase signs with the
project's asymmetric signing key (ES256 or RS256). This module checks it on every request with the public keys that
Supabase publishes (the JWKS, kept for JWKS_CACHE_SECONDS, as long as Supabase's own edge keeps it): the signature,
the algorithm, the issuer (<project URL>/auth/v1), the audience ("authenticated"), the expiry, the role and the user
id. A token signed any other way (a shared HS256 secret, "none", or a key Supabase does not publish) is refused, so an
id or email address that a browser sends is never believed on its own.

Endpoints: Supabase Auth's REST API, the one supabase-js uses (openapi.yaml of github.com/supabase/auth and Supabase's
guides, read 2026-11-22): POST /signup, POST /token?grant_type=password and refresh_token, POST /verify with a
token_hash, POST /recover, POST /resend, PUT /user, POST /logout?scope=local, GET /.well-known/jwks.json. Supabase
counts its rate limits by the caller's IP address, which is this server's for every visitor; web.py therefore also
limits each visitor itself (accounts.AttemptLimiter).
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Callable

import httpx
import jwt

from .config import USER_AGENT

AUTH_PATH = "/auth/v1"
TIMEOUT = httpx.Timeout(10.0, connect=5.0)
ALGORITHMS = ("ES256", "RS256")  # Supabase's asymmetric signing keys; a token with any other algorithm is refused
AUDIENCE = "authenticated"  # the aud claim of a signed-in person's access token
SIGNED_IN_ROLE = "authenticated"
JWKS_CACHE_SECONDS = 600  # Supabase's edge keeps the keys 10 minutes and asks clients not to keep them longer
JWKS_RETRY_SECONDS = 30  # an unknown key id (a new signing key) or a failed fetch is retried at most this often
LEEWAY_SECONDS = 30  # allowed difference between this server's clock and Supabase's
MAX_TOKEN_LENGTH = 8_192  # a longer cookie value is not a Supabase access token
# The two kinds of email link this site sends (DEPLOYMENT.md, "Email templates"): confirming a new account's address,
# and choosing a new password. "signup" is the older name of "email" in Supabase's API, accepted for older templates.
EMAIL_LINK_TYPES = ("email", "recovery", "signup")
UNREACHABLE = "unreachable"  # AuthError.code when Supabase did not answer at all
UNEXPECTED = "unexpected"  # AuthError.code when Supabase answered with something this module cannot read
# Supabase's id for a person: a UUID such as "0b3f5c1e-8f2a-4c6d-9e1b-2a7d4c8e9f01".
USER_ID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")

LOG = logging.getLogger("game_librarian.supabase_auth")


class AuthError(Exception):
    """Supabase refused a request, or could not be reached.

    `code` is Supabase's error code (such as "invalid_credentials" or "weak_password"; its documented list is at
    https://supabase.com/docs/guides/auth/debugging/error-codes), or UNREACHABLE / UNEXPECTED. `status` is the HTTP
    status (0 when Supabase did not answer). `reasons` are a weak password's reasons ("length", "characters",
    "pwned"). Supabase's own message is not kept: it can repeat the email address, and pages show their own words.
    """

    def __init__(self, code: str, status: int, reasons: tuple[str, ...] = ()) -> None:
        super().__init__(f"Supabase Auth answered {code} (HTTP {status})")
        self.code = code
        self.status = status
        self.reasons = reasons

    @property
    def service_problem(self) -> bool:
        """Supabase is down, limited or answered strangely: not a reason to end anyone's session."""
        return self.code in (UNREACHABLE, UNEXPECTED) or self.status >= 500 or self.status == 429


@dataclass(frozen=True)
class Session:
    """Supabase's session for a signed-in person: a short-lived access token (one hour by default) and the refresh
    token that gets the next one. Both stay on the server and in HttpOnly cookies."""

    access_token: str
    refresh_token: str


@dataclass(frozen=True)
class User:
    """The person a verified access token stands for."""

    user_id: str  # Supabase's id for them (the token's sub, a UUID): the key of their account on this site
    email: str  # their email address, confirmed before Supabase let them sign in


def _error(response: httpx.Response) -> AuthError:
    """The AuthError of an error answer. Supabase gives its code as error_code, or, in newer API versions, as a text
    `code`; OAuth-style answers give `error`. An HTTP 429 without a code is a rate limit."""
    try:
        body = response.json()
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        body = {}
    code = body.get("error_code")
    if not isinstance(code, str) and isinstance(body.get("code"), str):
        code = body["code"]
    if not isinstance(code, str) and isinstance(body.get("error"), str):
        code = body["error"]
    if not isinstance(code, str) or not code:
        code = "over_request_rate_limit" if response.status_code == 429 else f"http_{response.status_code}"
    weak = body.get("weak_password")
    reasons = weak.get("reasons") if isinstance(weak, dict) else None
    listed = tuple(reason for reason in reasons if isinstance(reason, str)) if isinstance(reasons, list) else ()
    return AuthError(code, response.status_code, listed)


def _session(answer: dict) -> Session:
    """The session in an answer of /token or /verify; AuthError(UNEXPECTED) when it has none."""
    access, refresh = answer.get("access_token"), answer.get("refresh_token")
    if not isinstance(access, str) or not isinstance(refresh, str) or not access or not refresh:
        raise AuthError(UNEXPECTED, 200)
    return Session(access, refresh)


class SupabaseAuth:
    """Calls to one Supabase project's Auth API, and the check of its access tokens.

    `url` is the project's URL (https://<project-ref>.supabase.co) and `publishable_key` its publishable key (or the
    legacy anon key). `http` and `clock` are injectable, so tests use an in-memory Supabase. One instance serves the
    whole website: the client and the cached signing keys are safe to share between threads.
    """

    def __init__(self, url: str, publishable_key: str, *, http: httpx.Client | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.issuer = url.rstrip("/") + AUTH_PATH  # also the base address of every request
        self._key = publishable_key
        self._http = http or httpx.Client(timeout=TIMEOUT)
        self._clock = clock
        self._keys_lock = threading.Lock()
        self._keys: dict[str, jwt.PyJWK] = {}
        self._keys_read_at: float | None = None  # when the keys were last read successfully
        self._keys_tried_at: float | None = None  # when they were last asked for, successfully or not

    def close(self) -> None:
        self._http.close()

    # ------------------------------------------------------------------ requests

    def _request(self, method: str, path: str, body: dict | None = None, *, token: str | None = None,
                 params: dict | None = None) -> dict:
        """One request to the Auth API: the answer's JSON object ({} for an empty answer), or AuthError."""
        headers = {"apikey": self._key, "User-Agent": USER_AGENT, "Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            response = self._http.request(method, self.issuer + path, json=body, params=params, headers=headers)
        except httpx.TransportError:  # timeouts, DNS failures, refused connections
            raise AuthError(UNREACHABLE, 0) from None
        if response.status_code >= 400:
            raise _error(response)
        if not response.content:
            return {}
        try:
            answer = response.json()
        except ValueError:
            raise AuthError(UNEXPECTED, response.status_code) from None
        return answer if isinstance(answer, dict) else {}

    def sign_up(self, email: str, password: str) -> Session | None:
        """Create an account. With email confirmation on (this site's setup) Supabase sends the confirmation email and
        returns no session; for an address that already has an account it answers the same way, so nobody can find
        out who has one. A session comes back only when the project does not ask for confirmation."""
        answer = self._request("POST", "/signup", {"email": email, "password": password})
        return _session(answer) if answer.get("access_token") else None

    def sign_in(self, email: str, password: str) -> Session:
        """Sign in with an email address and a password (refused until the address is confirmed)."""
        return _session(self._request("POST", "/token", {"email": email, "password": password},
                                      params={"grant_type": "password"}))

    def refresh(self, refresh_token: str) -> Session:
        """The next session of a signed-in person. A refresh token works once (Supabase allows its reuse for 10
        seconds, so two requests at once both get a session)."""
        return _session(self._request("POST", "/token", {"refresh_token": refresh_token},
                                      params={"grant_type": "refresh_token"}))

    def verify_email_link(self, token_hash: str, link_type: str) -> Session:
        """Use an email link's token_hash (the address is confirmed, or a password reset starts) and sign in."""
        if link_type not in EMAIL_LINK_TYPES:
            raise ValueError(f"not an email link type: {link_type!r}")
        return _session(self._request("POST", "/verify", {"type": link_type, "token_hash": token_hash}))

    def send_password_reset(self, email: str) -> None:
        """Email a link to choose a new password. Supabase answers the same whether or not the address has an
        account."""
        self._request("POST", "/recover", {"email": email})

    def resend_confirmation(self, email: str) -> None:
        """Email a new confirmation link to an account that waits for its address to be confirmed."""
        self._request("POST", "/resend", {"type": "signup", "email": email})

    def set_password(self, access_token: str, password: str) -> None:
        """Give the signed-in person a new password (after a password-reset link, or from their account page)."""
        self._request("PUT", "/user", {"password": password}, token=access_token)

    def sign_out(self, access_token: str) -> None:
        """End this session at Supabase (scope "local": the person's other devices stay signed in). The access token
        itself stays valid until it expires, which is why the site also deletes its cookies."""
        self._request("POST", "/logout", token=access_token, params={"scope": "local"})

    # ------------------------------------------------------------------ the access token

    def verify_access_token(self, token: str | None) -> User | None:
        """The person an access token stands for, or None when it is not a valid, current token of this project.

        Raises AuthError when Supabase's public keys cannot be read and none are known yet, so the caller can tell
        "nobody is signed in" from "sign-in cannot be checked now".
        """
        if not token or len(token) > MAX_TOKEN_LENGTH:
            return None
        try:
            header = jwt.get_unverified_header(token)
        except jwt.InvalidTokenError:
            return None
        algorithm, key_id = header.get("alg"), header.get("kid")
        if algorithm not in ALGORITHMS or not isinstance(key_id, str):
            return None  # never HS256 or "none": only the project's published asymmetric keys can sign
        key = self._signing_key(key_id)
        if key is None or key.algorithm_name != algorithm:
            return None
        try:
            claims = jwt.decode(token, key.key, algorithms=[algorithm], audience=AUDIENCE, issuer=self.issuer,
                                leeway=LEEWAY_SECONDS, options={"require": ["exp", "iat", "sub", "aud", "iss"]})
        except jwt.InvalidTokenError:  # a bad signature, an expired token, another issuer or audience, ...
            return None
        subject, email = claims.get("sub"), claims.get("email")
        if (claims.get("role") != SIGNED_IN_ROLE or claims.get("is_anonymous") is True
                or not isinstance(subject, str) or not USER_ID.fullmatch(subject)
                or not isinstance(email, str) or "@" not in email):
            return None
        return User(subject.lower(), email)

    def _signing_key(self, key_id: str) -> jwt.PyJWK | None:
        """The public key with this id: from the cache while it is fresh, else from Supabase. An unknown id is looked
        up again at most every JWKS_RETRY_SECONDS. When Supabase cannot be reached, keys read earlier keep working;
        with none at all, AuthError."""
        with self._keys_lock:
            now = self._clock()
            fresh = self._keys_read_at is not None and now - self._keys_read_at < JWKS_CACHE_SECONDS
            if fresh and key_id in self._keys:
                return self._keys[key_id]
            if self._keys_tried_at is not None and now - self._keys_tried_at < JWKS_RETRY_SECONDS:
                if self._keys_read_at is None:  # the only attempt so far failed moments ago
                    raise AuthError(UNREACHABLE, 0)
                return self._keys.get(key_id)  # asked moments ago: no new request
            self._keys_tried_at = now
            try:
                self._keys = self._read_keys()
            except AuthError:
                if not self._keys:
                    raise
                LOG.warning("Supabase's signing keys could not be read again; the ones read before are used.")
            else:
                self._keys_read_at = now
                if not self._keys:
                    LOG.error("Supabase publishes no asymmetric signing key (ES256 or RS256) at %s, so no sign-in "
                              "can be checked. The project must sign its tokens with an asymmetric JWT signing key "
                              "(DEPLOYMENT.md, \"Supabase\").", self.issuer + "/.well-known/jwks.json")
            return self._keys.get(key_id)

    def _read_keys(self) -> dict[str, jwt.PyJWK]:
        """The project's published public keys by id, keeping only ES256 and RS256 keys this module can use."""
        try:
            response = self._http.get(self.issuer + "/.well-known/jwks.json",
                                      headers={"apikey": self._key, "User-Agent": USER_AGENT})
        except httpx.TransportError:
            raise AuthError(UNREACHABLE, 0) from None
        if response.status_code != 200:
            raise AuthError(f"http_{response.status_code}", response.status_code)
        try:
            listed = response.json().get("keys")
        except (ValueError, AttributeError):
            raise AuthError(UNEXPECTED, response.status_code) from None
        keys: dict[str, jwt.PyJWK] = {}
        for data in listed if isinstance(listed, list) else []:
            if not isinstance(data, dict) or data.get("alg") not in ALGORITHMS or not isinstance(data.get("kid"), str):
                continue
            try:
                keys[data["kid"]] = jwt.PyJWK(data)
            except (jwt.PyJWKError, jwt.InvalidKeyError, ValueError, TypeError):
                continue  # a key this module cannot use is ignored, never trusted
        return keys
