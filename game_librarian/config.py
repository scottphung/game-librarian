"""Paths, environment settings and Steam URLs shared by the sync command and the web app."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values

PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = PACKAGE_DIR.parent

DEFAULT_DB_PATH = PROJECT_DIR / "data" / "game_librarian.sqlite3"
DEFAULT_CURATED_DIR = PROJECT_DIR / "curated"
# The Steam store region whose prices the sync reads. Steam prices differ by region and currency.
DEFAULT_STEAM_COUNTRY = "US"
# A two-letter country code as Steam's store uses it, such as "US" or "GB" (ISO 3166-1 alpha-2).
COUNTRY_CODE = re.compile(r"[A-Z]{2}")
# The Power BI project and the CSV tables it reads (written by `export-report`).
REPORTING_DIR = PROJECT_DIR / "reporting"
DEFAULT_REPORT_DATA_DIR = REPORTING_DIR / "data"
REPORT_PARAMETERS_FILE = REPORTING_DIR / "GameLibrarian.SemanticModel" / "definition" / "expressions.tmdl"

STORE_API_BASE = "https://store.steampowered.com/api"
WEB_API_BASE = "https://api.steampowered.com"
STORE_PAGE_URL = "https://store.steampowered.com/app/{appid}/"
# Asset file names from IStoreBrowseService are relative to this CDN folder.
ASSET_BASE_URL = "https://shared.akamai.steamstatic.com/store_item_assets/"
# Trailer clip ("microtrailer") file names from IStoreBrowseService are relative to this CDN folder:
# the same folder appdetails uses for its trailer streams (checked live on 2026-11-09).
TRAILER_BASE_URL = "https://video.akamai.steamstatic.com/store_trailers/"

USER_AGENT = "GameLibrarian/0.1 (personal game discovery app)"


@dataclass(frozen=True)
class Settings:
    """Runtime settings. Every value can be overridden with an environment variable.

    Secrets are left out of repr(), so printing or logging the settings never shows them.
    """

    db_path: Path
    curated_dir: Path
    steam_api_key: str | None = field(repr=False)
    review_language: str
    # Optional IGDB access (a Twitch application's credentials); without them IGDB is simply not used.
    igdb_client_id: str | None = field(default=None, repr=False)
    igdb_client_secret: str | None = field(default=None, repr=False)
    steam_country: str = DEFAULT_STEAM_COUNTRY  # the Steam store region prices are read for
    # Contact information (an email address or website you choose) for the User-Agent that Wikimedia's
    # policy asks automated clients to send. Without it the critic scores are not refreshed.
    contact: str | None = field(default=None, repr=False)

    @property
    def igdb_configured(self) -> bool:
        """IGDB and Twitch share these credentials (a Twitch application's client id and secret)."""
        return bool(self.igdb_client_id and self.igdb_client_secret)


def steam_country_code(value: str | None) -> str:
    """A valid two-letter store region from the setting, or the default "US" when it is empty or malformed."""
    code = (value or "").strip().upper()
    return code if COUNTRY_CODE.fullmatch(code) else DEFAULT_STEAM_COUNTRY


def load_settings() -> Settings:
    """Read the project .env, with environment variables taking precedence."""
    values = {**{k: v for k, v in dotenv_values(PROJECT_DIR / ".env").items() if v is not None}, **os.environ}
    return Settings(
        db_path=Path(values.get("GAME_LIBRARIAN_DB", DEFAULT_DB_PATH)),
        curated_dir=Path(values.get("GAME_LIBRARIAN_CURATED_DIR", DEFAULT_CURATED_DIR)),
        steam_api_key=values.get("STEAM_API_KEY") or None,
        review_language=values.get("GAME_LIBRARIAN_REVIEW_LANGUAGE", "english"),
        igdb_client_id=values.get("IGDB_CLIENT_ID") or None,
        igdb_client_secret=values.get("IGDB_CLIENT_SECRET") or None,
        steam_country=steam_country_code(values.get("GAME_LIBRARIAN_STEAM_COUNTRY")),
        contact=(values.get("GAME_LIBRARIAN_CONTACT") or "").strip() or None,
    )
