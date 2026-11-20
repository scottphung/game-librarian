"""Optional IGDB client (server side only): upcoming games, PopScore signals, Steam ids and catalog suggestions.

IGDB (https://api-docs.igdb.com/, read 2026-11-15 and 2026-11-19) is free for non-commercial use under the
Twitch Developer Services Agreement; its FAQ allows storing and serving the data and asks for visible
attribution to IGDB.com. Authentication uses a Twitch application's Client ID and Client Secret through
the client-credentials grant (twitch.TwitchAppClient, shared with the "Watch it live" streams). The token
is kept in memory only, never stored, logged or shown.

Limits (from the IGDB docs): 4 requests per second, 8 open requests, at most 500 results per query.
This client sends one request at a time, at least 0.3 s apart, and retries 429 and 5xx answers.

Uses:
  * catalog suggestions (candidates.py): popular PC games and the Steam app ids IGDB records for them
    (external_games with external_game_source 1 = Steam; the field replaced the deprecated `category`);
  * the Upcoming & Anticipated lists (upcoming.py): two PopScore popularity types, the upcoming games'
    release dates, platforms, companies, websites and covers, shown with attribution to IGDB.com;
  * linking catalog games to IGDB ids, which is how a game finds its Twitch category (twitch.py).
See README.md, "IGDB (optional)" and "Upcoming & Anticipated".
"""

from __future__ import annotations

from .twitch import TOKEN_URL, TwitchAppClient

__all__ = ["IgdbClient", "IgdbError", "TOKEN_URL", "steam_appids"]

API_BASE = "https://api.igdb.com/v4"
MIN_INTERVAL_SECONDS = 0.3  # IGDB allows 4 requests per second
MAX_ATTEMPTS = 4
BACKOFF_SECONDS = 2.0  # waits 2 s, 4 s, then 8 s between attempts
MAX_RESULTS_PER_QUERY = 500

STEAM_SOURCE = 1  # external_game_sources id of Steam (the old external_games.category value)
PC_PLATFORM = 6  # "PC (Microsoft Windows)"
# game_type values worth suggesting: main game, standalone expansion, remake, remaster, expanded game, port.
# DLC, expansions, bundles, mods, episodes, seasons, forks, packs and updates are left out.
CANDIDATE_GAME_TYPES = (0, 4, 8, 9, 10, 11)


class IgdbError(Exception):
    """IGDB or Twitch could not be reached, refused the credentials, or kept failing."""


class IgdbClient(TwitchAppClient):
    """Small IGDB client with token handling, rate limiting and retries (see TwitchAppClient)."""

    error = IgdbError
    service = "IGDB"
    min_interval_seconds = MIN_INTERVAL_SECONDS
    max_attempts = MAX_ATTEMPTS
    backoff_seconds = BACKOFF_SECONDS

    def _send_query(self, url: str, body: str, *, renew: bool):
        headers = {"Client-ID": self._client_id, "Authorization": f"Bearer {self._access_token(renew=renew)}",
                   "Accept": "application/json"}
        return self._send("POST", url, headers=headers, content=body.encode("utf-8"))

    def query(self, endpoint: str, body: str) -> list[dict]:
        """Run one Apicalypse query (`body`) against an IGDB endpoint, such as "games"."""
        url = f"{API_BASE}/{endpoint}"
        response = self._send_query(url, body, renew=False)
        if response.status_code == 401:  # the token expired or was revoked early: renew it once
            response = self._send_query(url, body, renew=True)
        if response.status_code != 200:
            detail = self._redact(self._message(response))
            raise IgdbError(f"IGDB {endpoint} query failed (HTTP {response.status_code}"
                            + (f": {detail}" if detail else "") + ").")
        try:
            data = response.json()
        except ValueError:
            raise IgdbError(f"IGDB {endpoint} answered with something other than JSON.") from None
        if not isinstance(data, list):
            raise IgdbError(f"IGDB {endpoint} answered with an unexpected shape.")
        return [item for item in data if isinstance(item, dict)]

    def popular_pc_games(self, *, limit: int, offset: int = 0, min_ratings: int = 20) -> list[dict]:
        """PC games with at least `min_ratings` IGDB user and critic ratings, the most rated first.

        Returns at most `limit` games, fetched in pages of up to 500, with each game's name, first
        release date, game type, rating count, IGDB URL and Steam ids (external_games).
        """
        games: list[dict] = []
        while len(games) < limit:
            page_size = min(MAX_RESULTS_PER_QUERY, limit - len(games))
            body = (
                "fields name,first_release_date,game_type,total_rating_count,url,"
                "external_games.external_game_source,external_games.uid;"
                f" where platforms = ({PC_PLATFORM}) & game_type = ({','.join(map(str, CANDIDATE_GAME_TYPES))})"
                f" & total_rating_count >= {int(min_ratings)};"
                f" sort total_rating_count desc; limit {page_size}; offset {int(offset) + len(games)};"
            )
            page = self.query("games", body)
            games.extend(page)
            if len(page) < page_size:
                break
        return games[:limit]

    def popularity(self, popularity_type: int, *, limit: int) -> list[dict]:
        """The `limit` highest PopScore values of one popularity type, highest first, in pages of 500.

        Each row has game_id, value and calculated_at. What a value means depends on the type (upcoming.py).
        """
        rows: list[dict] = []
        while len(rows) < limit:
            page_size = min(MAX_RESULTS_PER_QUERY, limit - len(rows))
            page = self.query("popularity_primitives",
                              f"fields game_id,value,calculated_at; where popularity_type = {int(popularity_type)};"
                              f" sort value desc; limit {page_size}; offset {len(rows)};")
            rows.extend(page)
            if len(page) < page_size:
                break
        return rows[:limit]

    def games_by_id(self, igdb_ids: list[int], fields: str) -> list[dict]:
        """The IGDB game records of `igdb_ids` with the given Apicalypse `fields` list, 500 ids per query."""
        ids = sorted({int(igdb_id) for igdb_id in igdb_ids})
        games: list[dict] = []
        for start in range(0, len(ids), MAX_RESULTS_PER_QUERY):
            chunk = ids[start:start + MAX_RESULTS_PER_QUERY]
            games.extend(self.query("games", f"fields {fields}; where id = ({','.join(map(str, chunk))});"
                                             f" limit {len(chunk)};"))
        return games

    def games_for_steam_appids(self, appids: list[int]) -> list[dict]:
        """IGDB's Steam records (external_games from the Steam source) for these app ids, as {game, uid} rows."""
        uids = sorted({int(appid) for appid in appids})
        rows: list[dict] = []
        for start in range(0, len(uids), MAX_RESULTS_PER_QUERY):
            chunk = uids[start:start + MAX_RESULTS_PER_QUERY]
            quoted = ",".join(f'"{uid}"' for uid in chunk)
            rows.extend(self.query("external_games", f"fields game,uid; where external_game_source = {STEAM_SOURCE}"
                                                     f" & uid = ({quoted}); limit {MAX_RESULTS_PER_QUERY};"))
        return rows


def steam_appids(game: dict) -> list[int]:
    """The distinct Steam app ids IGDB records for a game (external games from the Steam source)."""
    appids = []
    for external in game.get("external_games") or []:
        if not isinstance(external, dict) or external.get("external_game_source") != STEAM_SOURCE:
            continue
        uid = str(external.get("uid") or "").strip()
        if uid.isdigit() and int(uid) not in appids:
            appids.append(int(uid))
    return appids
