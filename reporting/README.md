# Game Librarian Power BI report

A Power BI Project (`GameLibrarian.pbip`) that analyzes the curated catalog: player reception and award coverage.
It is an analytical companion to the website, not a second catalog.

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
   label; the four-year ranking table (the website's rules; its subtitle shows the export's window); games by
   positive-share band. Slicers: tag or genre, production label, award
   status.
2. **Awards Explorer.** Award entries, winners, nominees, catalog games with awards and the share of entries matched
   to the catalog; entries by year (winners and nominees); how entries match Steam by category; a table of every
   verified entry with what the award applies to. Slicers: year, category, result, match to Steam.
3. **About the data.** Definitions, the ranking rules, the ranking window, coverage limits, refresh steps and the
   loaded export.

The four-year ranking (`Four-Year Rank`, `Ranking Tier Name`, `In Four-Year Window`, `Rank Status`, `Rank Note`) is
calculated by `export-report` exactly as on the website, with the window ending on the export day. Refreshing Power
BI does not move the window: run `export-report` first.

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
tags or awards is counted once. Verified in Power BI Desktop: filtering on "RPG or Indie" returns 108 games and
21,018,364 reviews, not the 25,806,731 that adding the two tags' totals would give.

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
| `Games with a verified label` | production label other than Unclassified |
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
3. **Secure embed (recommended).** Open the report in the service and select **File → Embed report → Website or
   portal**. Copy the link (`https://app.powerbi.com/reportEmbed?...`) and add it to the project's `.env`:
   `GAME_LIBRARIAN_DASHBOARD_URL=<link>`. Restart the website. The Dashboard page then shows the report in a frame;
   each viewer signs in with their own account, and Power BI shows the report only to accounts that have access
   (workspace role, app or share).
4. **Publish to web (public, not recommended).** It creates a link that anyone can open without signing in, and the
   data becomes public. Your organization may disable it. The website shows a warning when the configured link is
   a Publish to web link (`https://app.powerbi.com/view?r=...`). Nothing in this project publishes anything.

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
