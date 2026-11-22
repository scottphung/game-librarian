"""Accounts through Continue with Google, and private wishlists (accounts.py, supabase_auth.py, web.py).

Sign-in is Continue with Google (PKCE) through Supabase Auth, which only this site's server talks to; there is no
password and no email code. A session lives in HttpOnly cookies and is checked on every request with Supabase's
published keys, the sign-out form needs this browser's token, and each person reaches only their own list. The
Supabase here is tests/conftest.FakeSupabase, whose tokens are real signed JWTs; the addresses are made up.
"""

from __future__ import annotations

import base64
import json
import logging
import re
from dataclasses import replace
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from game_librarian import __main__ as cli
from game_librarian import accounts, store, supabase_auth
from game_librarian.accounts import Identity, import_entries, is_allowed, wishlist_entry
from game_librarian.web import create_app
from tests.conftest import FakeSupabase, new_signing_key
from tests.test_web import games, text_of  # noqa: F401 (the catalog fixture)

ADA, GRACE = "ada@example.com", "grace@example.org"
USER_ID = "0b3f5c1e-8f2a-4c6d-9e1b-2a7d4c8e9f01"
NOW = "2026-11-22T10:00:00+00:00"
# Every Supabase endpoint the site may call: whether Google is on, the signing keys, the exchange of Google's one-time
# code, session renewals and sign-out. Never an email endpoint (/otp, /verify, /signup, /recover) or a password.
GOOGLE_ONLY_CALLS = {("GET", "/auth/v1/settings"), ("GET", "/auth/v1/.well-known/jwks.json"),
                     ("POST", "/auth/v1/token?grant_type=pkce"), ("POST", "/auth/v1/token?grant_type=refresh_token"),
                     ("POST", "/auth/v1/logout?scope=local")}
# The pieces of the email-code sign-in that 0.22 drafted and that are gone again.
EMAIL_CODE_PIECES = ('type="email"', 'name="email"', 'name="code"', "/account/send-code", "/account/verify-code",
                     "Send me a code", "Enter your code", "Send a new code", "Use a different email", "with email",
                     'type="password"')


def site(settings, fake: FakeSupabase, **changes) -> TestClient:
    """A browser on the site with accounts, whose Supabase is `fake` (https when the site is hosted)."""
    configured = replace(settings, supabase_url=FakeSupabase.URL, supabase_publishable_key=FakeSupabase.KEY, **changes)
    app = create_app(configured, twitch_client=lambda: None, supabase=fake.client())
    return TestClient(app, base_url="https://testserver" if configured.hosted else "http://testserver",
                      follow_redirects=False)


def token_of(html: str) -> str:
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


def post_form(client: TestClient, page: str, action: str, fields: dict, **kwargs):
    """Open `page`, which gives this browser its form token, then post `fields` to `action` as the page's form does."""
    return client.post(action, data={"csrf_token": token_of(client.get(page).text), **fields}, **kwargs)


def google(client, fake, email=ADA, next_path=None, **options):
    """Continue with Google: start at /auth/google, play Supabase's and Google's part, come back to /auth/callback."""
    started = client.get("/auth/google", params={"next": next_path} if next_path else None)
    assert started.status_code == 303, started.text
    return client.get(fake.google_sign_in(started.headers["location"], email, **options))


def person(settings, fake, email=ADA, **changes) -> TestClient:
    """A browser signed in with Google (the first sign-in creates the account)."""
    client = site(settings, fake, **changes)
    assert google(client, fake, email).status_code == 303
    return client


def saved(client) -> list[str]:
    answer = client.get("/api/wishlist")
    assert answer.status_code == 200, answer.text
    return [item["id"] for item in answer.json()["items"]]


def toggle(client, key, name="A game", aliases=None, **kwargs):
    return client.post("/api/wishlist/toggle", json={"id": key, "name": name, "aliases": aliases or []}, **kwargs)


def set_cookies(client, **cookies) -> None:
    """Replace the browser's cookies, as a browser holding other ones would send them."""
    client.cookies.clear()
    for name, value in cookies.items():
        client.cookies.set(name, value)


def cookie_headers(response, name: str) -> list[str]:
    return [header for header in response.headers.get_list("set-cookie") if header.startswith(f"{name}=")]


def session_of(fake, email=ADA) -> str:
    """The id of the first session Supabase holds for `email`."""
    user_id = fake.user(email)["id"]
    return next(session for session, owner in fake.sessions.items() if owner == user_id)


