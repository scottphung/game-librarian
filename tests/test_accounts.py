"""Accounts and private wishlists (accounts.py, web.py): who is signed in, and that each person reaches only their own
list. The identity headers below are the ones Azure Container Apps' built-in authentication adds; on the hosted site
the platform removes any that a browser sends itself. The names and ids are made up.
"""

from __future__ import annotations

import base64
import json
import logging
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from game_librarian import accounts
from game_librarian.accounts import Identity, identity_from_headers, import_entries, is_allowed, wishlist_entry
from game_librarian.web import create_app
from tests.test_web import games  # noqa: F401 (the catalog fixture)


def who(subject: str, name: str, provider: str = "github") -> dict:
    """The headers the platform adds for a signed-in person."""
    return {"X-MS-CLIENT-PRINCIPAL-ID": subject, "X-MS-CLIENT-PRINCIPAL-NAME": name,
            "X-MS-CLIENT-PRINCIPAL-IDP": provider}


ALICE, BOB = who("1001", "alice"), who("2002", "bob")


def client_for(settings, **changes) -> TestClient:
    return TestClient(create_app(replace(settings, **changes), twitch_client=lambda: None))


@pytest.fixture
def site(settings) -> TestClient:
    """A site with GitHub sign-in turned on (GAME_LIBRARIAN_SIGN_IN_PROVIDERS=github)."""
    return client_for(settings, sign_in_providers=("github",))


def saved(client: TestClient, person: dict) -> list[str]:
    answer = client.get("/api/wishlist", headers=person)
    assert answer.status_code == 200, answer.text
    return [item["id"] for item in answer.json()["items"]]


def toggle(client: TestClient, person: dict, key: str, name: str = "A game", aliases: list[str] | None = None):
    return client.post("/api/wishlist/toggle", json={"id": key, "name": name, "aliases": aliases or []},
                       headers=person)


# ---------------------------------------------------------------------- who is signed in


def test_identity_headers_count_only_when_sign_in_is_configured():
    assert identity_from_headers(ALICE, ()) is None  # sign-in off: a header a browser sends changes nothing
    assert identity_from_headers(ALICE, ("github",)) == Identity("github", "1001", "alice")
    assert identity_from_headers({"X-MS-CLIENT-PRINCIPAL-NAME": "alice"}, ("github",)) is None  # no id
    assert identity_from_headers({**ALICE, "X-MS-CLIENT-PRINCIPAL-ID": "9" * 300}, ("github",)) is None
    assert identity_from_headers({**ALICE, "X-MS-CLIENT-PRINCIPAL-IDP": "Not a name!"}, ("github",)) is None


def test_the_provider_and_name_come_from_the_headers_the_principal_or_the_only_provider():
    principal = base64.b64encode(json.dumps({"auth_typ": "google", "claims": [
        {"typ": "name", "val": "Alice Example"}, {"typ": "email", "val": "alice@example.com"}]}).encode()).decode()
    found = identity_from_headers({"X-MS-CLIENT-PRINCIPAL-ID": "g-1", "X-MS-CLIENT-PRINCIPAL": principal},
                                  ("github", "google"))
    assert found == Identity("google", "g-1", "Alice Example") and found.provider_label == "Google"
    only_id = {"X-MS-CLIENT-PRINCIPAL-ID": "1001"}
    assert identity_from_headers(only_id, ("github",)) == Identity("github", "1001", "1001")
    assert identity_from_headers(only_id, ("github", "google")) is None  # the same id at two providers: two people
    damaged = {**only_id, "X-MS-CLIENT-PRINCIPAL": "%%% not base64 %%%"}
    assert identity_from_headers(damaged, ("aad",)) == Identity("aad", "1001", "1001")
    assert accounts.provider_label("aad") == "Microsoft"


