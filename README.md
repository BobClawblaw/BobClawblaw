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

Behavior when posting is enabled:
- It detects the current tail streak of consecutive `ChartBuddy` posts from the newest DB entries.
- Default rule: it never posts when `streak == 4`.
- For every consecutive post beyond that (i.e., when `streak > 4`), it attempts a post with 33% probability.
- If the 33% roll fails, it skips posting; if the ChartBuddy streak keeps growing, the next run will include one more `B` in the rainbow header because the header length tracks the streak length.

Index
- `python3 wall_observer_indexer.py`
- `python3 wall_observer_indexer.py --prune-missing --prune-anchors 10`

## Documentation
- More detail: `docs/newspost_documentation.md`

## Ollama + Hermes orchestration

This project is run by Hermes Agent as scheduled jobs (not a single monolithic script).

- Digest generation + optional posting
  - You run `python3 newspost.py` directly when you want to generate a digest.
  - The summarizer uses local Ollama at http://127.0.0.1:11434
  - Model: `Jarcgon/Qwen3.6-35B-A3B-Claude-4.7-Opus-abliterated-uncenfull:latest`

- DB-first indexing + buddychain detection (automated)
  - Hermes runs `/root/.hermes/scripts/wall_observer_indexer_cron.sh` every 30 minutes (`*/30 * * * *`).
  - That cron script runs, in order:
    1) `wall_observer_indexer.py --prune-missing --prune-anchors 10`
    2) `buddyblocker.py --streak 4` (detection-only by default)

Hermes job wiring (cron config) lives under `/root/.hermes/cron/jobs.json`.

## Hardware / system specs (this environment)

- Host / kernel: Linux gx10 6.17.0-1018-nvidia (Ubuntu 24.04.4 LTS)
- CPU: 20x ARM (aarch64) cores (Cortex-X925 / Cortex-A725), ~3.9GHz max
- RAM: 121GiB (108GiB used, ~1.9GiB free)
- Swap: 15GiB (8.5GiB used)
- Storage: root filesystem on /dev/nvme0n1p2, 1.8T total, 555G used
- GPU: NVIDIA GB10 (UUID GPU-a901e64d-4198-a5ea-5c00-f3bce5dd70e8)
- Python: Python 3.12.3