def unsigned_token(**claims) -> str:
    """A token that claims an algorithm of "none": no signature at all."""
    def part(data: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()
    return f"{part({'alg': 'none', 'typ': 'JWT', 'kid': 'test-key-rs256'})}.{part(claims)}."


def main_of(html: str) -> str:
    """The page's <main>: the card, without the header and footer."""
    return re.search(r"<main.*?</main>", html, re.S).group(0)


# ---------------------------------------------------------------------- the rules, without a website


def test_the_allow_list_return_paths_and_pkce():
    ada = Identity(USER_ID, "Ada@Example.com")
    assert is_allowed(ada, frozenset()) and is_allowed(ada, frozenset({"ada@example.com"}))  # without case
    assert not is_allowed(ada, frozenset({"grace@example.org"}))
    for unsafe in (None, "", "https://evil.example/", "//evil.example/", "/\\evil", "wishlist"):
        assert accounts.safe_return_path(unsafe) == "/"
    assert accounts.safe_return_path("/wishlist?x=1") == "/wishlist?x=1"
    # PKCE: RFC 7636's own example, and verifiers that are new each time and fit the cookie's rule.
    assert supabase_auth.code_challenge("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk") == \
        "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
    verifier = supabase_auth.new_code_verifier()
    assert accounts.CODE_VERIFIER.fullmatch(verifier) and verifier != supabase_auth.new_code_verifier()
    assert accounts.read_oauth_cookie(f"{verifier}~%2Fgames%2F101") == (verifier, "/games/101")
    assert accounts.read_oauth_cookie(f"{verifier}~https%3A%2F%2Fevil.example") == (verifier, "/")
    for damaged in (None, "", "short~%2F", f"{verifier}!~%2F"):
        assert accounts.read_oauth_cookie(damaged) is None


def test_wishlist_requests_are_read_strictly():
    assert wishlist_entry({"id": "570", "name": " Dota 2 ", "aliases": ["igdb-2963"]}) == ("570", "Dota 2",
                                                                                        ["igdb-2963"])
    for bad in (None, [], {"id": 570, "name": "x"}, {"id": "../570", "name": "x"}, {"id": "570", "name": ""},
                {"id": "570", "name": "x", "aliases": ["<script>"]}, {"id": "570", "name": "x", "aliases": "570"}):
        assert wishlist_entry(bad) is None
    assert wishlist_entry({"id": "570", "name": "x" * 500})[1] == "x" * accounts.MAX_NAME_LENGTH
    assert import_entries({"items": [{"id": "570", "name": "A"}, {"id": "570", "name": "B"}]}) == [("570", "A")]
    assert import_entries({"items": [{"id": "570", "name": "A"}, {"id": "bad", "name": "B"}]}) is None
    assert import_entries({"items": [{"id": str(n), "name": "x"} for n in range(accounts.WISHLIST_LIMIT + 1)]}) is None


def test_only_this_projects_signed_current_tokens_identify_anyone(fake_supabase):
    auth = fake_supabase.client()
    good = fake_supabase.access_token(USER_ID, ADA, "session-1")
    assert auth.verify_access_token(good) == supabase_auth.User(USER_ID, ADA)
    header, payload, signature = good.split(".")
    claims = json.loads(base64.urlsafe_b64decode(payload + "=="))
    tampered = base64.urlsafe_b64encode(json.dumps({**claims, "sub": "9" * 8 + USER_ID[8:]}).encode()).rstrip(b"=")
    same_claims = {key: claims[key] for key in ("iss", "aud", "sub", "email", "role", "iat", "exp")}
    for forged in (
        f"{header}.{tampered.decode()}.{signature}",  # another person's id under Ada's signature
        fake_supabase.access_token(USER_ID, ADA, "s", private_key=new_signing_key("RS256")),  # the key id, another key
        fake_supabase.access_token(USER_ID, ADA, "s", private_key=FakeSupabase.KEY, algorithm="HS256"),  # a shared secret
        unsigned_token(**same_claims),  # "none"
        fake_supabase.access_token(USER_ID, ADA, "s", lifetime=-120),  # expired, beyond the 30 seconds' leeway
        fake_supabase.access_token(USER_ID, ADA, "s", iss="https://another-project.supabase.co/auth/v1"),
        fake_supabase.access_token(USER_ID, ADA, "s", aud="anon"),
        fake_supabase.access_token(USER_ID, ADA, "s", role="anon"),
        fake_supabase.access_token(USER_ID, ADA, "s", is_anonymous=True),
        fake_supabase.access_token("not-a-user-id", ADA, "s"),
        fake_supabase.access_token(USER_ID, None, "s"),  # no email address
        fake_supabase.access_token(USER_ID, "", "s"),  # an empty one, as for a Google account without an address
        fake_supabase.access_token(USER_ID, ADA, "s", key_id="unknown-key"),
        "", "not a token", "a.b.c", "x" * 9_000,
    ):
        assert auth.verify_access_token(forged) is None, forged[:40]


def test_the_signing_keys_are_cached_a_new_key_is_read_and_an_outage_keeps_the_known_ones(fake_supabase, caplog):
    auth = fake_supabase.client()
    keys = "/auth/v1/.well-known/jwks.json"
    first = fake_supabase.access_token(USER_ID, ADA, "s")
    assert auth.verify_access_token(first) and auth.verify_access_token(first)
    assert fake_supabase.count("GET", keys) == 1  # read once, then kept (10 minutes, like Supabase's edge)
    fake_supabase.private_key, fake_supabase.key_id = new_signing_key("RS256"), "rotated-key"  # a new signing key
    rotated = fake_supabase.access_token(USER_ID, ADA, "s")
    assert auth.verify_access_token(rotated) is None  # moments after the last read, an unknown key id asks nothing
    fake_supabase.clock = 31.0
    assert auth.verify_access_token(rotated) and fake_supabase.count("GET", keys) == 2  # later: its key is read
    assert auth.verify_access_token(fake_supabase.access_token(USER_ID, ADA, "s", key_id="nobody's")) is None
    assert fake_supabase.count("GET", keys) == 2  # an unknown key id moments later asks nothing
    fake_supabase.down, fake_supabase.clock = True, 700.0  # the cache is old and Supabase does not answer
    with caplog.at_level(logging.WARNING, logger="game_librarian.supabase_auth"):
        assert auth.verify_access_token(rotated)  # the keys read before keep working
    assert "the ones read before are used" in caplog.text
    fresh = fake_supabase.client()  # a new process that never read the keys
    with pytest.raises(supabase_auth.AuthError) as raised:
        fresh.verify_access_token(rotated)
    assert raised.value.code == supabase_auth.UNREACHABLE  # "cannot check now", not "nobody is signed in"
    fake_supabase.down = False
    fake_supabase.publish_keys = False  # a project still on a shared HS256 secret publishes no key
    with caplog.at_level(logging.ERROR, logger="game_librarian.supabase_auth"):
        assert fake_supabase.client().verify_access_token(rotated) is None
    assert "publishes no asymmetric signing key" in caplog.text


def test_whether_google_is_on_is_read_from_supabase_and_kept_for_five_minutes(fake_supabase, caplog):
    auth = fake_supabase.client()
    settings_path = "/auth/v1/settings"
    assert auth.google_enabled() is True and auth.google_enabled() is True
    assert fake_supabase.count("GET", settings_path) == 1
    fake_supabase.google = False  # turned off in the project
    fake_supabase.clock = supabase_auth.SETTINGS_CACHE_SECONDS - 1
    assert auth.google_enabled() is True  # still the kept answer
    fake_supabase.clock = supabase_auth.SETTINGS_CACHE_SECONDS + 1
    with caplog.at_level(logging.WARNING, logger="game_librarian.supabase_auth"):
        assert auth.google_enabled() is False and fake_supabase.count("GET", settings_path) == 2
    assert "Google provider is off, so nobody can sign in" in caplog.text  # the owner hears of it, once per read
    fake_supabase.down, fake_supabase.clock = True, 1_000.0
    with caplog.at_level(logging.WARNING, logger="game_librarian.supabase_auth"):
        assert auth.google_enabled() is False  # Supabase silent: the last answer stays
    assert "settings could not be read (unreachable)" in caplog.text
    assert auth.google_enabled() is False and fake_supabase.count("GET", settings_path) == 3  # no new try for 30 s
    assert fake_supabase.client().google_enabled() is None  # a new process that never had an answer: unknown


def test_supabases_errors_become_codes_and_sentences_of_this_site():
    def answer(status, body):
        return supabase_auth._error(SimpleNamespace(status_code=status, json=lambda: body))

    expired = answer(422, {"code": 422, "error_code": "flow_state_expired", "msg": "invalid flow state"})
    assert expired.code == "flow_state_expired" and accounts.auth_problem(expired) == (400, accounts.GOOGLE_EXPIRED)
    assert answer(404, {"code": "flow_state_not_found", "message": "x"}).code == "flow_state_not_found"  # newer API
    assert answer(400, {"error": "invalid_grant", "error_description": "x"}).code == "invalid_grant"
    limited = answer(429, {"code": 429, "msg": "x"})
    assert limited.code == "over_request_rate_limit" and accounts.auth_problem(limited)[0] == 429
    assert accounts.auth_problem(answer(500, {}))[1] == accounts.UNAVAILABLE
    assert accounts.auth_problem(supabase_auth.AuthError(supabase_auth.UNREACHABLE, 0))[0] == 503
    for code in ("flow_state_not_found", "flow_state_expired", "bad_code_verifier"):
        assert accounts.auth_problem(answer(400, {"error_code": code})) == (400, accounts.GOOGLE_EXPIRED)

    def refusal(code):  # one of Supabase's refusals at its Google callback, by its error code
        return accounts.auth_problem(supabase_auth.AuthError(code, 400))

    assert refusal("signup_disabled")[0] == 403 and "New accounts cannot be created" in refusal("signup_disabled")[1]
    unverified = refusal("provider_email_needs_verification")
    assert unverified[0] == 403 and "Google has not verified the email address" in unverified[1]
    assert refusal("user_banned") == (403, "This account cannot sign in.")
    assert refusal("provider_disabled")[0] == 503
    assert refusal("unexpected_failure") == (400, accounts.GOOGLE_UNFINISHED)


def test_the_visitor_address_on_the_hosted_site_is_the_last_one_the_proxy_added():
    def request(forwarded, host="100.100.0.5"):
        return SimpleNamespace(headers={"x-forwarded-for": forwarded} if forwarded else {},
                               client=SimpleNamespace(host=host))

    assert accounts.client_address(request("9.9.9.9, 8.8.4.4"), hosted=True) == "8.8.4.4"  # nothing after the proxy's
    assert accounts.client_address(request("8.8.4.4, 10.1.2.3"), hosted=True) == "8.8.4.4"  # an inner hop is skipped
    assert accounts.client_address(request("[2606:4700::1111]:443"), hosted=True) == "2606:4700::1111"
    assert accounts.client_address(request("8.8.4.4:5000"), hosted=True) == "8.8.4.4"
    assert accounts.client_address(request("8.8.4.4"), hosted=False) == "100.100.0.5"  # here the header is not read
    assert accounts.client_address(request(None), hosted=True) == "100.100.0.5"
    assert accounts.client_address(SimpleNamespace(headers={}, client=None), hosted=True) == "unknown"


def test_the_attempt_limiter_counts_per_visitor_and_forgets():
    moment = [0.0]
    limiter = accounts.AttemptLimiter(2, 60, clock=lambda: moment[0])
    assert limiter.allow("visitor") and limiter.allow("visitor")
    assert not limiter.allow("visitor")  # the visitor's two are used; the refused one is not counted
    assert limiter.allow("another visitor")
    moment[0] = 61.0
    assert limiter.allow("visitor") and limiter.allow("visitor")  # the window has passed


# ---------------------------------------------------------------------- the pages: only Continue with Google


def test_without_supabase_there_are_no_accounts_and_identity_headers_sign_nobody_in(settings, fake_supabase):
    plain = TestClient(create_app(settings, twitch_client=lambda: None))
    page = plain.get("/wishlist").text
    assert 'data-wishlist="off"' in page and 'href="/account"' not in page
    for path in ("/account", "/account/sign-up", "/auth/google", "/auth/callback?code=abcdefgh"):
        assert plain.get(path).status_code == 404, path
    assert plain.post("/account/sign-out", data={}).status_code == 404
    assert plain.get("/api/wishlist").json()["error"] == "sign_in_off"
    # With accounts, 0.20's built-in-authentication headers (or anything else a browser sends) sign nobody in.
    client = site(settings, fake_supabase)
    easy_auth = {"X-MS-CLIENT-PRINCIPAL-ID": "1001", "X-MS-CLIENT-PRINCIPAL-NAME": "octocat",
                 "X-MS-CLIENT-PRINCIPAL-IDP": "github", "X-User-Id": USER_ID}
    assert client.get("/api/wishlist", headers=easy_auth).status_code == 401
    assert 'data-wishlist="browser"' in client.get("/wishlist", headers=easy_auth).text
    assert 'aria-label="Sign in"' in client.get("/wishlist").text  # the header's person icon
    assert client.get("/.auth/login/github").status_code == 404  # GitHub sign-in is not this app's
    for gone in ("/account/forgot-password", "/account/password", "/auth/confirm?token_hash=abcdefgh&type=email"):
        assert client.get(gone).status_code == 404, gone  # 0.21's password pages are gone
    assert client.post("/account/sign-in", data={}).status_code == 404
    assert client.post("/account/sign-up", data={}).status_code == 405


def test_the_sign_in_pages_offer_only_continue_with_google(settings, fake_supabase):
    client = site(settings, fake_supabase)
    page = client.get("/account")
    card = text_of(main_of(page.text))
    assert page.status_code == 200 and "Welcome back" in card and "Your wishlist is waiting." in card
    assert 'href="/auth/google"' in page.text and "Continue with Google" in card
    assert "New to Game Librarian? Create an account" in card and 'href="/account/sign-up"' in page.text
    assert "never asks for a password" in card and "Made an account with your email address before?" in card
    sign_up = client.get("/account/sign-up")
    card = text_of(main_of(sign_up.text))
    assert "Create your account" in card and "Save games you love and find your wishlist on any device." in card
    assert 'href="/auth/google"' in sign_up.text and "Already have an account? Sign in" in card
    for html in (main_of(page.text), main_of(sign_up.text)):  # one button, and no form at all
        assert "<form" not in html and "<input" not in html and html.count("Continue with Google") == 1
        for piece in EMAIL_CODE_PIECES + ("GitHub", "Facebook", "LinkedIn"):
            assert piece not in html, piece
    for path in ("/account/send-code", "/account/verify-code"):  # the email-code routes are gone
        assert client.get(path).status_code == 404 and client.post(path, data={}).status_code == 404, path
    assert fake_supabase.count("GET", "/auth/v1/settings") == 1  # whether Google is on: asked once, then kept


def test_without_googles_provider_or_supabase_the_button_is_disabled_and_says_why(settings, fake_supabase, caplog):
    fake_supabase.google = False  # the project's Google provider is off: nobody can sign in, and the page says so
    off = site(settings, fake_supabase)
    with caplog.at_level(logging.WARNING, logger="game_librarian.supabase_auth"):
        page = off.get("/account").text
    assert '<button class="button google-button" type="button" disabled aria-describedby="google-note">' in page
    assert 'href="/auth/google"' not in page and "Google provider is off" in caplog.text
    assert "Signing in is turned off on this site for now. Your wishlist still works in this browser." in text_of(page)
    refused = off.get("/auth/google")
    assert refused.status_code == 404 and "Signing in is turned off on this site for now." in text_of(refused.text)
    assert not cookie_headers(refused, "gl_oauth") and "location" not in refused.headers
    assert 'data-wishlist="browser"' in off.get("/wishlist").text  # the browser's wishlist works as before
    fake_supabase.google, fake_supabase.down = True, True  # Supabase silent, and never asked before by this site
    away = site(settings, fake_supabase)
    with caplog.at_level(logging.WARNING):
        page = away.get("/account").text
    assert 'disabled aria-describedby="google-note"' in page and 'href="/auth/google"' not in page
    assert "Google sign-in cannot be reached right now. Try again in a minute." in text_of(page)
    started = away.get("/auth/google")
    assert started.status_code == 503 and not cookie_headers(started, "gl_oauth")


def test_visitors_browse_without_signing_in_and_their_pages_ask_supabase_nothing(settings, fake_supabase, games):
    visitor = site(settings, fake_supabase)
    for path in ("/", "/trending", "/games/101", "/wishlist"):
        answer = visitor.get(path)
        assert answer.status_code == 200, path
        assert 'aria-label="Sign in"' in answer.text, path  # offered, never required
    assert 'data-wishlist="browser"' in visitor.get("/wishlist").text
    assert not fake_supabase.requests  # without a session cookie there is nothing to check


# ---------------------------------------------------------------------- Continue with Google


def test_continue_with_google_goes_through_supabase_with_pkce_and_creates_the_account(settings, fake_supabase):
    client = site(settings, fake_supabase, hosted=True)
    started = client.get("/auth/google", params={"next": "/games/101"})
    assert started.status_code == 303
    target = urlsplit(started.headers["location"])
    query = {name: values[0] for name, values in parse_qs(target.query).items()}
    assert f"{target.scheme}://{target.netloc}{target.path}" == f"{FakeSupabase.URL}/auth/v1/authorize"
    assert query == {"provider": "google", "redirect_to": "https://testserver/auth/callback",
                     "code_challenge": query["code_challenge"], "code_challenge_method": "s256"}  # no key, no token
    (cookie,) = cookie_headers(started, "__Host-gl_oauth")
    assert all(part in cookie for part in ("HttpOnly", "Secure", "SameSite=lax", "Path=/", "Max-Age=600"))
    verifier = client.cookies.get("__Host-gl_oauth").partition("~")[0]
    assert query["code_challenge"] == supabase_auth.code_challenge(verifier) != verifier  # only the hash leaves
    back = client.get(fake_supabase.google_sign_in(started.headers["location"], ADA))
    assert back.status_code == 303 and back.headers["location"] == "/games/101"  # where the sign-in started
    assert "Max-Age=0" in cookie_headers(back, "__Host-gl_oauth")[0]  # a verifier is used once
    for name in ("__Host-gl_access", "__Host-gl_refresh"):
        (session_cookie,) = cookie_headers(back, name)
        assert all(part in session_cookie for part in ("HttpOnly", "Secure", "SameSite=lax", "Path=/",
                                                       "Max-Age=2592000"))
    assert fake_supabase.count("POST", "/auth/v1/token") == 1 and back.headers["cache-control"] == "no-store"
    account = text_of(client.get("/account").text)
    assert "Signed in as ada@example.com." in account and "Your wishlist holds 0 games." in account
    assert fake_supabase.user(ADA)["providers"] == ["google"] and len(fake_supabase.users) == 1  # created once
    assert 'aria-label="Account"' in client.get("/wishlist").text
    assert client.get("/account/sign-up").headers["location"] == "/account"  # signed in: nothing to create
    later = site(settings, fake_supabase, hosted=True)  # the same Google account later, in another browser
    assert google(later, fake_supabase, ADA).status_code == 303 and len(fake_supabase.users) == 1
    assert client.get("/auth/google").headers["location"].startswith(f"{FakeSupabase.URL}/auth/v1/authorize?")


def test_a_google_sign_in_finishes_only_in_the_browser_that_started_it(settings, fake_supabase):
    attacker = site(settings, fake_supabase)
    started = attacker.get("/auth/google")
    trap = fake_supabase.google_sign_in(started.headers["location"], "mallory@example.net")  # the attacker's account
    victim = site(settings, fake_supabase)
    landed = victim.get(trap)  # the link sent to someone else: their browser holds no verifier
    assert landed.status_code == 400 and accounts.GOOGLE_EXPIRED in text_of(landed.text)
    assert not cookie_headers(landed, "gl_access") and fake_supabase.count("POST", "/auth/v1/token") == 0
    victim.get("/auth/google")  # the victim's own, unfinished sign-in: another verifier
    crossed = victim.get(trap)
    assert crossed.status_code == 400 and not cookie_headers(crossed, "gl_access")  # Supabase: bad_code_verifier
    assert victim.get("/api/wishlist").status_code == 401
    assert attacker.get(trap).status_code == 303  # the code still works for the browser that started it, once
    # On the hosted site a plain-named verifier cookie, as another app under azurecontainerapps.io could plant, is
    # never read: only __Host-gl_oauth counts.
    hosted_attacker = site(settings, fake_supabase, hosted=True)
    trap = fake_supabase.google_sign_in(hosted_attacker.get("/auth/google").headers["location"], "mallory@example.net")
    planted = site(settings, fake_supabase, hosted=True)
    set_cookies(planted, gl_oauth=hosted_attacker.cookies.get("__Host-gl_oauth"))
    assert planted.get(trap).status_code == 400 and planted.get("/api/wishlist").status_code == 401


def test_google_cancels_and_late_returns_explain_themselves_without_repeating_the_address(settings, fake_supabase,
                                                                                         caplog):
    client = site(settings, fake_supabase)
    cancelled = google(client, fake_supabase, ADA, cancel=True)  # Cancel on Google's screen
    assert cancelled.status_code == 400 and accounts.GOOGLE_CANCELLED in text_of(cancelled.text)
    assert "Max-Age=0" in cookie_headers(cancelled, "gl_oauth")[0] and client.get("/api/wishlist").status_code == 401
    client.get("/auth/google")
    with caplog.at_level(logging.WARNING, logger="game_librarian.web"):
        crafted = client.get("/auth/callback", params={
            "error": "access_denied", "error_code": "access_denied<script>",
            "error_description": "<script>alert(1)</script> Sign in again at evil.example"})
    assert crafted.status_code == 400 and accounts.GOOGLE_UNFINISHED in text_of(crafted.text)
    assert "evil.example" not in crafted.text and "<script>alert" not in crafted.text
    assert "came back with access_deniedscript" in caplog.text and "evil.example" not in caplog.text
    for params in ({"error": "server_error", "error_description": "x"}, {"code": "<not a code>"}):
        client.get("/auth/google")
        failed = client.get("/auth/callback", params=params)
        assert failed.status_code == 400 and accounts.GOOGLE_UNFINISHED in text_of(failed.text), params
    started = client.get("/auth/google")
    late = fake_supabase.google_sign_in(started.headers["location"], ADA)
    fake_supabase.clock += FakeSupabase.FLOW_LIFETIME + 1  # Supabase keeps the one-time code for minutes only
    answer = client.get(late)
    assert answer.status_code == 400 and accounts.GOOGLE_EXPIRED in text_of(answer.text)
    assert "Continue with Google" in answer.text and client.get("/api/wishlist").status_code == 401
    fake_supabase.down = True
    assert site(settings, fake_supabase).get("/auth/google").status_code == 503  # Supabase silent at the start


def test_supabases_refusals_at_the_callback_say_why_and_are_not_called_cancellations(settings, fake_supabase,
                                                                                     caplog):
    fake_supabase.add_user(GRACE, password="from 0.21, made up 1")  # an account from before sign-ups were closed
    fake_supabase.signups_closed = True  # "Allow new users to sign up" turned off in the project
    client = site(settings, fake_supabase)
    with caplog.at_level(logging.WARNING, logger="game_librarian.web"):
        closed = google(client, fake_supabase, "newcomer@example.net")
    closed_text = text_of(closed.text)
    assert closed.status_code == 403 and "New accounts cannot be created on this site right now." in closed_text
    assert "cancelled" not in closed_text and "came back with signup_disabled" in caplog.text
    assert not cookie_headers(closed, "gl_access") and len(fake_supabase.users) == 1
    assert google(client, fake_supabase, GRACE).status_code == 303  # an existing account still signs in
    unverified = google(site(settings, fake_supabase), fake_supabase, "unverified@example.net", verified=False)
    assert unverified.status_code == 403 and "Google has not verified the email address" in text_of(unverified.text)
    with caplog.at_level(logging.ERROR, logger="game_librarian.web"):  # such as a wrong client secret in Supabase
        broken = google(site(settings, fake_supabase), fake_supabase, ADA,
                        supabase_error=("server_error", "unexpected_failure"))
    assert broken.status_code == 400 and accounts.GOOGLE_UNFINISHED in text_of(broken.text)
    assert "Check the Client ID and Client Secret of its Google provider" in caplog.text
    banned = google(site(settings, fake_supabase), fake_supabase, ADA, supabase_error=("access_denied", "user_banned"))
    assert banned.status_code == 403 and "This account cannot sign in." in text_of(banned.text)
    assert not cookie_headers(banned, "gl_access")


def test_accounts_made_with_an_email_address_in_0_21_are_reached_through_google_with_that_address(
        settings, fake_supabase, monkeypatch, capsys):
    confirmed = fake_supabase.add_user(ADA, confirmed=True, password="correct horse 9")  # signed up and confirmed
    pending = fake_supabase.add_user(GRACE, confirmed=False, password="never confirmed 1")  # never confirmed
    moved = fake_supabase.add_user("lin@example.net", confirmed=True, password="another address 2")
    site(settings, fake_supabase)  # the site makes its database
    conn = store.connect(settings.database)
    try:
        for user, items in ((confirmed, [("570", "Dota 2")]), (moved, [("730", "Counter-Strike 2")])):
            account = store.account_id(conn, accounts.PROVIDER, user["id"], user["email"], now=NOW)
            store.import_wishlist_items(conn, account, items, now=NOW, limit=accounts.WISHLIST_LIMIT)
    finally:
        conn.close()
    ada = site(settings, fake_supabase)
    assert google(ada, fake_supabase, "Ada@Example.com").status_code == 303  # Google's address, in its own case
    assert saved(ada) == ["570"]  # Supabase linked Google to Ada's user: the same id, so the same account and games
    assert fake_supabase.user(ADA)["providers"] == ["email", "google"]
    assert fake_supabase.user(ADA)["password"] == "correct horse 9"  # nothing was deleted at Supabase
    grace = site(settings, fake_supabase)
    assert google(grace, fake_supabase, GRACE).status_code == 303
    assert fake_supabase.user(GRACE)["id"] == pending["id"] and saved(grace) == []  # never confirmed: nothing saved
    assert fake_supabase.user(GRACE)["password"] is None  # Supabase drops the unconfirmed password
    # Lin now signs in with a Google account under another address: a new, empty account. Nothing is merged.
    lin = site(settings, fake_supabase)
    assert google(lin, fake_supabase, "lin.new@example.net").status_code == 303
    assert saved(lin) == [] and len(fake_supabase.users) == 4
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    assert cli.main(["accounts", "list"]) == 0
    listed = capsys.readouterr().out
    assert listed.count("supabase  ada@example.com") == 1 and "supabase  lin.new@example.net" in listed
    # The owner, having checked that both addresses are Lin's, copies the games deliberately; both accounts stay.
    conn = store.connect(settings.database)
    try:
        old = store.find_account(conn, accounts.PROVIDER, moved["id"])["id"]
        new = store.find_account(conn, accounts.PROVIDER, fake_supabase.user("lin.new@example.net")["id"])["id"]
    finally:
        conn.close()
    assert cli.main(["accounts", "copy-wishlist", "--from", str(old), "--to", str(new)]) == 0
    assert "1 game(s) added" in capsys.readouterr().out and saved(lin) == ["730"]
    conn = store.connect(settings.database)
    try:
        assert [row["game_key"] for row in store.wishlist_items(conn, old)] == ["730"]  # the old account is unchanged
    finally:
        conn.close()


def test_the_site_itself_never_joins_accounts_by_their_email_address(settings, fake_supabase, caplog):
    ada = person(settings, fake_supabase, ADA)
    toggle(ada, "570", "Dota 2")
    fake_supabase.link_identities = False  # were Supabase to make a second user with the same address
    separate = site(settings, fake_supabase)
    assert google(separate, fake_supabase, ADA).status_code == 303
    assert saved(separate) == [] and saved(ada) == ["570"]  # another Supabase id: another account, nothing merged
    # With "Allow users without an email" on, Supabase could sign in a Google account without an address; this site
    # refuses that session, because the allow list and the account page need a verified address.
    fake_supabase.allow_no_email = True
    nameless = site(settings, fake_supabase)
    with caplog.at_level(logging.ERROR, logger="game_librarian.web"):
        refused = google(nameless, fake_supabase, "")
    assert refused.status_code == 503 and "Google sign-in did not finish on this site." in text_of(refused.text)
    assert not cookie_headers(refused, "gl_access") and "needs a verified email address" in caplog.text


# ---------------------------------------------------------------------- sessions, cookies and isolation


def test_on_the_hosted_site_the_cookies_are_secure_and_only_this_host_can_set_them(settings, fake_supabase):
    client = site(settings, fake_supabase, hosted=True)
    done = google(client, fake_supabase)
    for name in ("__Host-gl_access", "__Host-gl_refresh"):
        (cookie,) = cookie_headers(done, name)
        assert "Secure" in cookie and "HttpOnly" in cookie and "Path=/" in cookie and "Domain" not in cookie
    (csrf,) = cookie_headers(client.get("/account"), "__Host-gl_csrf")  # the account page's form token
    assert "Secure" in csrf and "HttpOnly" in csrf and "Max-Age" not in csrf  # for this browser session only
    assert {"__Host-gl_access", "__Host-gl_refresh", "__Host-gl_csrf"} == set(client.cookies.keys())
    assert client.get("/api/wishlist").status_code == 200
    # Another app under azurecontainerapps.io could set a plain-named cookie for this host; it is never read.
    stranger = site(settings, fake_supabase, hosted=True)
    set_cookies(stranger, gl_access=client.cookies.get("__Host-gl_access"))
    assert stranger.get("/api/wishlist").status_code == 401


def test_signing_in_goes_back_only_to_a_page_of_this_site(settings, fake_supabase):
    for target, landing in (("/wishlist?x=1", "/wishlist?x=1"), ("https://evil.example/", "/"),
                            ("//evil.example/x", "/"), ("/\\evil.example", "/"), (None, "/wishlist")):
        answer = google(site(settings, fake_supabase), fake_supabase, ADA, next_path=target)
        assert answer.status_code == 303 and answer.headers["location"] == landing, target
    page = site(settings, fake_supabase).get("/account", params={"next": "/wishlist?x=1"}).text
    assert 'href="/auth/google?next=/wishlist%3Fx%3D1"' in page
    assert 'href="/account/sign-up?next=/wishlist%3Fx%3D1"' in page


def test_signing_out_ends_the_session_at_supabase_and_deletes_the_cookies(settings, fake_supabase):
    client = person(settings, fake_supabase)
    toggle(client, "570", "Dota 2")
    other_device = person(settings, fake_supabase)  # the same Google account in another browser
    forged = client.post("/account/sign-out", data={"next": "/"})  # no form token: from elsewhere
    assert forged.status_code == 403 and "Your wishlist holds 1 game." in text_of(forged.text)
    assert saved(client) == ["570"] and not fake_supabase.signed_out
    refresh = client.cookies.get("gl_refresh")
    out = post_form(client, "/account", "/account/sign-out", {})
    assert out.status_code == 303 and out.headers["location"] == "/account?notice=signed_out"
    for name in ("gl_access", "gl_refresh"):
        assert "Max-Age=0" in cookie_headers(out, name)[0]
    assert len(fake_supabase.signed_out) == 1 and refresh not in fake_supabase.refresh_tokens  # ended at Supabase
    assert client.get("/api/wishlist").status_code == 401
    assert saved(other_device) == ["570"]  # only this browser's session ended
    page = text_of(client.get("/account?notice=signed_out").text)
    assert "You are signed out." in page and "Welcome back" in page and "Continue with Google" in page
    assert google(client, fake_supabase).status_code == 303 and saved(client) == ["570"]  # the account keeps its games


def test_an_expired_access_token_is_renewed_once_with_the_refresh_token(settings, fake_supabase):
    client = person(settings, fake_supabase)
    expired = fake_supabase.access_token(fake_supabase.user(ADA)["id"], ADA, session_of(fake_supabase), lifetime=-120)
    refresh = client.cookies.get("gl_refresh")
    set_cookies(client, gl_access=expired, gl_refresh=refresh)
    answer = client.get("/api/wishlist")
    assert answer.status_code == 200  # renewed on the way
    assert answer.cookies.get("gl_access") and answer.cookies.get("gl_refresh") not in (None, refresh)
    other = site(settings, fake_supabase)  # a second browser still holding the old pair
    set_cookies(other, gl_access=expired, gl_refresh=refresh)
    gone = other.get("/api/wishlist")
    assert gone.status_code == 401 and "Max-Age=0" in cookie_headers(gone, "gl_refresh")[0]  # worked once: deleted


def test_each_account_reads_and_changes_only_its_own_wishlist(settings, fake_supabase):
    ada, grace = person(settings, fake_supabase, ADA), person(settings, fake_supabase, GRACE)
    assert toggle(ada, "570", "Dota 2").json()["saved"] is True
    assert toggle(ada, "101", "Game 01").json()["saved"] is True
    assert toggle(grace, "730", "Counter-Strike 2").json()["saved"] is True
    assert saved(ada) == ["570", "101"] and saved(grace) == ["730"]
    assert grace.post("/api/wishlist/remove", json={"id": "570"}).status_code == 200  # Grace has no 570
    assert saved(ada) == ["570", "101"]
    # Naming Ada in the body or a header changes nothing: the account comes from the verified session only.
    ada_id = fake_supabase.user(ADA)["id"]
    sneaky = grace.post("/api/wishlist/toggle", headers={"X-User-Id": ada_id},
                        json={"id": "570", "name": "x", "aliases": [], "user_id": ada_id, "email": ADA})
    assert [item["id"] for item in sneaky.json()["items"]] == ["730", "570"] and saved(ada) == ["570", "101"]
    # A token for Ada that Supabase did not sign is nobody's.
    for forged in (fake_supabase.access_token(ada_id, ADA, "s", private_key=new_signing_key("RS256")),
                   unsigned_token(sub=ada_id, email=ADA, role="authenticated", aud="authenticated")):
        intruder = site(settings, fake_supabase)
        set_cookies(intruder, gl_access=forged)
        assert intruder.get("/api/wishlist").status_code == 401
        assert toggle(intruder, "999").status_code == 401
    assert saved(ada) == ["570", "101"]
    # The browser wishlist's alias rule: a game saved under its Steam app id counts as saved under its IGDB key.
    removed = toggle(ada, "igdb-2963", "Dota 2", aliases=["570"]).json()
    assert removed["saved"] is False and saved(ada) == ["101"]
    page = ada.get("/wishlist").text
    assert 'data-wishlist="account"' in page and "Saved in your account (ada@example.com)" in page
    assert ada.get("/account").text.count("Your wishlist holds 1 game.") == 1


def test_a_browser_wishlist_is_added_only_when_asked_and_only_what_is_missing(settings, fake_supabase):
    ada, grace = person(settings, fake_supabase, ADA), person(settings, fake_supabase, GRACE)
    toggle(ada, "570", "Dota 2")
    answer = ada.post("/api/wishlist/import", json={"items": [
        {"id": "570", "name": "Dota 2"}, {"id": "101", "name": "Game 01"}, {"id": "igdb-5", "name": "Game 05"}]})
    assert answer.status_code == 200
    body = answer.json()
    assert (body["added"], body["already_saved"], body["not_added_full"]) == (2, 1, 0)
    assert [item["id"] for item in body["items"]] == ["570", "101", "igdb-5"]  # the browser's order after 570
    assert saved(grace) == []
    bad = ada.post("/api/wishlist/import", json={"items": [{"id": "../etc", "name": "x"}]})
    assert bad.status_code == 400 and saved(ada) == ["570", "101", "igdb-5"]
    page = ada.get("/wishlist").text  # the offer and the removal of the browser's copy are the page's own buttons
    assert "data-wishlist-import-add" in page and "data-wishlist-import-clear" in page


def test_a_full_wishlist_takes_no_more_games(settings, fake_supabase, monkeypatch):
    monkeypatch.setattr(accounts, "WISHLIST_LIMIT", 2)
    ada, grace = person(settings, fake_supabase, ADA), person(settings, fake_supabase, GRACE)
    toggle(ada, "1")
    toggle(ada, "2")
    full = toggle(ada, "3")
    assert full.status_code == 409 and full.json()["error"] == "wishlist_full" and saved(ada) == ["1", "2"]
    two = {"items": [{"id": "7", "name": "x"}, {"id": "8", "name": "y"}]}
    body = grace.post("/api/wishlist/import", json=two).json()
    assert body["added"] == 2
    body = grace.post("/api/wishlist/import", json={"items": [{"id": "9", "name": "z"}]}).json()
    assert (body["added"], body["not_added_full"]) == (0, 1)


# ---------------------------------------------------------------------- forged posts, attempts, outages


def test_the_sign_out_form_needs_this_browsers_form_token_and_a_post_from_this_site(settings, fake_supabase):
    client = person(settings, fake_supabase)
    token = token_of(client.get("/account").text)
    assert client.post("/account/sign-out", data={"csrf_token": "x" * 43}).status_code == 403  # not this browser's
    for fetch_site in ("cross-site", "same-site"):  # same-site: another app under azurecontainerapps.io
        refused = client.post("/account/sign-out", data={"csrf_token": token}, headers={"Sec-Fetch-Site": fetch_site})
        assert refused.status_code == 403 and "did not come from this site" in text_of(refused.text)
    assert client.post("/account/sign-out", json={"csrf_token": token}).status_code == 400  # not a form
    assert not fake_supabase.signed_out and client.get("/api/wishlist").status_code == 200  # still signed in
    done = client.post("/account/sign-out", data={"csrf_token": token}, headers={"Sec-Fetch-Site": "same-origin"})
    assert done.status_code == 303 and len(fake_supabase.signed_out) == 1
    assert client.get("/account").headers["cache-control"] == "no-store"  # a page with a form token is never cached
    assert client.get("/auth/google").headers["cache-control"] == "no-store"


def test_wishlist_changes_need_a_json_request_from_this_sites_own_pages(settings, fake_supabase):
    client = person(settings, fake_supabase)
    assert client.post("/api/wishlist/toggle", data={"id": "570", "name": "x"}).status_code == 415
    cross = toggle(client, "570", headers={"Sec-Fetch-Site": "cross-site"})
    assert cross.status_code == 403 and cross.json()["error"] == "cross_site"
    assert toggle(client, "570", headers={"Sec-Fetch-Site": "same-origin"}).status_code == 200
    assert saved(client) == ["570"]
    big = client.post("/api/wishlist/import", headers={"Content-Type": "application/json"},
                      content=b'{"items": [' + b'{"id": "1", "name": "x"},' * 20_000 + b"]}")
    assert big.status_code == 400  # larger than any wishlist: not read


def test_google_sign_ins_are_counted_per_visitor_before_supabase_is_asked(settings, fake_supabase, monkeypatch):
    monkeypatch.setattr(accounts, "GOOGLE_SIGN_INS", (2, 600))
    client = site(settings, fake_supabase)
    for _ in range(2):
        assert google(client, fake_supabase).status_code == 303
    third = google(client, fake_supabase)
    assert third.status_code == 429 and "Too many attempts. Wait a few minutes, then try again." in text_of(third.text)
    assert fake_supabase.count("POST", "/auth/v1/token") == 2  # the refused attempt never reached Supabase
    hosted = site(settings, fake_supabase, hosted=True)  # on the hosted site each visitor address counts alone
    for address in ("8.8.4.4", "9.9.9.9"):
        for _ in range(2):
            back = fake_supabase.google_sign_in(hosted.get("/auth/google").headers["location"], ADA)
            answer = hosted.get(back, headers={"X-Forwarded-For": f"203.0.113.1, {address}"})
            assert answer.status_code == 303, address


def test_when_supabase_does_not_answer_the_site_says_so_and_keeps_the_session(settings, fake_supabase, caplog):
    client = person(settings, fake_supabase)
    toggle(client, "570")
    other = site(settings, fake_supabase)
    back = fake_supabase.google_sign_in(other.get("/auth/google").headers["location"], GRACE)  # on Google's screen
    fake_supabase.down = True
    with caplog.at_level(logging.WARNING, logger="game_librarian.web"):
        failed = other.get(back)
    assert failed.status_code == 503 and "The sign-in service is not answering right now." in text_of(failed.text)
    assert "Google sign-in failed with unreachable" in caplog.text and GRACE not in caplog.text
    assert saved(client) == ["570"]  # a current access token needs no Supabase: its keys are known
    expired = fake_supabase.access_token(fake_supabase.user(ADA)["id"], ADA, session_of(fake_supabase), lifetime=-120)
    set_cookies(client, gl_access=expired, gl_refresh=client.cookies.get("gl_refresh"))
    answer = client.get("/api/wishlist")  # an expired one cannot be renewed now: nobody is signed in, for now
    assert answer.status_code == 503 and answer.json()["error"] == "sign_in_unavailable"
    assert not cookie_headers(answer, "gl_refresh")  # the cookies stay for later
    assert "Your account could not be reached just now" in client.get("/wishlist").text
    fake_supabase.down = False
    assert saved(client) == ["570"]  # renewed once Supabase answers again


def test_a_project_without_asymmetric_signing_keys_signs_nobody_in(settings, fake_supabase, caplog):
    fake_supabase.publish_keys = False  # a project still on a shared HS256 secret: no published key
    client = site(settings, fake_supabase)
    with caplog.at_level(logging.ERROR):
        answer = google(client, fake_supabase, ADA)
    assert answer.status_code == 503 and "Google sign-in did not finish on this site." in text_of(answer.text)
    assert not cookie_headers(answer, "gl_access")
    assert "publishes no asymmetric signing key" in caplog.text


def test_the_site_calls_only_supabases_google_session_and_key_endpoints(settings, fake_supabase):
    client = person(settings, fake_supabase)
    toggle(client, "570")
    expired = fake_supabase.access_token(fake_supabase.user(ADA)["id"], ADA, session_of(fake_supabase), lifetime=-120)
    set_cookies(client, gl_access=expired, gl_refresh=client.cookies.get("gl_refresh"))
    assert saved(client) == ["570"]  # renewed
    assert post_form(client, "/account", "/account/sign-out", {}).status_code == 303
    grace = person(settings, fake_supabase, GRACE)
    assert grace.post("/api/account/delete", json={}).json() == {"deleted": True}
    called = {(request.method, request.url.raw_path.decode()) for request in fake_supabase.requests}
    assert called == GOOGLE_ONLY_CALLS  # so turning off Supabase's Email provider changes nothing for this site


# ---------------------------------------------------------------------- the allow list, deletion, what pages carry


def test_the_allow_list_keeps_accounts_to_a_small_circle(settings, fake_supabase):
    circle = frozenset({"ada@example.com"})
    ada = person(settings, fake_supabase, ADA, allowed_users=circle)
    grace = person(settings, fake_supabase, GRACE, allowed_users=circle)
    assert toggle(ada, "570").status_code == 200
    refused = toggle(grace, "570")
    assert refused.status_code == 403 and refused.json()["error"] == "not_allowed"
    page = grace.get("/account").text
    assert "not on this site’s list of allowed accounts" in page and "Sign out" in page
    assert 'data-wishlist="browser"' in grace.get("/wishlist").text


def test_deleting_account_data_removes_it_and_signs_out(settings, fake_supabase):
    ada, grace = person(settings, fake_supabase, ADA), person(settings, fake_supabase, GRACE)
    toggle(ada, "570")
    toggle(grace, "730")
    assert ada.post("/api/account/delete", data="x").status_code == 415
    answer = ada.post("/api/account/delete", json={})
    assert answer.json() == {"deleted": True} and "Max-Age=0" in cookie_headers(answer, "gl_access")[0]
    assert ada.get("/api/wishlist").status_code == 401 and saved(grace) == ["730"]
    assert len(fake_supabase.signed_out) == 1  # the session ended at Supabase too
    conn = store.connect(settings.database)
    try:
        assert store.find_account(conn, accounts.PROVIDER, fake_supabase.user(ADA)["id"]) is None
    finally:
        conn.close()
    assert len(fake_supabase.users) == 2  # the Supabase users stay: only the site's owner can delete them there
    notice = text_of(ada.get("/account?notice=deleted").text)
    assert "(your Google identity and its email address) is kept by Supabase" in notice


def test_personal_pages_are_never_cached_and_no_page_carries_a_token_or_the_key(settings, fake_supabase, games):
    ada = person(settings, fake_supabase)
    assert ada.get("/wishlist").headers["cache-control"] == "no-store"
    assert "cache-control" not in ada.get("/static/style.css").headers
    visitor = site(settings, fake_supabase)
    assert "no-store" not in visitor.get("/wishlist").headers.get("cache-control", "")
    assert "The Game Librarian wishlist is saved in your account." in ada.get("/games/101").text
    assert "The Game Librarian wishlist is saved in this browser only." in visitor.get("/games/101").text
    access, refresh = ada.cookies.get("gl_access"), ada.cookies.get("gl_refresh")
    for path in ("/account", "/wishlist", "/games/101", "/api/wishlist", "/trending"):
        body = ada.get(path).text
        assert access not in body and refresh not in body and FakeSupabase.KEY not in body, path
        assert "supabase.co" not in body, path  # the pages send the browser to this site, never to Supabase
    for path in ("/account", "/account/sign-up"):
        body = visitor.get(path).text
        assert FakeSupabase.KEY not in body and "supabase.co" not in body, path
    started = visitor.get("/auth/google")
    assert FakeSupabase.KEY not in started.headers["location"]  # the redirect to Supabase carries no key either
    assert visitor.cookies.get("gl_oauth") not in started.headers["location"]  # nor the verifier


def test_an_es256_project_works_the_same(settings):
    fake = FakeSupabase("ES256")
    ada = person(settings, fake)
    assert toggle(ada, "570").status_code == 200 and saved(ada) == ["570"]
    assert google(site(settings, fake), fake, GRACE).status_code == 303


# ---------------------------------------------------------------------- 0.20's GitHub accounts (the `accounts` command)


def test_old_github_accounts_are_kept_and_moved_only_by_the_owners_decision(settings, fake_supabase, monkeypatch,
                                                                            capsys):
    ada = person(settings, fake_supabase)
    toggle(ada, "730", "Counter-Strike 2")
    conn = store.connect(settings.database)
    try:
        old = store.account_id(conn, "github", "1001", "octocat", now="2026-11-21T22:00:00+00:00")
        store.import_wishlist_items(conn, old, [("570", "Dota 2"), ("730", "Counter-Strike 2")],
                                    now="2026-11-21T22:00:00+00:00", limit=accounts.WISHLIST_LIMIT)
        new = store.find_account(conn, accounts.PROVIDER, fake_supabase.user(ADA)["id"])["id"]
    finally:
        conn.close()
    assert saved(ada) == ["730"]  # nothing is moved by itself, and no address is matched to a GitHub name
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    assert cli.main(["accounts", "list"]) == 0
    out = capsys.readouterr().out
    assert "github    octocat" in out and "supabase  ada@example.com" in out
    assert "1 account(s) from a sign-in this site no longer offers" in out and "nothing is matched by email" in out
    assert cli.main(["accounts", "copy-wishlist", "--from", str(old), "--to", str(new)]) == 0
    out = capsys.readouterr().out
    assert "1 game(s) added, 1 already saved there" in out and f"Account {old} is unchanged" in out
    assert saved(ada) == ["730", "570"]
    assert cli.main(["accounts", "delete", "--id", str(old)]) == 1  # without --yes it only says what would go
    assert "has 2 saved game(s). Nothing was deleted" in capsys.readouterr().out
    assert cli.main(["accounts", "delete", "--id", str(old), "--yes"]) == 0
    assert "Deleted account" in capsys.readouterr().out
    assert cli.main(["accounts", "copy-wishlist", "--from", "99999", "--to", str(new)]) == 1
    assert "no account has the id 99999" in capsys.readouterr().err
    assert cli.main(["accounts", "copy-wishlist", "--from", str(new), "--to", str(new)]) == 1
    assert saved(ada) == ["730", "570"]