def test_the_allow_list_return_paths_and_sign_in_addresses():
    person = Identity("github", "1001", "Alice")
    assert is_allowed(person, frozenset())  # no list: everyone who signs in
    assert is_allowed(person, frozenset({"alice"})) and is_allowed(person, frozenset({"github:alice"}))
    assert not is_allowed(person, frozenset({"google:alice", "bob"}))
    for unsafe in (None, "", "https://evil.example/", "//evil.example/", "/\\evil", "wishlist"):
        assert accounts.safe_return_path(unsafe) == "/"
    assert accounts.safe_return_path("/wishlist?x=1") == "/wishlist?x=1"
    assert accounts.sign_in_url("github", "/wishlist") == "/.auth/login/github?post_login_redirect_uri=%2Fwishlist"
    assert accounts.sign_in_url("github", "https://evil.example/") == \
        "/.auth/login/github?post_login_redirect_uri=%2F"
    assert accounts.SIGN_OUT_URL == "/.auth/logout?post_logout_redirect_uri=%2F"


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


# ---------------------------------------------------------------------- the website


def test_without_sign_in_there_are_no_accounts_and_forged_headers_change_nothing(settings):
    client = client_for(settings)
    page = client.get("/wishlist", headers=ALICE).text
    assert 'data-wishlist="off"' in page and "Saved only in this browser on this device" in page
    assert 'href="/account"' not in page and "Sign in" not in page
    assert client.get("/account", headers=ALICE).status_code == 404
    answer = client.get("/api/wishlist", headers=ALICE)
    assert answer.status_code == 404 and answer.json()["error"] == "sign_in_off"


def test_a_signed_out_visitor_keeps_the_browser_wishlist_and_is_offered_sign_in(site):
    page = site.get("/wishlist").text
    assert 'data-wishlist="browser"' in page and "Saved only in this browser on this device" in page
    assert '<a class="text-link" href="/account?next=/wishlist">Sign in</a>' in page
    assert 'aria-label="Sign in"' in page  # the header's account icon
    account = site.get("/account").text
    assert 'href="/.auth/login/github?post_login_redirect_uri=%2Fwishlist">Sign in with GitHub</a>' in account
    assert "never sees your password" in account
    answer = site.get("/api/wishlist")
    assert answer.status_code == 401 and answer.json()["error"] == "sign_in_required"
    assert toggle(site, {}, "570").status_code == 401


def test_each_account_reads_and_changes_only_its_own_wishlist(site):
    assert toggle(site, ALICE, "570", "Dota 2").json()["saved"] is True
    assert toggle(site, ALICE, "101", "Game 01").json()["saved"] is True
    assert toggle(site, BOB, "730", "Counter-Strike 2").json()["saved"] is True
    assert saved(site, ALICE) == ["570", "101"] and saved(site, BOB) == ["730"]
    assert site.post("/api/wishlist/remove", json={"id": "570"}, headers=BOB).status_code == 200  # Bob has no 570
    assert saved(site, ALICE) == ["570", "101"]
    assert toggle(site, BOB, "570").json()["items"] == [{"id": "730", "name": "Counter-Strike 2"},
                                                         {"id": "570", "name": "A game"}]
    assert saved(site, ALICE) == ["570", "101"]  # Bob's own 570 is his
    assert saved(site, who("1001", "alice", provider="google")) == []  # the same id at Google is another person
    # The browser wishlist's alias rule: a game saved under its Steam app id counts as saved under its IGDB key.
    removed = toggle(site, ALICE, "igdb-2963", "Dota 2", aliases=["570"]).json()
    assert removed["saved"] is False and saved(site, ALICE) == ["101"]
    page = site.get("/wishlist", headers=ALICE).text
    assert 'data-wishlist="account"' in page and "Saved in your account (GitHub: alice)" in page
    assert site.get("/account", headers=ALICE).text.count("Your wishlist holds 1 game.") == 1


def test_a_browser_wishlist_is_added_only_when_asked_and_only_what_is_missing(site):
    toggle(site, ALICE, "570", "Dota 2")
    answer = site.post("/api/wishlist/import", headers=ALICE, json={"items": [
        {"id": "570", "name": "Dota 2"}, {"id": "101", "name": "Game 01"}, {"id": "igdb-5", "name": "Game 05"}]})
    assert answer.status_code == 200
    body = answer.json()
    assert (body["added"], body["already_saved"], body["not_added_full"]) == (2, 1, 0)
    assert [item["id"] for item in body["items"]] == ["570", "101", "igdb-5"]  # the browser's order after 570
    assert saved(site, BOB) == []
    bad = site.post("/api/wishlist/import", headers=ALICE, json={"items": [{"id": "../etc", "name": "x"}]})
    assert bad.status_code == 400 and saved(site, ALICE) == ["570", "101", "igdb-5"]


