"""Paths, environment settings and Steam URLs shared by the sync command and the web app."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values

PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = PACKAGE_DIR.parent

DEFAULT_DB_PATH = PROJECT_DIR / "data" / "game_librarian.sqlite3"
DEFAULT_CURATED_DIR = PROJECT_DIR / "curated"
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
    """Runtime settings. Every value can be overridden with an environment variable."""

    db_path: Path
    curated_dir: Path
    steam_api_key: str | None
    review_language: str
    dashboard_url: str | None = None


def load_settings() -> Settings:
    """Read the project .env, with environment variables taking precedence."""
    values = {**{k: v for k, v in dotenv_values(PROJECT_DIR / ".env").items() if v is not None}, **os.environ}
    return Settings(
        db_path=Path(values.get("GAME_LIBRARIAN_DB", DEFAULT_DB_PATH)),
        curated_dir=Path(values.get("GAME_LIBRARIAN_CURATED_DIR", DEFAULT_CURATED_DIR)),
        steam_api_key=values.get("STEAM_API_KEY") or None,
        review_language=values.get("GAME_LIBRARIAN_REVIEW_LANGUAGE", "english"),
        dashboard_url=values.get("GAME_LIBRARIAN_DASHBOARD_URL") or None,
    )
