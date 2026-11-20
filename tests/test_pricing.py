"""Steam prices: reading GetItems purchase options, freshness, sale badges, and what the details say."""

from datetime import datetime, timedelta, timezone

import pytest

from game_librarian import pricing
from game_librarian.prepare import FREE, NOT_SOLD, PRICED, UNAVAILABLE, price_from_store_item

NOW = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)
# IStoreBrowseService/GetItems answers in the US store, checked live on 2026-11-19 (trimmed).
LIVE_REBIRTH = {"appid": 2909400, "success": 1, "best_purchase_option": {
    "packageid": 1036725, "purchase_option_name": "FINAL FANTASY VII REBIRTH", "final_price_in_cents": "749",
    "original_price_in_cents": "4999", "formatted_final_price": "$7.49", "formatted_original_price": "$49.99",
    "discount_pct": 85, "active_discounts": [{"discount_amount": "4250", "discount_end_date": 1796234400}],
    "hide_discount_pct_for_compliance": False, "price_cannot_be_displayed_as_discount": False}}
LIVE_HADES = {"appid": 1145360, "success": 1, "is_free": None, "best_purchase_option": {
    "packageid": 415372, "purchase_option_name": "Hades", "final_price_in_cents": "2499",
    "formatted_final_price": "$24.99", "hide_discount_pct_for_compliance": False}}
LIVE_CS2 = {"appid": 730, "success": 1, "is_free": True, "best_purchase_option": None}
LIVE_GTA_V_LEGACY = {"appid": 271590, "success": 1, "is_free": None, "best_purchase_option": None}


def test_live_shaped_answers_are_read_without_guessing():
    rebirth = price_from_store_item(LIVE_REBIRTH)
    assert (rebirth.status, rebirth.final_cents, rebirth.original_cents, rebirth.discount_percent) == (PRICED, 749, 4999, 85)
    assert (rebirth.final_text, rebirth.original_text) == ("$7.49", "$49.99")
    assert rebirth.discount_ends_at == datetime.fromtimestamp(1796234400, timezone.utc).isoformat(timespec="seconds")
    hades = price_from_store_item(LIVE_HADES)
    assert (hades.final_cents, hades.original_cents, hades.discount_percent, hades.original_text) == (2499, 2499, 0, None)
    assert price_from_store_item(LIVE_CS2).status == FREE  # free only because Steam says is_free
    assert price_from_store_item(LIVE_GTA_V_LEGACY).status == NOT_SOLD  # no purchase option is not "free"
    broken = {"best_purchase_option": {"final_price_in_cents": "n/a"}}
    assert price_from_store_item(broken).status == NOT_SOLD


def test_the_earliest_active_discount_end_and_compliance_flags_are_kept():
    option = {**LIVE_REBIRTH["best_purchase_option"], "price_cannot_be_displayed_as_discount": True,
              "active_discounts": [{"discount_end_date": 1796234400}, {"discount_end_date": 1796000000}]}
    facts = price_from_store_item({"best_purchase_option": option})
    assert facts.discount_hidden is True
    assert facts.discount_ends_at == datetime.fromtimestamp(1796000000, timezone.utc).isoformat(timespec="seconds")


def price_row(**overrides) -> dict:
    """A stored price row (store.prices) as the web layer reads it."""
    row = {"country_code": "US", "currency": "USD", "status": PRICED, "final_cents": 749, "original_cents": 4999,
           "discount_percent": 85, "final_text": "$7.49", "original_text": "$49.99",
           "discount_ends_at": "2026-12-02T18:00:00+00:00", "discount_hidden": 0,
           "purchase_option": "FINAL FANTASY VII REBIRTH", "checked_at": "2026-11-19T06:00:00+00:00"}
    return {**row, **overrides}


def test_a_fresh_current_discount_gets_a_badge():
    badge = pricing.sale_badge(price_row(), NOW)
    assert badge.label == "−85% on Steam" and badge.region == "US"
    edge = (NOW - pricing.FRESH_FOR).isoformat()
    assert pricing.sale_badge(price_row(checked_at=edge), NOW) is not None  # exactly 36 hours old is still fresh


@pytest.mark.parametrize("row", [
    None,  # never checked
    price_row(discount_percent=0, original_cents=749, original_text=None),  # no discount
    price_row(checked_at=(NOW - pricing.FRESH_FOR - timedelta(seconds=1)).isoformat()),  # too old to trust
    price_row(discount_ends_at="2026-11-19T11:59:00+00:00"),  # Steam's end date has passed
    price_row(discount_hidden=1),  # Steam asks not to show it as a discount
    price_row(status=FREE, final_cents=None, discount_percent=0),
    price_row(status=NOT_SOLD, final_cents=None, discount_percent=0),
], ids=["missing", "no-discount", "stale", "ended", "hidden", "free", "not-sold"])
def test_no_badge_without_a_fresh_visible_current_discount(row):
    assert pricing.sale_badge(row, NOW) is None


def test_the_details_describe_each_price_state_with_region_currency_and_check_time():
    assert pricing.price_view(None, NOW, "Game")["state"] == "not_checked"
    view = pricing.price_view(price_row(), NOW, "FINAL FANTASY VII REBIRTH")
    assert (view["state"], view["region"], view["currency"], view["stale"]) == ("discounted", "US", "USD", False)
    assert view["percent_shown"] and view["option"] is None  # the option is just the game's own name
    assert pricing.price_view(price_row(purchase_option="Big Bundle"), NOW, "Game")["option"] == "Big Bundle"
    assert pricing.price_view(price_row(discount_percent=0), NOW, "Game")["state"] == "priced"
    assert pricing.price_view(price_row(discount_ends_at="2026-11-18T00:00:00+00:00"), NOW, "Game")["state"] \
        == "discount_ended"
    assert pricing.price_view(price_row(checked_at="2026-11-10T00:00:00+00:00"), NOW, "Game")["stale"] is True
    for status in (FREE, NOT_SOLD, UNAVAILABLE):
        assert pricing.price_view(price_row(status=status), NOW, "Game")["state"] == status
