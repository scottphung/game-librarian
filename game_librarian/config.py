"""Paths, environment settings and Steam URLs shared by the sync command and the web app."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values

from .database import PostgresDatabase

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

# A sign-in provider as Container Apps' built-in authentication names it in /.auth/login/<name>: "github", "google",
# "aad" (Microsoft), "facebook", "x", or the name given to a custom OpenID Connect provider.
PROVIDER_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,39}")
TRUE_WORDS = {"1", "true", "yes", "on"}


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
    # The hosted site's PostgreSQL database (GAME_LIBRARIAN_DATABASE_URL); None: the local SQLite file at db_path.
    # Hidden from repr(), because a URL may hold a password (the documented way keeps it in PGPASSWORD instead).
    database_url: str | None = field(default=None, repr=False)
    # GAME_LIBRARIAN_HOSTED: this process serves other people from a container, whose files do not last. It needs
    # the PostgreSQL database, never treats a request as coming from this computer, and its sync fails when a source
    # it should refresh has no credentials, instead of skipping it quietly.
    hosted: bool = False
    # GAME_LIBRARIAN_SIGN_IN_PROVIDERS: the providers of Container Apps' built-in authentication. Only when it is set
    # are the identity headers that the platform adds trusted (accounts.py); empty: nobody can sign in.
    sign_in_providers: tuple[str, ...] = ()
    # GAME_LIBRARIAN_ALLOWED_USERS: who may keep an account (names as the provider gives them, compared without case);
    # empty: everyone who signs in.
    allowed_users: frozenset[str] = frozenset()

    @property
    def igdb_configured(self) -> bool:
        """IGDB and Twitch share these credentials (a Twitch application's client id and secret)."""
        return bool(self.igdb_client_id and self.igdb_client_secret)

    @property
    def database(self) -> Path | PostgresDatabase:
        """Where the catalog is: the PostgreSQL database when GAME_LIBRARIAN_DATABASE_URL is set, else the SQLite file."""
        return PostgresDatabase(self.database_url) if self.database_url else self.db_path

    @property
    def local_evaluation(self) -> bool:
        """Whether Gamalytic's and TwitchTracker's figures may be collected and shown: their terms allow no sharing,
        so they are kept only in the local SQLite database and shown only to a browser on the computer that runs the
        site. Never with the PostgreSQL database, and never in a hosted process."""
        return not self.database_url and not self.hosted


def steam_country_code(value: str | None) -> str:
    """A valid two-letter store region from the setting, or the default "US" when it is empty or malformed."""
    code = (value or "").strip().upper()
    return code if COUNTRY_CODE.fullmatch(code) else DEFAULT_STEAM_COUNTRY


class SettingsError(ValueError):
    """A setting that cannot be used; the message names it and never repeats a secret."""


def _words(value: str | None) -> list[str]:
    """The comma-separated items of a setting, trimmed, without empty ones: " a, b ," -> ["a", "b"]."""
    return [item.strip() for item in (value or "").split(",") if item.strip()]


def sign_in_providers(value: str | None) -> tuple[str, ...]:
    """The configured sign-in providers, lowercase and without repeats; SettingsError for a malformed name."""
    providers = tuple(dict.fromkeys(item.lower() for item in _words(value)))
    for provider in providers:
        if not PROVIDER_NAME.fullmatch(provider):
            raise SettingsError(f"GAME_LIBRARIAN_SIGN_IN_PROVIDERS: {provider!r} is not a provider name such as "
                                "github, google or aad.")
    return providers


def load_settings() -> Settings:
    """Read the project .env, with environment variables taking precedence.

    Raises SettingsError, or database.DatabaseConfigError (both ValueErrors), for a setting that cannot work, such as
    a hosted process without its PostgreSQL database, so the problem stops the start instead of losing data later.
    """
    values = {**{k: v for k, v in dotenv_values(PROJECT_DIR / ".env").items() if v is not None}, **os.environ}
    settings = Settings(
        db_path=Path(values.get("GAME_LIBRARIAN_DB", DEFAULT_DB_PATH)),
        curated_dir=Path(values.get("GAME_LIBRARIAN_CURATED_DIR", DEFAULT_CURATED_DIR)),
        steam_api_key=values.get("STEAM_API_KEY") or None,
        review_language=values.get("GAME_LIBRARIAN_REVIEW_LANGUAGE", "english"),
        igdb_client_id=values.get("IGDB_CLIENT_ID") or None,
        igdb_client_secret=values.get("IGDB_CLIENT_SECRET") or None,
        steam_country=steam_country_code(values.get("GAME_LIBRARIAN_STEAM_COUNTRY")),
        contact=(values.get("GAME_LIBRARIAN_CONTACT") or "").strip() or None,
        database_url=(values.get("GAME_LIBRARIAN_DATABASE_URL") or "").strip() or None,
        hosted=(values.get("GAME_LIBRARIAN_HOSTED") or "").strip().lower() in TRUE_WORDS,
        sign_in_providers=sign_in_providers(values.get("GAME_LIBRARIAN_SIGN_IN_PROVIDERS")),
        allowed_users=frozenset(name.casefold() for name in _words(values.get("GAME_LIBRARIAN_ALLOWED_USERS"))),
    )
    if settings.hosted and not settings.database_url:
        raise SettingsError("GAME_LIBRARIAN_HOSTED is set, so the catalog must be the PostgreSQL database: set "
                            "GAME_LIBRARIAN_DATABASE_URL (and PGPASSWORD). A SQLite file inside a container is lost "
                            "when the container restarts.")
    if settings.database_url:
        settings.database.parameters()  # a malformed URL or a weak sslmode stops here, with a DatabaseConfigError
    return settings
