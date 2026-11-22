"""Signed-in visitors of the hosted site, from Azure Container Apps' built-in authentication.

Container Apps' built-in authentication (often called "Easy Auth") runs in a sidecar in front of the app. It signs a
visitor in with an identity provider (GitHub, Google, Microsoft and others), keeps the session in its own cookie, and
passes each request of a signed-in visitor on with X-MS-CLIENT-PRINCIPAL-* headers. Microsoft's documentation says:
"External requests aren't allowed to set these headers, so they're present only if set by Container Apps."

So this module reads those headers only when GAME_LIBRARIAN_SIGN_IN_PROVIDERS says that built-in authentication is
on. Otherwise a header that a browser sends is ignored and nobody is signed in. The app never sees a password or a
provider's token, and sets no cookie of its own: signing in and out are the platform's /.auth/login/<provider> and
/.auth/logout. A person is identified by the provider's name and the provider's id for them, never by a name that can
change; GAME_LIBRARIAN_ALLOWED_USERS can limit accounts to a small circle.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import dataclass
from urllib.parse import quote

PRINCIPAL_ID = "x-ms-client-principal-id"  # "An identifier that the identity provider sets for the caller"
PRINCIPAL_NAME = "x-ms-client-principal-name"  # "A human-readable name ... such as an email address"
PRINCIPAL_IDP = "x-ms-client-principal-idp"  # "The name of the identity provider"
PRINCIPAL = "x-ms-client-principal"  # "A Base64-encoded JSON representation of available claims"
MAX_FIELD_LENGTH = 256  # a longer id or name is refused; no provider sends one
WISHLIST_LIMIT = 500  # games one account can save
MAX_NAME_LENGTH = 200  # characters of a game name kept with a saved game
MAX_ALIASES = 10
PROVIDER_LABELS = {"aad": "Microsoft", "github": "GitHub", "google": "Google", "facebook": "Facebook", "x": "X",
                   "apple": "Apple"}
# A provider's name as built-in authentication uses it in /.auth/login/<name>: "github", "aad", a custom OIDC name.
PROVIDER = re.compile(r"[a-z0-9][a-z0-9_-]{0,39}")
# A game key (identity.py), as static/wishlist.js accepts it: a Steam app id such as "271590", or "igdb-52189".
GAME_KEY = re.compile(r"(?:\d{1,10}|igdb-\d{1,10})")
# Claims that can name a person when the -NAME header is missing (OpenID Connect names and Microsoft's own).
NAME_CLAIMS = ("name", "preferred_username", "email", "emails",
               "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/name",
               "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress")


@dataclass(frozen=True)
class Identity:
    """A signed-in person, as the platform names them."""

    provider: str  # the identity provider's name in built-in authentication, such as "github"
    subject: str  # the provider's id for the person: it does not change, so it identifies the account
    name: str  # a human-readable name (a user name or an email address), shown and matched by the allow list

    @property
    def provider_label(self) -> str:
        return provider_label(self.provider)


@dataclass(frozen=True)
class AccountState:
    """What a page needs to know about sign-in for one request."""

    enabled: bool  # GAME_LIBRARIAN_SIGN_IN_PROVIDERS is set and built-in authentication answers in front of the app
    identity: Identity | None  # who is signed in, if anyone
    allowed: bool  # the signed-in person may keep an account (GAME_LIBRARIAN_ALLOWED_USERS)
    providers: tuple[str, ...] = ()

    @property
    def signed_in(self) -> bool:
        """Signed in with an account they may use: their wishlist is kept on the server."""
        return self.identity is not None and self.allowed

    @property
    def mode(self) -> str:
        """For the page's scripts: "account" (server wishlist), "browser" (signed out) or "off" (no sign-in)."""
        return "account" if self.signed_in else ("browser" if self.enabled else "off")


def provider_label(provider: str) -> str:
    """The provider's name for people: "github" -> "GitHub", "aad" -> "Microsoft", "my-oidc" -> "My-oidc"."""
    return PROVIDER_LABELS.get(provider, provider.capitalize())


def _principal(value: str) -> dict:
    """The decoded X-MS-CLIENT-PRINCIPAL header (auth_typ, claims, ...), or {} when it is missing or damaged."""
    if not value or len(value) > 16_384:
        return {}
    try:
        data = json.loads(base64.b64decode(value, validate=False))
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _claim(principal: dict, names: tuple[str, ...]) -> str:
    """The first of `names` among the principal's claims ({"typ", "val"} objects), or ""."""
    claims = principal.get("claims")
    values = {claim.get("typ"): claim.get("val") for claim in claims if isinstance(claim, dict)} \
        if isinstance(claims, list) else {}
    for name in names:
        if isinstance(values.get(name), str) and values[name].strip():
            return values[name]
    return ""


def identity_from_headers(headers, providers: tuple[str, ...]) -> Identity | None:
    """The person a request comes from, or None when nobody is signed in or sign-in is off.

    `headers` is a case-insensitive mapping (Starlette's request.headers). The provider's id for the person is
    required; the provider comes from the -IDP header or the principal's auth_typ, and only when exactly one provider
    is configured can it be assumed, because the same id at two providers could be two people.
    """
    if not providers:
        return None
    found = {key.lower(): value for key, value in headers.items()}  # header names are case-insensitive
    subject = (found.get(PRINCIPAL_ID) or "").strip()
    if not subject or len(subject) > MAX_FIELD_LENGTH:
        return None
    principal = _principal(found.get(PRINCIPAL) or "")
    provider = str(found.get(PRINCIPAL_IDP) or principal.get("auth_typ") or "").strip().lower()
    if not provider and len(providers) == 1:
        provider = providers[0]
    if not PROVIDER.fullmatch(provider):
        return None
    name = (found.get(PRINCIPAL_NAME) or _claim(principal, NAME_CLAIMS) or subject).strip()
    return Identity(provider, subject, name[:MAX_FIELD_LENGTH])


def is_allowed(identity: Identity, allowed_users: frozenset[str]) -> bool:
    """Whether the person may keep an account: anyone when the list is empty, else a listed name ("octocat") or a
    provider and name ("github:octocat"), compared without case."""
    if not allowed_users:
        return True
    name = identity.name.casefold()
    return name in allowed_users or f"{identity.provider}:{name}" in allowed_users


def safe_return_path(value: str | None) -> str:
    """`value` when it is a path on this site ("/wishlist?x=1"), else "/": a sign-in never returns to another site."""
    if not value or not value.startswith("/") or value.startswith("//") or "\\" in value or len(value) > 512:
        return "/"
    return value


def sign_in_url(provider: str, return_to: str | None) -> str:
    """The platform's sign-in address for `provider`, coming back to `return_to` (a path here) afterwards."""
    return f"/.auth/login/{quote(provider)}?post_login_redirect_uri={quote(safe_return_path(return_to), safe='')}"


SIGN_OUT_URL = "/.auth/logout?post_logout_redirect_uri=" + quote("/", safe="")


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
