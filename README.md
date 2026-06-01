# BobClawblaw Wall Observer Digest (local pipeline)

A local automation pipeline that compiles a small Bitcoin news digest and posts it to the Bitcointalk “Wall Observer” thread (topic 178336), plus a DB-only “buddychain” detector for ChartBuddy streaks.

## What runs where

1) Digest generation (optional posting)
- `newspost.py`
  - Pulls candidate headlines from local SearXNG (POST).
  - Scrapes article content via local Firecrawl.
  - Cleans + filters content, deduplicates stories, and generates BBCode summaries via local Ollama.
  - Writes output under `/root/.hermes/saved_files/digests/` by default.
  - Optionally posts using the repo’s posting script: `/root/BobClawblaw/post_wall_observer.py`.

2) Posting to Bitcointalk
- `post_wall_observer.py`
  - Uses cookie persistence + credentials from `/root/.hermes/bobclawblaw/profile/`.
  - Maps `₿` → `B` before ASCII filtering so posted content stays readable.

3) Local indexing (DB-first)
- `wall_observer_indexer.py`
  - Fetches the Wall Observer topic pages and stores parsed posts into a local SQLite DB: `/root/.hermes/bobclawblaw/wall_posts.db`.
  - Supports deletion reconciliation via `--prune-missing --prune-anchors <N>`.

4) Buddyblocker (detection-only by default)
- `buddyblocker.py`
  - Reads only the local SQLite DB (no forum scraping).
  - Detects the current tail streak of consecutive ChartBuddy posts.
  - Optional posting: `--post` (requires posting utilities + credentials).

5) Cron wiring
- `/root/.hermes/scripts/wall_observer_indexer_cron.sh`
  - Runs indexer first (with pruning), then buddyblocker.

## Quick commands

Digest
- `python3 newspost.py --post`
- `python3 newspost.py --out-dir /some/path`
- `python3 newspost.py --with-footer` (include the footer)

Buddyblocker
- `python3 buddyblocker.py` (detection-only)
- `python3 buddyblocker.py --post`

Index
- `python3 wall_observer_indexer.py`
- `python3 wall_observer_indexer.py --prune-missing --prune-anchors 10`

## Documentation
- More detail: `docs/newspost_documentation.md`
