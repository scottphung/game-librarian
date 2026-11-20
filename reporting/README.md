# Game Librarian Power BI report

A Power BI Project (`GameLibrarian.pbip`) that analyzes the curated catalog: player reception and award coverage.
It is an analytical companion to the website, not a second catalog. It is optional: the website's **Dashboard**
pages show the same pages as HTML, calculated from the same tables with the measure definitions below, without
Power BI (see the main README, "Dashboard").

| Item | Format | Notes |
| --- | --- | --- |
| `GameLibrarian.pbip` | Power BI Project | Open this file in Power BI Desktop. |
| `GameLibrarian.SemanticModel/` | TMDL (text) | Tables, relationships, measures and the `DataFolder` parameter. |
| `GameLibrarian.Report/` | PBIR (JSON) | Three pages and the dark theme (`StaticResources/RegisteredResources/GameLibrarianDark.json`). |
| `data/` | CSV, generated | Written by `python -m game_librarian export-report`. Git-ignored. |

## Refresh the data locally

```powershell
.\.venv\Scripts\python.exe -m game_librarian sync            # Steam data into SQLite (as before)
.\.venv\Scripts\python.exe -m game_librarian export-report   # SQLite + curated CSVs -> reporting\data\*.csv
```

Then open `reporting\GameLibrarian.pbip` in Power BI Desktop and select **Home → Refresh**. A project opened for
the first time has no data until you refresh it.

`export-report` also writes this computer's absolute export folder into the `DataFolder` parameter in
`GameLibrarian.SemanticModel/definition/expressions.tmdl`, because Power BI Desktop needs an absolute path. If you
move the project, run `export-report` again. The path contains your Windows user name; edit the parameter before
sharing the project if that matters to you. The export contains no settings, keys or review text.

## Pages

