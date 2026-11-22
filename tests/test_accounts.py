"""Email-and-password accounts and private wishlists (accounts.py, supabase_auth.py, web.py).

Supabase Auth is reached through this site's server only, a session lives in HttpOnly cookies and is checked on every
request with Supabase's published keys, forms need this browser's token, and each person reaches only their own list.
The Supabase here is tests/conftest.FakeSupabase, whose tokens are real signed JWTs; the addresses and passwords are
made up.
"""

from __future__ import annotations

import base64
import json
import logging
import re
from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from game_librarian import __main__ as cli
from game_librarian import accounts, store, supabase_auth
from game_librarian.accounts import Identity, import_entries, is_allowed, wishlist_entry
from game_librarian.web import create_app
from tests.conftest import FakeSupabase, new_signing_key
from tests.test_web import games, text_of  # noqa: F401 (the catalog fixture)

PASSWORD = "correct horse 9"
ADA, GRACE = "ada@example.com", "grace@example.org"
USER_ID = "0b3f5c1e-8f2a-4c6d-9e1b-2a7d4c8e9f01"


def site(settings, fake: FakeSupabase, **changes) -> TestClient:
    """A browser on the site with email accounts, whose Supabase is `fake` (https when the site is hosted)."""
    configured = replace(settings, supabase_url=FakeSupabase.URL, supabase_publishable_key=FakeSupabase.KEY, **changes)
    app = create_app(configured, twitch_client=lambda: None, supabase=fake.client())
    return TestClient(app, base_url="https://testserver" if configured.hosted else "http://testserver",
                      follow_redirects=False)


def token_of(html: str) -> str:
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


def post_form(client: TestClient, page: str, action: str, fields: dict, **kwargs):
    """Open `page`, which gives this browser its form token, then post `fields` to `action` as the page's form does."""
    return client.post(action, data={"csrf_token": token_of(client.get(page).text), **fields}, **kwargs)


def sign_up(client, email=ADA, password=PASSWORD, again=None):
    return post_form(client, "/account/sign-up", "/account/sign-up",
                     {"email": email, "password": password, "password_confirm": password if again is None else again})


def open_link(client, fake, email=ADA, kind="email"):
    """Open the newest email link of `kind` sent to `email`, and press its button (the post that uses the link)."""
    link_type = "recovery" if kind == "recovery" else "email"
    token_hash = fake.link(email, kind)
    return post_form(client, f"/auth/confirm?token_hash={token_hash}&type={link_type}", "/auth/confirm",
                     {"token_hash": token_hash, "type": link_type})


def sign_in(client, email=ADA, password=PASSWORD, next_path=None, **kwargs):
    fields = {"email": email, "password": password, **({"next": next_path} if next_path is not None else {})}
    return post_form(client, "/account", "/account/sign-in", fields, **kwargs)


def person(settings, fake, email=ADA, **changes) -> TestClient:
    """A browser signed in with a new account whose address is confirmed."""
    client = site(settings, fake, **changes)
    assert sign_up(client, email).status_code == 200
    assert open_link(client, fake, email).status_code == 303
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
    """The id of the only session Supabase holds for `email`."""
    user_id = fake.user(email)["id"]
    return next(session for session, owner in fake.sessions.items() if owner == user_id)


