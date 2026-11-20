"""Steam prices for display: whether a stored price is fresh enough to show, and how to describe it.

Prices are supporting information. They come from one Steam store region (the
GAME_LIBRARIAN_STEAM_COUNTRY setting, US by default) and are only as current as the last
sync, while Steam can change a price at any moment. So every price says which store and
currency it is for and when it was checked, and Steam stays the place to confirm it.

Rules (pure; `now` is passed in):
- A card shows a sale badge ("−30% on Steam") only when all of these hold: Steam reported a
  discount; Steam allows showing it as a percentage; the price was checked within FRESH_FOR;
  and the discount's end date, when Steam gave one, has not passed.
- The details always describe the stored price with its region, currency and check time.
  After FRESH_FOR it is marked as possibly out of date; a discount whose end date has passed
  is reported as ended, without a current price, because the new price is not known.
- No stored price means "not checked yet", never "free". Steam's own free flag is the only
  source of "Free".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from .prepare import FREE, NOT_SOLD, PRICED, UNAVAILABLE

# A daily sync plus a margin: older prices may have changed and lose their sale badge.
FRESH_FOR = timedelta(hours=36)
MINUS = "−"  # a minus sign, which screen readers read as "minus" (a hyphen may be read as "dash")


@dataclass(frozen=True)
class SaleBadge:
    """A current Steam discount for a catalog card."""

    percent: int
    region: str
    checked_at: datetime
    ends_at: datetime | None

    @property
    def label(self) -> str:
        """'−30% on Steam'."""
        return f"{MINUS}{self.percent}% on Steam"


def _moment(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def is_fresh(row, now: datetime) -> bool:
    """True when a stored price row was checked within FRESH_FOR of `now`."""
    checked = _moment(row["checked_at"]) if row is not None else None
    return checked is not None and now - checked <= FRESH_FOR


def discount_ended(row, now: datetime) -> bool:
    """True when the stored price is a discount whose end date, given by Steam, has passed."""
    ends = _moment(row["discount_ends_at"]) if row is not None else None
    return bool(row is not None and row["discount_percent"] and ends is not None and ends <= now)


def sale_badge(row, now: datetime) -> SaleBadge | None:
    """The card's sale badge for a stored price row, or None when no current discount can be shown."""
    if row is None or row["status"] != PRICED or not row["discount_percent"] or row["discount_hidden"]:
        return None
    if not is_fresh(row, now) or discount_ended(row, now):
        return None
    return SaleBadge(percent=row["discount_percent"], region=row["country_code"],
                     checked_at=_moment(row["checked_at"]), ends_at=_moment(row["discount_ends_at"]))


def price_view(row, now: datetime, game_name: str) -> dict:
    """What the details say about the price: the state and the facts to show with it.

    state is one of: not_checked, priced, discounted, discount_ended, free, not_sold, unavailable.
    """
    if row is None:
        return {"state": "not_checked"}
    view = {
        "region": row["country_code"],
        "currency": row["currency"],
        "checked_at": row["checked_at"],
        "stale": not is_fresh(row, now),
    }
    if row["status"] == FREE:
        return {**view, "state": "free"}
    if row["status"] in (NOT_SOLD, UNAVAILABLE):
        return {**view, "state": row["status"]}
    option = row["purchase_option"]
    view.update(
        final=row["final_text"],
        original=row["original_text"],
        percent=row["discount_percent"],
        percent_shown=bool(row["discount_percent"]) and not row["discount_hidden"],
        ends_at=row["discount_ends_at"],
        # The option's name only when it is not just the game's own name, such as a bundle.
        option=option if option and option.casefold() != game_name.casefold() else None,
    )
    if discount_ended(row, now):
        return {**view, "state": "discount_ended"}
    return {**view, "state": "discounted" if row["discount_percent"] else "priced"}