1. **Player Reception.** Games with review data, total Steam reviews, the review-weighted positive rate and the
   average game positive rate; a scatter of review volume against positive share; the two rates by production
   label; the four-year ranking table (the website's rules: award group, wins, nominations, then Steam reception;
   its subtitle shows the export's window); games by positive-share band. Slicers: tag or genre, production label, award
   status.
2. **Awards Explorer.** Award entries, winners, nominees, catalog games with awards and the share of entries matched
   to the catalog; entries by year (winners and nominees); how entries match Steam by award; a table of every
   verified entry with what the award applies to. Slicers: year (ceremony year), award (organization · category,
   because several organizations use "Game of the Year"), result, match to Steam.
3. **About the data.** Definitions, the ranking rules, the ranking window, coverage limits, refresh steps and the
   loaded export.

The four-year ranking (`Four-Year Rank`, `Ranking Tier`, `Ranking Tier Name`, `Ranking Tier From Original Release`,
`Awards Won`, `Award Nominations`, `In Four-Year Window`, `Rank Status`, `Rank Note`) is calculated by `export-report`
exactly as on the website, with the window ending on the export day. Since 0.9 `Ranking Tier` is the **award group**
(1 The Game Awards — Game of the Year winner … 8 No award recorded); within a group, more `Awards Won`, then more
`Award Nominations`, then Steam reception (500+ reviews first, highest `Positive Share` first, then more reviews)
decide. Production labels play no part. It is Game Librarian's editorial discovery order, not a measure of quality,
and award coverage is incomplete. The rules are in the main README, "Ranking". Refreshing Power BI does not move the
window: run `export-report` first.

No chart shows prices, player counts or trends over time: the project stores only the latest values, so there is
no history to chart. Collecting dated snapshots would be the first step toward trend pages.

## Data model

| Table | Grain (one row per) | Source |
| --- | --- | --- |
| `Games` | visible curated game | `games.csv` |
| `GameTags` | game and Steam tag (bridge) | `game_tags.csv` |
| `Tags` | Steam tag | `tags.csv` |
| `Awards` | verified award result, with or without a Steam app | `awards.csv` |
| `Export Info` | export run (one row) | `export_info.csv` |

Relationships:

| From (many) | To (one) | Cross-filter | Why |
| --- | --- | --- | --- |
| `GameTags[AppID]` | `Games[AppID]` | both | a tag slicer filters games |
| `GameTags[Tag]` | `Tags[Tag]` | single | `Tags` feeds the tag slicer |
| `Awards[Catalog AppID]` | `Games[AppID]` | both | award slicers filter games; entries with no catalog game have a blank key |

Review counts exist only in `Games`. Tags and awards filter games through their own tables, so a game with several
tags or awards is counted once. Verified in Power BI Desktop on 2026-11-15 (238 games): filtering on "RPG or Indie"
returns 149 games and 25,632,046 reviews, not the 30,565,152 that adding the two tags' totals would give.

Because some award entries match no catalog game, Power BI adds a blank placeholder row to `Games`; the two slicers
on `Games` columns carry an "is not blank" filter so the placeholder never appears as a choice.

Measures (each has a description in the model):

| Measure | Definition |
| --- | --- |
| `Game count` | `COUNTROWS(Games)` |
| `Games with review data` | games with a usable review summary |
| `Steam reviews`, `Positive Steam reviews`, `Negative Steam reviews` | sums over the selected games |
| `Review-weighted positive rate` | `DIVIDE([Positive Steam reviews], [Steam reviews])`: pools every review, so big games dominate |
| `Average game positive rate` | `AVERAGE(Games[Positive Share])`: every game counts once |
| `Median game positive rate` | `MEDIAN(Games[Positive Share])` |
| `Games in four-year window`, `Ranked games`, `Unranked games`, `Best four-year rank` | from the four-year ranking (see the main README) |
| `Ranking window` | the release dates the loaded export ranks, such as "Released 9 Nov 2022 – 9 Nov 2026" |
| `Games with a verified label` | production label other than "No verified label" (or "Unclassified", its name in exports made before 0.8) |
| `Promoted games` | catalog games added through the candidate review workflow (`Games[Catalog Source]` = "Promoted candidate") |
| `Award entries`, `Winning entries`, `Nominee entries` | verified award results; a winner is listed once, as Winner |
| `Entries in catalog`, `Entries on Steam outside catalog`, `Entries not mapped to Steam` | where each result matches Steam |
| `Original-release entries` | awards that went to an earlier version than the edition Steam sells |
| `Awarded catalog games` | distinct catalog games with an award result |
| `Catalog coverage of entries` | `DIVIDE([Entries in catalog], [Award entries])` |
| `Data as of` | export time and the sync it reflects |

## Publish and connect (needs an account)

These steps need a Power BI account. Sharing with other people usually needs a Power BI Pro or Premium Per User
license, or a workspace on Fabric/Premium capacity. Check your organization's licensing.

1. In Power BI Desktop, sign in and select **Home → Publish**, then pick a workspace.
2. **Refresh in the Power BI service.** The service cannot read files on your computer by itself. Install and
   configure an on-premises data gateway (personal mode is enough) for the `reporting\data` folder, then refresh
   the semantic model on demand or on a schedule. Without a gateway, refresh in Desktop and publish again.
   Running `sync` or `export-report` never updates a published report on its own.
3. **Share** the report from the service with the people who should see it (workspace role, app or share link).
   Each viewer signs in with their own account. The website no longer embeds or links a published report: its
   Dashboard pages are calculated by the website itself.
4. **Publish to web (public, not recommended).** It creates a link that anyone can open without signing in, and the
   data becomes public. Your organization may disable it. Nothing in this project publishes anything.

## Changes in version 0.9 (2026-11-20): the award-based four-year ranking

- **Ranking rule.** The four-year rank no longer puts AAA, then AA, then the other games first, each by positive
  review count. It now orders games by award group (the highest-priority verified award result), then distinct
  verified wins, then distinct verified nominations, then Steam reception ("Best player reception": at least 500
  reviews first, highest positive share first, then more reviews), then title and app ID. A game with an award keeps
  its rank without usable review data; a game with neither an award nor usable reviews is Unranked.
- **Columns (no schema break).** No table, column, measure or relationship was removed or renamed.
  - `games.csv` `ranking_tier` / `Games[Ranking Tier]` (1–8) and `ranking_tier_name` / `Games[Ranking Tier Name]`
    now hold the award group and its name instead of the production tier. Visuals or filters of your own that
    expect "AAA", "AA" or "Indie or no verified label" there need the production label (`Games[Production Label]`)
    instead.
  - New: `award_nominations` / `Games[Award Nominations]` and `ranking_tier_original_release` /
    `Games[Ranking Tier From Original Release]` (TRUE when every award result that sets the group went to the
    original release of the edition Steam sells).
  - `award_entries`, `award_wins` and `top_award_wins` count distinct results. `check-curated` now also reports a
    verified award row that repeats an earlier result, and every table leaves it out, so neither `Games` nor
    `Awards` counts it twice. Today's curated data has no such row, so these values did not change.
- **Report.** The Player Reception rank table shows Rank, Game, Award group, Wins, Nominations, % positive and
  Total reviews, titled "Four-year ranking: awards, then Steam reception". The About the data page and the
  descriptions of the ranking columns and measures describe the new rule.
- **Manual steps, in this order:**
  1. `.\.venv\Scripts\python.exe -m game_librarian export-report` (a pre-0.9 export lacks the two new columns, so
     the model cannot refresh from it: "column not found").
  2. Close Power BI Desktop if `GameLibrarian.pbip` is open; Desktop does not reload edited report files.
  3. Open `reporting\GameLibrarian.pbip` and select **Home → Refresh**.
  4. A copy published to the Power BI service changes only after you refresh it there (gateway) or republish it.
- **Checked offline:** TOM (`TmdlSerializer`, Power BI Desktop 2.159.1231.0) deserialized the edited model with 0
  `Validate()` errors and no case-insensitive name clashes, the two new columns are bound to the exported CSV
  columns, all 70 field references in the visuals exist in the model, all report JSON parses, and the tests check
  that the model reads exactly the exported columns and that the export ranks exactly like the website and the
  Dashboard. **Not checked:** the project was not opened or refreshed in Power BI Desktop for 0.9; the rank table's
  column widths (the award-group names are long) and the length of the About page's text need a look there.

## Changes in version 0.8 (2026-11-19)

- **Wording.** `games.csv`'s `production_label` says **No verified label** for a game without a verified AAA, AA
  or Indie label (it said "Unclassified"), and `ranking_tier_name` names the third tier **Indie or no verified
  label**. The values mean the same as before; no game moves.
- **Model.** `Games with a verified label` now leaves out both "No verified label" and "Unclassified", so it counts
  correctly with an export made before or after 0.8. The descriptions of `Production Label`, `Ranking Tier`,
  `Ranking Tier Name` and `Four-Year Rank`, and the About the data page's text, use the new wording.
- **Checked:** TOM (`TmdlSerializer`, Power BI Desktop 2.159) deserialized the edited model with 0 `Validate()`
  errors, and an export to a test folder wrote 179 games with "No verified label" and 59 with a label (238 games).
  **Not checked:** the project was not opened or refreshed in Power BI Desktop for 0.8. Do that once, as below.
- **Manual steps:** run `python -m game_librarian export-report`, open `GameLibrarian.pbip` in Power BI Desktop and
  select **Home → Refresh**. The Production label slicer then lists "No verified label". A copy published to the
  Power BI service changes only after you refresh or republish it there.
- **Gap: no Trending page.** The website's Dashboard → Trending games page analyzes the cross-platform Trending lists
  (IGDB, Twitch) and Steam snapshots. None of that is in this export, so the report has no such page. Adding one
  would need new export tables (IGDB lists, release dates, Steam snapshots), model tables and a page checked in
  Desktop. Twitch's top list cannot be exported at all: Twitch's terms allow keeping its data only 24 hours.

## Changes in version 0.4 (2026-11-15)

- New columns: `Games[Catalog Source]` (Hand-picked / Promoted candidate), `Games[Top Award Wins]`,
  `Awards[Award]` (organization · category) and `Awards[Is Top Award]`; new measure `Promoted games`. The Awards
  Explorer's category slicer, "entries by match" chart and entries table now use `Award`. Text boxes describe the
  new award coverage (see the main README).
- Checked offline: the TMDL folder deserializes with TOM (`TmdlSerializer`, from Power BI Desktop 2.159's `bin`
  folder), `Model.Validate()` reports no errors, no measure and column names clash ignoring case, all 39 visual
  field references exist in the model, all report JSON parses, and a test checks that the model reads exactly the
  exported columns of `games.csv`, `awards.csv` and `export_info.csv`.
- Checked in Power BI Desktop 2.159 on 2026-11-15, with an export of 238 games and 341 award rows:
  - The project opened without errors, and all five tables refreshed in 3–4 seconds.
  - 26 DAX results matched an independent Python calculation from the CSV files exactly, for example
    `Promoted games` 78, `Awarded catalog games` 161 and the "RPG or Indie" filter. So did the entry counts for all
    8 award names.
  - The screenshots showed two text problems, now fixed. The Awards Explorer subtitle was cut off, and the
    "Coverage limits" box still described one organization. The two top charts are now 320 px tall (the entries
    table is 40 px shorter), so "How award entries match Steam" shows all 8 award names without scrolling. The
    three longest names are shortened on the axis and appear in full in the slicer and the table.
- A copy that is already published does not change until you refresh or republish it there.

## Verification (2026-11-10)

- Power BI Desktop 2.159.1231.0 (Microsoft Store) opened `GameLibrarian.pbip` without errors and refreshed all five
  tables from the export in 3 seconds.
- 24 DAX results (counts, sums, both rates, the median, award coverage and filtered totals) matched an independent
  Python calculation from the CSV files exactly.
- All 44 JSON files validate against Microsoft's published schemas for their `$schema` versions, and all 63 field
  references in the visuals exist in the model.
- 2026-11-10, ranking update: Desktop opened and refreshed the updated project; 17 summary values and all 51 ranked
  rows (rank, app ID, tier, positive reviews) matched the website's Python ranking; the measure-bound subtitle and
  the Ranking window card rendered.
- Not verified: publishing, gateway refresh and embedding in the Power BI service (they need an account); the
  report in browsers other than Desktop.
- Known limit: the scatter's horizontal axis is linear, so games with under 1M reviews sit close together. A
  generated log-scale setting was not applied by Desktop. You can try the visual's Format pane.
