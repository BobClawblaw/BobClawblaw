# newspost.py Documentation

## Overview
`newspost.py` is the core automation script for gathering, deduplicating, summarizing, and posting Bitcoin-related news digests to the Bitcointalk "Wall Observer" thread. It supersedes all previous versions (v1-v25) and `news_pipeline.py`.

## Key Features
- **Sources:** Diversified news sources, excluding low-quality domains (e.g., Bloomberg).
- **Filtering:** Employs ratio-based link density filters and strict keyword filtering (`KEEP_KEYWORDS` vs `DISCARD_KEYWORDS`) to maintain topic relevance.
- **Deduplication:** Sophisticated multi-level deduplication (`are_similar` and `are_similar_cross`) ensures high signal-to-noise ratios, even across different news sources reporting on the same story.
- **Summary:** Uses `Jarcgon/Qwen3.6-35B-A3B-Claude-4.7-Opus-abliterated-uncenfull:latest` (Ollama) to generate exactly 5-sentence summaries in the BobClawblaw persona.
- **Market Data:** Integrated live BTC pricing and market cap data via CoinGecko.

## Logic Flow
1. **Discovery:** Queries SearXNG to fetch news headlines and URLs.
2. **Extraction:** Uses Firecrawl (locally hosted) to scrape substantive content from article URLs.
3. **Filtering:**
   - Drops non-BTC content (`NON_BTC_KEYWORDS`).
   - Discards noise (ethereum, altcoins, etc. in `DISCARD_KEYWORDS`).
4. **Deduplication:**
   - Compares article titles using Levenshtein ratio and custom heuristics (e.g., monitoring wallet movements or corporate Bitcoin strategy updates).
   - Groups similar articles cross-source.
5. **Summarization:**
   - Cleans article boilerplate.
   - Summarizes text using the persona-configured Ollama LLM.
   - Enforces the strict "exactly 5 sentences" rule, adding filler if necessary.
6. **Posting:** Uses a unified `posting_util.py` (via `post_with_login` logic) to login and post the formatted output to Bitcointalk in BBCode.

## Key Functions
- `are_similar`/`are_similar_cross`: Performs title-based story deduplication.
- `enforce_five_sentences`: Ensures all reports meet the BobClawblaw character voice and length constraints.
- `make_aware`/`_dt_parse`: Handles fuzzy date extraction across varied web source formats.
- `clean_text`: Strips HTML, boilerplate, and common ad-tracking patterns from scraped text.

## Configuration
- **Ollama Host:** `http://127.0.0.1:11434`
- **Ollama Model:** `Jarcgon/Qwen3.6-35B-A3B-Claude-4.7-Opus-abliterated-uncenfull:latest`
- **SearXNG Host:** `http://127.0.0.1:8080` (POST requests required)
- **Firecrawl Host:** `http://localhost:3002`

## Deployment & Maintenance
- **Repository:** Maintained in the `BobClawblaw` GitHub repository.
- **Requirements:** Run with `python3 newspost.py`. Ensure Firecrawl, Ollama, and SearXNG are running before execution.

### Output + posting flags
- `--out-dir <path>`: where to write digests (default: `/root/.hermes/saved_files`).
- `--post`: submit the most recently generated BBCode to Bitcointalk.
- `--with-footer` / `--footer`: include the “[i]Spotted by BobClawblaw …[/i]” footer.

### Posting implementation (single source of truth)
- Posting uses the repo script: `/root/BobClawblaw/post_wall_observer.py`.
- Hermes’ copy at `/root/.hermes/bobclawblaw/post_wall_observer.py` is a thin wrapper that forwards to the repo script.

### Symbol handling
- `post_wall_observer.py` maps `₿ -> B` before ASCII filtering, so the posted content stays readable on Bitcointalk.
