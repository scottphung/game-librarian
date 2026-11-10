"""Storage: an existing catalog from an earlier version is upgraded in place, without data loss."""

import sqlite3

from game_librarian import store


def test_initialize_adds_new_columns_to_an_existing_catalog_and_keeps_its_rows(tmp_path):
    db_path = tmp_path / "old.sqlite3"
    old = sqlite3.connect(db_path)
    old_schema = store.SCHEMA.replace(
        "    preview_clip           TEXT,                 -- JSON {title, webm, mp4, poster}: Steam's trailer clip\n", ""
    ).replace("    screenshot_url         TEXT,                 -- first all-ages store screenshot\n", "")
    assert "preview_clip" not in old_schema and "screenshot_url" not in old_schema
    old.executescript(old_schema)
    old.execute("INSERT INTO games (appid, curated_title, name, review_total) VALUES (10, 'Old Game', 'Old Game', 42)")
    old.commit()
    old.close()

    conn = store.initialize(db_path)
    try:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(games)")}
        assert {"preview_clip", "screenshot_url"} <= columns
        row = conn.execute("SELECT * FROM games WHERE appid = 10").fetchone()
        assert (row["name"], row["review_total"], row["preview_clip"]) == ("Old Game", 42, None)
    finally:
        conn.close()
    store.initialize(db_path).close()  # running it again changes nothing
