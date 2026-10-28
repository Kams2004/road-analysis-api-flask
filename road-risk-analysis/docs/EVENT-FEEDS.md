# Daily event files (SFTP "event data" source)

The fleet platform exports the previous day's driving exceptions once a day (around 03:00)
as one CSV file, `YYYY-MM-DD.csv`, on an SFTP server. The files stay on the server. VIASafe
imports them into `historical_events`, the event store read by **Historical analysis** and
**Driver behaviour**. This replaces the Ymane API sync: same event ids (`exceptionid`, source
`ymane`), so the two never duplicate each other.

## Set it up

*Cloud sources → Add a source → SFTP server*, then choose **This server provides: Event data (CSV)**.

| Field | Example |
|-------|---------|
| Host / port | `bi.camtrack.mg` / `22` |
| Username / password | the upload account (the password is stored encrypted, see SFTP-SOURCE.md §1) |
| Base path | the folder the files are dropped in, e.g. `/upload` |

The card shows an **Events** badge. Selecting it shows the daily files instead of a file
browser: what each import did, **Check now** and **Import this file again**. Event sources
never appear in the video import flows (`409 not_a_video_source`).

## What happens every hour

Beat task `sync_event_feeds` (`app/workers/event_feed_tasks.py`), one check per source at a
time (Redis lock), in `app/modules/m08_historical/event_feed.py`:

1. **One SFTP listing** of the base folder. Files named `YYYY-MM-DD.csv` (or `YYYY_MM_DD.csv`)
   are compared with `event_files`. A file is fetched when it is **new**, when its **size or
   modification time changed** (regenerated), or when its last import **failed** (5 attempts).
   At most 31 files per check, oldest day first: the first check catches up on the history a
   month at a time.
2. **Archive**: each fetched file is kept as received (gzip) in the private MinIO bucket
   `events-raw` (`<connection id>/YYYY/MM/<name>.gz`, ~150 KB per day). Everything can be
   re-imported from there, even if the SFTP server loses its files.
3. **Parse** (`sources/ymane_csv.py`), see below. A file with the same sha256 as its last
   import is not imported twice.
4. **Place on the map**: grid cell for every event; road name via OSRM for new events only when
   the point is within 200 m of a road OSRM knows. The server's OSRM covers Cameroon, so
   events in other countries get their grid cell but no road name.
5. **Store in bulk**: `INSERT … ON CONFLICT (source, source_event_id) DO UPDATE`. A corrected
   file updates its events and never creates duplicates.

## Columns

| CSV | Stored as |
|-----|-----------|
| `exceptionid` | `source_event_id` (source `ymane`) |
| `exception_type_name` | `event_type`: Speeding → `SPEEDING`, Harsh Acceleration → `HARD_ACCELERATION`, Harsh Braking → `HARD_BRAKING`, Harsh Cornering → `HARSH_CORNERING`, other → `OTHER` |
| `startdatetime`, `enddatetime` | `occurred_at`, `ended_at`: **UTC** |
| `startgps` | `longitude`, `latitude`: **longitude first**, `"9.12,4.01"` or `"(49.15,-18.43)"` |
| `vmax`, `threshold` | `speed_mps`, `speed_limit_mps` (km/h ÷ 3.6; the limit only for Speeding) |
| `level` | `severity` (1 low, 2 medium, 3 high) |
| `totalduration` (hours), `totaldistance` (km) | `duration_s`, `distance_km` |
| `vehicledesc` (else `unitid`), `driver_name` | vehicle, driver (`UNKNOWN DRIVER` → none) |
| `customer_affiliate_name`, `transporter_name` | `country` (upper case), `transporter` |
| every non-empty column | `raw` |

A line without `exceptionid`, with an invalid start time or with missing / invalid GPS is
skipped. The file's row in the console shows how many lines were skipped and why (first 20).

## API

`GET /cloud-connections/{id}/event-files`, `POST …/event-files/sync` (check now),
`POST …/event-files/{file_id}/reimport`, plus `purpose` and `event_feed` on the connection.
See `docs/openapi.json` (tag *Cloud connections*).

## Scale

About 1,800 events a day (~650,000 a year, ~250 MB a year in PostgreSQL). Indexes:
`(event_type, occurred_at)`, `(country, occurred_at)`, driver, vehicle, file. Next steps:

- **Step 2:** daily summary tables (day × driver × type, day × zone × type) read by the
  screens, cached results and a country filter. The Driver behaviour screens currently load
  raw events (up to 200,000) for each request.
- **Step 3:** missing-file alerts and a calendar of days received / missing.
- **Retention:** raw events 3 years, daily summaries without limit.