def unsigned_token(**claims) -> str:
    """A token that claims an algorithm of "none": no signature at all."""
    def part(data: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()
    return f"{part({'alg': 'none', 'typ': 'JWT', 'kid': 'test-key-rs256'})}.{part(claims)}."


# ---------------------------------------------------------------------- the rules, without a website


def test_the_allow_list_return_paths_new_passwords_and_addresses():
    ada = Identity(USER_ID, "Ada@Example.com")
    assert is_allowed(ada, frozenset()) and is_allowed(ada, frozenset({"ada@example.com"}))  # without case
    assert not is_allowed(ada, frozenset({"grace@example.org"}))
    for unsafe in (None, "", "https://evil.example/", "//evil.example/", "/\\evil", "wishlist"):
        assert accounts.safe_return_path(unsafe) == "/"
    assert accounts.safe_return_path("/wishlist?x=1") == "/wishlist?x=1"
    assert accounts.password_problem("short1") == "Choose a password of at least 8 characters."
    assert "at most 72 characters" in accounts.password_problem("é" * 40)  # 80 bytes: bcrypt reads 72
    assert accounts.password_problem(PASSWORD, "something else") == "The two passwords are not the same."
    assert accounts.password_problem(PASSWORD, PASSWORD) is None
    assert accounts.clean_email("  ada@example.com ") == "ada@example.com"
    for bad in (None, "", "ada", "ada@example", "a b@example.com", "@example.com", "x" * 250 + "@example.com"):
        assert accounts.clean_email(bad) is None


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


def test_supabases_errors_become_codes_and_sentences_of_this_site():
    def answer(status, body):
        return supabase_auth._error(SimpleNamespace(status_code=status, json=lambda: body))

    weak = answer(422, {"code": 422, "error_code": "weak_password", "msg": "x", "weak_password": {"reasons": ["length"]}})
    assert (weak.code, weak.reasons) == ("weak_password", ("length",))
    assert accounts.auth_problem(weak) == (400, "Choose a stronger password: make it longer.")
    assert answer(400, {"code": "email_not_confirmed", "message": "x"}).code == "email_not_confirmed"  # newer API
    assert answer(400, {"error": "invalid_grant", "error_description": "x"}).code == "invalid_grant"
    limited = answer(429, {"code": 429, "msg": "x"})
    assert limited.code == "over_request_rate_limit" and accounts.auth_problem(limited)[0] == 429
    assert accounts.auth_problem(answer(500, {}))[1] == accounts.UNAVAILABLE
    assert accounts.auth_problem(supabase_auth.AuthError(supabase_auth.UNREACHABLE, 0))[0] == 503
    assert accounts.auth_problem(answer(403, {"error_code": "otp_expired"})) == (400, accounts.LINK_PROBLEM)


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


def test_the_attempt_limiter_counts_per_visitor_and_per_address_and_forgets():
    moment = [0.0]
    limiter = accounts.AttemptLimiter({"try": ((2, 60), (1, 60))}, clock=lambda: moment[0])
    assert limiter.allow("try", "visitor", "ada@example.com")
    assert not limiter.allow("try", "visitor", "ADA@example.com")  # the same address, without case
    assert limiter.allow("try", "visitor", "grace@example.org")
    assert not limiter.allow("try", "visitor", "third@example.net")  # the visitor's two are used
    assert limiter.allow("try", "another visitor", "third@example.net")
    moment[0] = 61.0
    assert limiter.allow("try", "visitor", "ada@example.com")  # the window has passed


# ---------------------------------------------------------------------- signing up, in and out


def test_without_supabase_there_are_no_accounts_and_identity_headers_sign_nobody_in(settings, fake_supabase):
    plain = TestClient(create_app(settings, twitch_client=lambda: None))
    page = plain.get("/wishlist").text
    assert 'data-wishlist="off"' in page and 'href="/account"' not in page
    for path in ("/account", "/account/sign-up", "/account/forgot-password", "/auth/confirm?token_hash=abcdefgh&type=email"):
        assert plain.get(path).status_code == 404
    assert plain.get("/api/wishlist").json()["error"] == "sign_in_off"
    # With email accounts, 0.20's built-in-authentication headers (or anything else a browser sends) sign nobody in.
    client = site(settings, fake_supabase)
    easy_auth = {"X-MS-CLIENT-PRINCIPAL-ID": "1001", "X-MS-CLIENT-PRINCIPAL-NAME": "octocat",
                 "X-MS-CLIENT-PRINCIPAL-IDP": "github", "X-User-Id": USER_ID}
    assert client.get("/api/wishlist", headers=easy_auth).status_code == 401
    assert 'data-wishlist="browser"' in client.get("/wishlist", headers=easy_auth).text
    assert 'aria-label="Sign in"' in client.get("/wishlist").text  # the header's person icon
    assert client.get("/.auth/login/github").status_code == 404  # GitHub sign-in is not this app's


def test_a_new_account_confirms_its_address_with_a_button_and_is_signed_in(settings, fake_supabase):
    client = site(settings, fake_supabase)
    form = client.get("/account/sign-up").text
    assert 'action="/account/sign-up"' in form and 'autocomplete="new-password"' in form and 'minlength="8"' in form
    created = sign_up(client)
    assert created.status_code == 200 and "A link is on its way to ada@example.com." in text_of(created.text)
    assert [mail["type"] for mail in fake_supabase.sent] == ["email"]
    early = sign_in(client)
    assert early.status_code == 400 and "Confirm your email address first" in text_of(early.text)
    assert 'action="/account/resend"' in early.text  # a new link can be asked for right there
    page = client.get(f"/auth/confirm?token_hash={fake_supabase.link(ADA)}&type=email")
    assert page.status_code == 200 and "Confirm my email address" in page.text
    assert fake_supabase.count("POST", "/auth/v1/verify") == 0  # opening the link uses nothing (link scanners open it)
    used = open_link(client, fake_supabase)
    assert used.status_code == 303 and used.headers["location"] == "/account?notice=confirmed"
    for name in ("gl_access", "gl_refresh"):
        (cookie,) = cookie_headers(used, name)
        assert "HttpOnly" in cookie and "SameSite=lax" in cookie and "Path=/" in cookie and "Max-Age=2592000" in cookie
    account = text_of(client.get("/account?notice=confirmed").text)
    assert "Your email address is confirmed, and you are signed in." in account
    assert "Signed in as ada@example.com." in account and "Your wishlist holds 0 games." in account
    assert 'aria-label="Account"' in client.get("/wishlist").text
    again = open_link(client, fake_supabase)  # a link works once
    assert again.status_code == 400 and "each email link works once" in text_of(again.text)


def test_on_the_hosted_site_the_cookies_are_secure_and_only_this_host_can_set_them(settings, fake_supabase):
    client = site(settings, fake_supabase, hosted=True)
    sign_up(client)
    used = open_link(client, fake_supabase)
    for name in ("__Host-gl_access", "__Host-gl_refresh"):
        (cookie,) = cookie_headers(used, name)
        assert "Secure" in cookie and "HttpOnly" in cookie and "Path=/" in cookie and "Domain" not in cookie
    assert {"__Host-gl_access", "__Host-gl_refresh", "__Host-gl_csrf"} <= set(client.cookies.keys())
    assert not {"gl_access", "gl_refresh", "gl_csrf"} & set(client.cookies.keys())
    assert client.get("/api/wishlist").status_code == 200
    # Another app under azurecontainerapps.io could set a plain-named cookie for this host; it is never read.
    stranger = site(settings, fake_supabase, hosted=True)
    set_cookies(stranger, gl_access=client.cookies.get("__Host-gl_access"))
    assert stranger.get("/api/wishlist").status_code == 401


def test_signing_in_goes_back_only_to_a_page_of_this_site(settings, fake_supabase):
    person(settings, fake_supabase)
    client = site(settings, fake_supabase)  # a signed-out browser
    wrong = sign_in(client, password="not it 1")
    assert wrong.status_code == 400 and "The email address or password is not right." in text_of(wrong.text)
    assert 'value="ada@example.com"' in wrong.text and "not it 1" not in wrong.text  # the address stays, not the password
    for target, landing in (("/wishlist?x=1", "/wishlist?x=1"), ("https://evil.example/", "/"),
                            ("//evil.example/x", "/"), ("/\\evil.example", "/"), (None, "/wishlist")):
        answer = sign_in(client, next_path=target)
        assert answer.status_code == 303 and answer.headers["location"] == landing, target
    visitor = site(settings, fake_supabase)
    assert 'name="next" value="/wishlist?x=1"' in visitor.get("/account", params={"next": "/wishlist?x=1"}).text


def test_signing_out_ends_the_session_at_supabase_and_deletes_the_cookies(settings, fake_supabase):
    client = person(settings, fake_supabase)
    toggle(client, "570", "Dota 2")
    assert client.post("/account/sign-out", data={"next": "/"}).status_code == 403  # no form token: from elsewhere
    assert saved(client) == ["570"] and not fake_supabase.signed_out
    out = post_form(client, "/account", "/account/sign-out", {})
    assert out.status_code == 303 and out.headers["location"] == "/account?notice=signed_out"
    assert cookie_headers(out, "gl_access") and "Max-Age=0" in cookie_headers(out, "gl_access")[0]
    assert len(fake_supabase.signed_out) == 1 and not fake_supabase.refresh_tokens  # Supabase ended the session
    assert client.get("/api/wishlist").status_code == 401
    assert "You are signed out." in text_of(client.get("/account?notice=signed_out").text)
    assert sign_in(client).status_code == 303 and saved(client) == ["570"]  # the account keeps its games


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


def test_forms_need_this_browsers_form_token_and_a_post_from_this_site(settings, fake_supabase):
    client = site(settings, fake_supabase)
    fields = {"email": ADA, "password": PASSWORD, "password_confirm": PASSWORD}
    assert client.post("/account/sign-up", data=fields).status_code == 403  # no token: another site's form
    token = token_of(client.get("/account/sign-up").text)
    assert client.post("/account/sign-up", data={**fields, "csrf_token": "x" * 43}).status_code == 403
    for fetch_site in ("cross-site", "same-site"):  # same-site: another app under azurecontainerapps.io
        refused = client.post("/account/sign-up", data={**fields, "csrf_token": token},
                              headers={"Sec-Fetch-Site": fetch_site})
        assert refused.status_code == 403 and "did not come from this site" in text_of(refused.text)
    assert client.post("/account/sign-up", json={**fields, "csrf_token": token}).status_code == 400  # not a form
    assert fake_supabase.count("POST", "/auth/v1/signup") == 0  # none of them reached Supabase
    ok = client.post("/account/sign-up", data={**fields, "csrf_token": token},
                     headers={"Sec-Fetch-Site": "same-origin"})
    assert ok.status_code == 200 and len(fake_supabase.sent) == 1
    assert client.get("/account").headers["cache-control"] == "no-store"  # a page with a form token is never cached


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


def test_attempts_are_counted_per_visitor_and_per_address_before_supabase_is_asked(settings, fake_supabase,
                                                                                   monkeypatch):
    monkeypatch.setitem(accounts.LIMITS, "sign_in", ((3, 600), (2, 600)))
    client = site(settings, fake_supabase)
    for _ in range(2):
        assert sign_in(client, password="wrong 1").status_code == 400
    third = sign_in(client, password="wrong 1")
    assert third.status_code == 429 and "Too many attempts. Wait a few minutes, then try again." in text_of(third.text)
    assert fake_supabase.count("POST", "/auth/v1/token") == 2  # the refused attempt never reached Supabase
    assert sign_in(client, GRACE, "wrong 1").status_code == 400  # another address: the visitor's third attempt
    assert sign_in(client, "third@example.net", "wrong 1").status_code == 429  # the visitor's three are used
    hosted = site(settings, fake_supabase, hosted=True)  # on the hosted site each visitor address counts alone
    for address in ("8.8.4.4", "9.9.9.9"):
        for _ in range(2):
            answer = sign_in(hosted, f"{address}@example.net", "wrong 1",
                             headers={"X-Forwarded-For": f"203.0.113.1, {address}"})
            assert answer.status_code == 400, address


def test_when_supabase_does_not_answer_the_site_says_so_and_keeps_the_session(settings, fake_supabase, caplog):
    client = person(settings, fake_supabase)
    toggle(client, "570")
    fake_supabase.down = True
    with caplog.at_level(logging.WARNING, logger="game_librarian.web"):
        failed = sign_in(site(settings, fake_supabase))  # another visitor tries to sign in
    assert failed.status_code == 503 and "The sign-in service is not answering right now." in text_of(failed.text)
    assert "sign-in failed with unreachable" in caplog.text and ADA not in caplog.text  # the code, never the address
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
    sign_up(client)
    with caplog.at_level(logging.ERROR):
        used = open_link(client, fake_supabase)
    assert used.status_code == 503 and "Your email address is confirmed, but this site could not sign you in" in \
        text_of(used.text)
    assert not cookie_headers(used, "gl_access")
    assert "publishes no asymmetric signing key" in caplog.text
    refused = sign_in(client)
    assert refused.status_code == 503 and not cookie_headers(refused, "gl_access")


# ---------------------------------------------------------------------- email links, passwords, the allow list


def test_an_email_link_that_is_damaged_or_refused_by_supabase_explains_itself(settings, fake_supabase):
    client = site(settings, fake_supabase)
    for address in ("/auth/confirm", "/auth/confirm?type=email", "/auth/confirm?token_hash=<script>&type=email",
                    "/auth/confirm?token_hash=abcdef123456&type=magic",
                    "/auth/confirm?error=access_denied&error_code=otp_expired&error_description=Email+link+is+invalid"):
        page = client.get(address)
        assert page.status_code == 400 and "each email link works once" in text_of(page.text), address
        assert "<script>" not in page.text and "Email link is invalid" not in page.text  # nothing from the address
    unknown = post_form(client, "/account/forgot-password", "/auth/confirm", {"token_hash": "a" * 56, "type": "email"})
    assert unknown.status_code == 400 and fake_supabase.count("POST", "/auth/v1/verify") == 1
    sign_up(client)
    fake_supabase.responses = [503]  # Supabase down for a moment: the link may still work, so the button stays
    pressed = open_link(client, fake_supabase)
    assert pressed.status_code == 503 and "Confirm my email address" in pressed.text
    assert open_link(client, fake_supabase).status_code == 303


def test_a_forgotten_password_is_replaced_through_a_link_that_works_once(settings, fake_supabase):
    person(settings, fake_supabase)  # Ada's account
    client = site(settings, fake_supabase)  # another browser
    unknown = post_form(client, "/account/forgot-password", "/account/forgot-password", {"email": "nobody@example.net"})
    known = post_form(client, "/account/forgot-password", "/account/forgot-password", {"email": ADA})
    assert text_of(unknown.text).replace("nobody@example.net", ADA) == text_of(known.text)  # nobody learns who has one
    assert [mail["type"] for mail in fake_supabase.sent] == ["email", "recovery"]
    page = client.get(f"/auth/confirm?token_hash={fake_supabase.link(ADA, 'recovery')}&type=recovery")
    assert "Reset your password" in page.text and ">Continue</button>" in page.text
    used = open_link(client, fake_supabase, ADA, "recovery")
    assert used.status_code == 303 and used.headers["location"] == "/account/password?notice=recovery"
    form = client.get("/account/password?notice=recovery").text
    assert "You are signed in. Choose the new password for your account." in form

    def choose(password, again=None):
        return post_form(client, "/account/password", "/account/password",
                         {"password": password, "password_confirm": password if again is None else again})

    assert "The two passwords are not the same." in text_of(choose("new horse 10", "new horse 11").text)
    assert "Choose a password of at least 8 characters." in text_of(choose("short1").text)
    weak = choose("no digits at all")  # Supabase's own rule: this fake project asks for a digit
    assert weak.status_code == 400
    assert "Choose a stronger password: mix in the kinds of characters" in text_of(weak.text)
    assert "different from your current one" in text_of(choose(PASSWORD).text)
    done = choose("new horse 10")
    assert done.status_code == 303 and done.headers["location"] == "/account?notice=password_changed"
    assert "Your new password is saved." in text_of(client.get("/account?notice=password_changed").text)
    assert sign_in(site(settings, fake_supabase), ADA, PASSWORD).status_code == 400  # the old password is gone
    assert sign_in(site(settings, fake_supabase), ADA, "new horse 10").status_code == 303
    assert open_link(client, fake_supabase, ADA, "recovery").status_code == 400  # the link worked once
    signed_out = site(settings, fake_supabase)
    assert signed_out.get("/account/password").headers["location"] == "/account?next=/account/password"


def test_signing_up_never_tells_who_already_has_an_account(settings, fake_supabase, caplog):
    person(settings, fake_supabase)  # Ada's account, confirmed
    client = site(settings, fake_supabase)
    again = sign_up(client, ADA)
    assert again.status_code == 200 and "A link is on its way to ada@example.com." in text_of(again.text)
    assert "If this address already has an account, sign in instead." in text_of(again.text)
    assert len(fake_supabase.sent) == 1  # Supabase sent nothing the second time
    weak = sign_up(client, GRACE, "no digits here")
    assert weak.status_code == 400 and "Choose a stronger password: mix in" in text_of(weak.text)
    assert 'value="grace@example.org"' in weak.text
    assert "The two passwords are not the same." in text_of(sign_up(client, GRACE, PASSWORD, "other 1").text)
    fake_supabase.team_only = True  # Supabase's built-in email service, before custom SMTP
    with caplog.at_level(logging.WARNING, logger="game_librarian.web"):
        blocked = sign_up(client, "new@example.net")
    assert blocked.status_code == 503 and "This site cannot send email yet" in text_of(blocked.text)
    assert "email_address_not_authorized" in caplog.text and "new@example.net" not in caplog.text


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
    assert "Your sign-in (email address and password) is kept by Supabase" in ada.get("/account?notice=deleted").text


def test_personal_pages_are_never_cached_and_no_page_carries_a_token_or_the_key(settings, fake_supabase, games):
    ada = person(settings, fake_supabase)
    assert ada.get("/wishlist").headers["cache-control"] == "no-store"
    assert "cache-control" not in ada.get("/static/style.css").headers
    visitor = site(settings, fake_supabase)
    assert "no-store" not in visitor.get("/wishlist").headers.get("cache-control", "")
    assert "The Game Librarian wishlist is saved in your account." in ada.get("/games/101").text
    assert "The Game Librarian wishlist is saved in this browser only." in visitor.get("/games/101").text
    access, refresh = ada.cookies.get("gl_access"), ada.cookies.get("gl_refresh")
    for path in ("/account", "/wishlist", "/games/101", "/account/password", "/api/wishlist", "/trending"):
        body = ada.get(path).text
        assert access not in body and refresh not in body and FakeSupabase.KEY not in body, path
        assert "supabase.co" not in body, path  # the browser is never sent to Supabase


def test_an_es256_project_works_the_same(settings):
    fake = FakeSupabase("ES256")
    ada = person(settings, fake)
    assert toggle(ada, "570").status_code == 200 and saved(ada) == ["570"]


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