def test_a_full_wishlist_takes_no_more_games(site, monkeypatch):
    monkeypatch.setattr(accounts, "WISHLIST_LIMIT", 2)
    toggle(site, ALICE, "1")
    toggle(site, ALICE, "2")
    full = toggle(site, ALICE, "3")
    assert full.status_code == 409 and full.json()["error"] == "wishlist_full" and saved(site, ALICE) == ["1", "2"]
    body = site.post("/api/wishlist/import", headers=BOB, json={"items": [{"id": "7", "name": "x"},
                                                                          {"id": "8", "name": "y"}]}).json()
    assert body["added"] == 2
    body = site.post("/api/wishlist/import", headers=BOB, json={"items": [{"id": "9", "name": "z"}]}).json()
    assert (body["added"], body["not_added_full"]) == (0, 1)


def test_changes_need_a_json_request_from_this_sites_own_pages(site):
    form = site.post("/api/wishlist/toggle", data={"id": "570", "name": "x"}, headers=ALICE)
    assert form.status_code == 415  # a form on another site could send this without the visitor knowing
    cross = site.post("/api/wishlist/toggle", json={"id": "570", "name": "x"},
                      headers={**ALICE, "Sec-Fetch-Site": "cross-site"})
    assert cross.status_code == 403 and cross.json()["error"] == "cross_site"
    same = site.post("/api/wishlist/toggle", json={"id": "570", "name": "x"},
                     headers={**ALICE, "Sec-Fetch-Site": "same-origin"})
    assert same.status_code == 200 and saved(site, ALICE) == ["570"]
    big = site.post("/api/wishlist/import", headers={**ALICE, "Content-Type": "application/json"},
                    content=b'{"items": [' + b'{"id": "1", "name": "x"},' * 20_000 + b"]}")
    assert big.status_code == 400  # larger than any wishlist: not read


def test_the_allow_list_keeps_accounts_to_a_small_circle(settings):
    client = client_for(settings, sign_in_providers=("github",), allowed_users=frozenset({"alice"}))
    assert toggle(client, ALICE, "570").status_code == 200
    refused = toggle(client, BOB, "570")
    assert refused.status_code == 403 and refused.json()["error"] == "not_allowed"
    page = client.get("/account", headers=BOB).text
    assert "not on this site’s list of allowed accounts" in page and "Sign out" in page
    assert 'data-wishlist="browser"' in client.get("/wishlist", headers=BOB).text


def test_deleting_account_data_removes_the_account_and_its_wishlist(site):
    toggle(site, ALICE, "570")
    toggle(site, BOB, "730")
    assert site.post("/api/account/delete", json={}, headers=ALICE).json() == {"deleted": True}
    assert saved(site, ALICE) == [] and saved(site, BOB) == ["730"]
    assert site.post("/api/account/delete", data="x", headers=ALICE).status_code == 415


def test_a_site_without_the_platforms_sign_in_stops_trusting_identity_headers(site, caplog):
    assert saved(site, ALICE) == []
    with caplog.at_level(logging.ERROR, logger="game_librarian.web"):
        answer = site.get("/.auth/login/github")  # with built-in authentication on, the platform answers this
    assert answer.status_code == 503 and "Signing in is not available on this server" in answer.text
    assert "built-in authentication is not in front of it" in caplog.text
    after = site.get("/api/wishlist", headers=ALICE)
    assert after.status_code == 404 and 'data-wishlist="off"' in site.get("/wishlist", headers=ALICE).text


def test_pages_for_a_signed_in_person_are_never_cached_and_the_details_say_where_the_list_is(site, games):
    page = site.get("/wishlist", headers=ALICE)
    assert page.headers["cache-control"] == "no-store"
    assert "cache-control" not in site.get("/static/style.css", headers=ALICE).headers
    assert "no-store" not in site.get("/wishlist").headers.get("cache-control", "")
    assert "The Game Librarian wishlist is saved in your account." in site.get("/games/101", headers=ALICE).text
    assert "The Game Librarian wishlist is saved in this browser only." in site.get("/games/101").text
