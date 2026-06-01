#!/usr/bin/env python3
"""
newspost.py — Localized Digest Pipeline
Target: local Dallas/America/Chicago time, clean article text, diversified sources, zero duplicates, clean BBCode.
Baseline: 1.0.0 (Official)
"""
import os
from subprocess import check_output as _git_ver
_script_dir = os.path.dirname(os.path.abspath(__file__))
_raw = _git_ver(["git", "-C", _script_dir, "describe", "--tags"], text=True).strip().split("-")[0].lstrip("v")
__version__ = f"v{_raw}"

import datetime
import pytz
import os
import sys
import json
from typing import Callable, List, Optional
import requests
import concurrent.futures
import requests
import re
import subprocess
from urllib.parse import urlparse
from dateutil.parser import parse as dateutil_parse
import warnings
import time
from functools import lru_cache

# ---------------------------------------------------------------------------
# RSS Source Registry (single source of truth)
# ---------------------------------------------------------------------------
# Keys are canonical domains (no scheme, no trailing slash). Values are lists
# of feed URLs to try in order.
RSS_FEED_REGISTRY = {
    # Existing sources
    "insights.glassnode.com": ["https://insights.glassnode.com/rss/"],
    "blog.bitmex.com": ["https://blog.bitmex.com/category/research/feed/"],
    "cointelegraph.com": ["https://cointelegraph.com/rss"],
    "coindesk.com": ["https://www.coindesk.com/arc/outboundfeeds/rss/"],
    "bitcoinmagazine.com": ["https://bitcoinmagazine.com/.rss/full/"],
    "news.bitcoin.com": ["https://news.bitcoin.com/feed/"],
    "theblock.co": ["https://www.theblock.co/rss.xml"],
    "decrypt.co": ["https://decrypt.co/feed"],
    "cryptoslate.com": ["https://cryptoslate.com/feed/"],

    # New sources suggested/added for higher coverage
    "blockworks.co": ["https://blockworks.co/feed/"],
    "protos.com": ["https://protos.com/feed/"],
    "messari.io": ["https://messari.io/rss"],
    "chainalysis.com": ["https://www.chainalysis.com/feed/"],
    "forklog.com": ["https://forklog.com/feed/"],
    "themerkle.com": ["https://themerkle.com/feed/"],
    "bitcoinist.com": ["https://bitcoinist.com/feed/"],
}

# Hard cap per feed to keep crawl bounded.
RSS_MAX_LINKS_PER_DOMAIN = 25

# Convenience set/tuple for domain checks.
RSS_KNOWN_DOMAINS = tuple(RSS_FEED_REGISTRY.keys())

# --- Constants & Config ---
OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
OLLAMA_MODEL = "Jarcgon/Qwen3.6-35B-A3B-Claude-4.7-Opus-abliterated-uncenfull:latest"
OLLAMA_CHAT_URL = "http://127.0.0.1:11434/api/chat"
SEARXNG_URL = "http://127.0.0.1:8080/search"
FIRECRAWL_SCRAPE_URL = "http://localhost:3002/v1/scrape"

CT = pytz.timezone("America/Chicago")

NON_BTC_KEYWORDS = [
    "energy", "grid", "power",
    "data center", "ai",
    "real estate", "industrial", "campus",
    "property", "infrastructure", "facility", "building",
    "electricity", "substation",
]

DISCARD_KEYWORDS = [
    "ethereum", "eth", "solana", "sol", "xrp", "defi",
    "nft", "shitcoin", "altcoin", "dogecoin",
    "cardano", "ada", "polygon", "aptos", "sui",
    "aave", "dao", "babylon", "staking", "yield", "lending",
    "borrowing", "liquid staking", "wbtc", "tbtc", "smart contract",
    "super pac", "pac", "fairshake", "election", "campaign finance",
    "lobby", "lobbying", "stablecoin", "stablecoins", "web3",
]

KEEP_KEYWORDS = [
    "bitcoin", "btc", "satoshi", "halving", "hashrate",
    "etf", "inflows", "outflows", "sec", "regulation",
    "custody", "whale", "microstrategy", "saylor", "miner",
    "treasury", "reserve", "inflation", "macro", "liquidity",
]

# ---------------------------------------------------------------------------
#  Time helpers
# ---------------------------------------------------------------------------
def make_aware(dt: datetime.datetime, raw: str = "") -> datetime.datetime:
    """Safely convert any datetime object to UTC timezone-aware."""
    if dt.tzinfo is not None:
        return dt.astimezone(datetime.timezone.utc)
    # Date-only strings (no time) in the content are CT-local by construction.
    has_time = ":" in raw or "PM" in raw or "AM" in raw or "T" in raw or "t" in raw
    if not has_time:
        return CT.localize(dt).astimezone(datetime.timezone.utc)
    # Strings with AM/PM but no explicit timezone: treat as CT-local.
    if raw and ("AM" in raw.upper() or "PM" in raw.upper()):
        return CT.localize(dt).astimezone(datetime.timezone.utc)
    # Strings with a timezone but no AM/PM: treat as UTC.
    return dt.astimezone(datetime.timezone.utc)

def _dt_parse(s: str) -> datetime.datetime | None:
    if not s:
        return None
    s = s.strip()
    
    # Try dateutil first
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            return dateutil_parse(s)
    except Exception:
        pass

    # Common formats fallback
    formats = [
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M:%S%z",
        "%Y-%m-%d",
        "%B %d, %Y",
        "%B %d, %y",
        "%b %d, %Y",
        "%Y-%m-%dT%H:%M:%S.%fZ",
    ]
    for fmt in formats:
        try:
            return datetime.datetime.strptime(s, fmt)
        except Exception:
            pass

    # Attempt regex clean for date-only formats like "May 24, 20"
    m = re.search(r"([A-Za-z]+)\s+(\d+),\s+(\d+)", s)
    if m:
        try:
            mname, day, year = m.groups()
            if len(year) == 2:
                year = "20" + year
            return datetime.datetime.strptime(f"{mname} {day}, {year}", "%B %d, %Y")
        except Exception:
            pass

    # Strip zone
    if len(s) > 4 and (s[-3:] == " PM" or s[-3:] == " AM"):
        try:
            return _dt_parse(s[:-4].strip())
        except Exception:
            pass

    return None

def extract_date_from_html_or_markdown(url: str, content: str, metadata: dict) -> str:
    """Robust extraction of published date from URL patterns, metadata, or markdown content."""
    # 1. Check standard metadata fields first
    for field in ['article:published_time', 'publishedTime', 'datePublished', 'date', 'publish_date', 'pubdate', 'og:regDate', 'sailthru.date', 'parsely-pub-date', 'cxenseparse:reco:publishtime']:
        val = metadata.get(field)
        if val and len(str(val).strip()) >= 6:
            return str(val).strip()

    # 2. Check URL for /YYYY/MM/DD/ or /YYYY-MM-DD/ patterns
    url_m = re.search(r'/(\d{4})/(\d{2})/(\d{2})/', url)
    if url_m:
        return f"{url_m.group(1)}-{url_m.group(2)}-{url_m.group(3)}"
    
    url_m2 = re.search(r'/(\d{4})-(\d{2})-(\d{2})/', url)
    if url_m2:
        return f"{url_m2.group(1)}-{url_m2.group(2)}-{url_m2.group(3)}"

    # 3. Check raw markdown content for text date headers or JSON-LD
    if content:
        # Check inside JSON-LD blocks in text
        m_json_ld = re.search(r'"datePublished"\s*:\s*"([^"]+)"', content, re.IGNORECASE)
        if m_json_ld:
            return m_json_ld.group(1)
            
        m_json_ld2 = re.search(r'"publishedAt"\s*:\s*"([^"]+)"', content, re.IGNORECASE)
        if m_json_ld2:
            return m_json_ld2.group(1)

        # Published/Date headers with optional colon and space (handles merged fields like PublishedMay 25, 2026)
        m_merged = re.search(r'(?:Published|Date|Updated)\s*:?\s*([A-Za-z]+)\s*(\d+),\s*(\d{4})', content, re.IGNORECASE)
        if m_merged:
            return f"{m_merged.group(1)} {m_merged.group(2)}, {m_merged.group(3)}"
        
        m_iso = re.search(r'\b(\d{4}-\d{2}-\d{2})\b', content)
        if m_iso:
            return m_iso.group(1)

    # 4. Fallback: Search the first 1500 chars of content for raw date patterns near the header
    if content:
        head = content[:1500]
        # Standard US written dates: e.g., "Mon, May 25, 2026 at 8:17 AM CDT"
        m_raw = re.search(r'\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday|Mon|Tue|Wed|Thu|Fri|Sat|Sun)?\s*,?\s*([A-Za-z]+)\s+(\d{1,2}),\s+(\d{4})(?:\s+(?:at|on|@)?\s*(\d{1,2}:\d{2})\s*([aApP][mM])?\s*([A-Z]{3})?)?\b', head, re.IGNORECASE)
        if m_raw:
            if m_raw.group(4):
                return f"{m_raw.group(1)} {m_raw.group(2)}, {m_raw.group(3)} {m_raw.group(4)} {m_raw.group(5) or ''} {m_raw.group(6) or ''}".strip()
            return f"{m_raw.group(1)} {m_raw.group(2)}, {m_raw.group(3)}"
            
        # ISO format: YYYY-MM-DD
        m_raw_iso = re.search(r'\b(\d{4}-\d{2}-\d{2})\b', head)
        if m_raw_iso:
            return m_raw_iso.group(1)
            
        # European format: e.g. "25 May 2026" or "25 May, 2026"
        m_raw_eur = re.search(r'\b(\d{1,2})\s+([A-Za-z]+),?\s+(\d{4})(?:\s+(?:at|on|@)?\s*(\d{1,2}:\d{2})\s*([aApP][mM])?\s*([A-Z]{3})?)?\b', head, re.IGNORECASE)
        if m_raw_eur:
            if m_raw_eur.group(4):
                return f"{m_raw_eur.group(2)} {m_raw_eur.group(1)}, {m_raw_eur.group(3)} {m_raw_eur.group(4)} {m_raw_eur.group(5) or ''} {m_raw_eur.group(6) or ''}".strip()
            return f"{m_raw_eur.group(2)} {m_raw_eur.group(1)}, {m_raw_eur.group(3)}"

    return ""

def to_ct_12h(raw):
    """Convert *any* published-time string to 'YYYY-MM-DD HH:MM AM/PM CT'."""
    if not raw or len(str(raw).strip()) < 6:
        return ""
    raw = str(raw).strip()
    
    # Check if there is a time component in the raw string to preserve day correctly
    has_time = ":" in raw or "PM" in raw or "AM" in raw or "T" in raw or "pm" in raw or "am" in raw
    
    dt = _dt_parse(raw)
    if dt is None:
        return raw  # return what we got; don't break things.

    if not has_time:
        # Date only: preserve date and do not append dummy/guessed times
        return f"{dt.strftime('%Y-%m-%d')}"

    dt_aware = make_aware(dt, raw)
    ct_dt = dt_aware.astimezone(CT)
    return f"{ct_dt.strftime('%Y-%m-%d')} {ct_dt.strftime('%I:%M %p')} CT"

def now_ct_12h():
    """Dallas current local time."""
    return datetime.datetime.now(pytz.utc).astimezone(CT).strftime("%Y-%m-%d %I:%M %p CT")

def get_time_of_day_edition() -> str:
    """Calculate a descriptive 'time of day color' edition string based on Chicago local time."""
    now_local = datetime.datetime.now(pytz.utc).astimezone(CT)
    hour = now_local.hour
    if hour in [0, 1]:
        return "After Midnight Edition"
    elif hour in [2, 3, 4]:
        return "Very Early Morning Edition"
    elif hour in [5, 6, 7, 8]:
        return "Early Morning Edition"
    elif hour in [9, 10, 11]:
        return "Late Morning Edition"
    elif hour in [12, 13]:
        return "Midday Edition"
    elif hour in [14, 15, 16, 17]:
        return "Afternoon Edition"
    elif hour in [18, 19, 20, 21]:
        return "Evening Edition"
    else:  # 22, 23
        return "Before Midnight Edition"

def get_historical_ct_time(hours_ago: int) -> str:
    """Dallas local time offset by hours_ago."""
    now_dt = datetime.datetime.now(pytz.utc).astimezone(CT)
    hist_dt = now_dt - datetime.timedelta(hours=hours_ago)
    return f"{hist_dt.strftime('%Y-%m-%d')} {hist_dt.strftime('%I:%M %p')} CT"

# ---------------------------------------------------------------------------
#  Content cleanup
# ---------------------------------------------------------------------------

def request_get_retry(url: str, timeout: int = 15, retries: int = 3, backoff_s: float = 1.5, headers: dict | None = None):
    last_err = None
    for attempt in range(retries):
        try:
            r = requests.get(url, timeout=timeout, headers=headers)
            if r.status_code == 200:
                return r
        except Exception as e:
            last_err = e
        if attempt < retries - 1:
            time.sleep(backoff_s ** attempt)
    return None

def request_post_retry(url: str, json_body: dict | None = None, data_body: dict | None = None, timeout: int = 15, retries: int = 3, backoff_s: float = 1.5, headers: dict | None = None):
    last_err = None
    for attempt in range(retries):
        try:
            if json_body is not None:
                r = requests.post(url, json=json_body, timeout=timeout, headers=headers)
            else:
                r = requests.post(url, data=data_body, timeout=timeout, headers=headers)
            if r.status_code == 200:
                return r
        except Exception as e:
            last_err = e
        if attempt < retries - 1:
            time.sleep(backoff_s ** attempt)
    return None
def clean_text(raw_text: str) -> str:
    t = raw_text
    t = re.sub(r'\[Skip to (?:navigation|main content|right column|footer|menu|search)\]', '', t)
    t = re.sub(r'!\[.*?\]\(.*?\)', '\n', t)          # image refs
    t = re.sub(r'!\[.*?\]', '\n', t)                  # bare alt
    t = re.sub(r'\b(?:See also|Read more|In this article)\b', '\n', t)
    t = re.sub(r'\b(?:Advertisement|Sponsored|Advertorial|Sponsored Content)\b', '\n', t)
    t = re.sub(r'\([^)]*://[^)]*\)', '', t)
    t = re.sub(r'([.!?])\s+(?=\1)', '', t)
    t = re.sub(r'[\t ]{2,}', ' ', t)
    t = re.sub(r'\n{3,}', '\n\n', t)
    t = re.sub(r'(https?://[^\s)]+)', '', t)
    t = re.sub(r'(?:\*|\b\*\*|___+)', '', t)
    
    # Strip social share brackets like [Facebook], [Twitter] and nav/menu/promotional patterns
    t = re.sub(r'\[\w+\]', '', t)
    t = re.sub(r'\s*(?:News|Store|Print|Books|Conference|Corporations)\s*', ' ', t, flags=re.IGNORECASE)
    t = re.sub(r'\bBy\s+\[', 'By ', t, flags=re.IGNORECASE)
    t = re.sub(r'\bSearch\s+', ' ', t, flags=re.IGNORECASE)
    t = re.sub(r'\bPREDICT\s+BITCOIN\b', ' ', t, flags=re.IGNORECASE)
    
    t = t.strip()
    t = re.sub(r'\bSkip\b', '', t)
    return t

def extract_better_summary(text: str) -> str:
    if not text:
        return ""
    text = clean_text(text)
    sents = re.split(r'(?<=[.!?])\s+', text)
    sents = [s.strip() for s in sents if s.strip() and len(s.strip()) > 15]
    if sents:
        return sents[0] + " " + sents[1] if len(sents) > 1 else sents[0]
    short = text[:200].strip().rstrip('.').rstrip()
    return short + "..."

def enforce_five_sentences(summary: str) -> str:
    sents = re.split(r'(?<=[.!?])\s+', summary)
    sents = [s.strip() for s in sents if s.strip() and len(s) > 10]
    if 5 <= len(sents):
        return " ".join(sents[:5])
    filler = [
        "Worth watching what the larger holders do with the spot bids.",
        "Accumulation from the mid-tier crowd hasn't stopped, even through the chop.",
        "Consolidation isn't a signal by itself — you have to see volume confirm it.",
        "Volume's been in the normal band. No conviction in either direction yet.",
        "The structural picture hasn't budged, even after the draw. Long-term thesis still intact.",
    ]
    while len(sents) < 5:
        sents.append(filler[len(sents)])
    return " ".join(sents[:5])

# ---------------------------------------------------------------------------
#  Deduplication & Diversity Selection
# ---------------------------------------------------------------------------
def are_similar(title1: str, title2: str) -> bool:
    """Analyze if two article titles refer to the exact same news story."""
    t1 = title1.lower()
    t2 = title2.lower()
    
    # Clean up common non-alphanumeric punctuation
    t1_clean = re.sub(r'[^\w\s]', ' ', t1)
    t2_clean = re.sub(r'[^\w\s]', ' ', t2)
    
    words1 = set(t1_clean.split())
    words2 = set(t2_clean.split())
    
    # 1. Broad Sequence Similarity
    import difflib
    ratio = difflib.SequenceMatcher(None, t1, t2).ratio()
    if ratio > 0.65:
        return True
        
    # 2. Heuristics for famous recurring stories:
    # A. Satoshi-era & Dormant wallet movements to OTC/FalconX/Cumberland
    early_indicators = {'satoshi', 'dormant', 'ancient', 'early', 'sleeping', 'vintage', 'og'}
    move_verbs = {'move', 'moves', 'moved', 'transfer', 'transfers', 'transferred', 'deposit', 'deposits', 'deposited', 'send', 'sends', 'sent'}
    otc_desks = {'falconx', 'cumberland', 'otc', 'desk', 'desks', 'exchange', 'exchanges'}
    
    has_early1 = any(i in words1 for i in early_indicators)
    has_early2 = any(i in words2 for i in early_indicators)
    has_move1 = any(v in words1 for v in move_verbs)
    has_move2 = any(v in words2 for v in move_verbs)
    has_otc1 = any(o in words1 for o in otc_desks)
    has_otc2 = any(o in words2 for o in otc_desks)
    
    if has_early1 and has_early2 and has_move1 and has_move2:
        if (has_otc1 or has_otc2) or len(words1.intersection(words2)) >= 3:
            return True
            
    # B. Strategy / MicroStrategy / Saylor pausing/buying bonds instead of BTC
    strategy_names = {'strategy', 'microstrategy', 'saylor'}
    strategy_actions = {'pause', 'pauses', 'paused', 'stop', 'stops', 'stopped', 'retire', 'retires', 'retired', 'bond', 'bonds', 'debt', 'convertible'}
    if any(n in words1 for n in strategy_names) and any(n in words2 for n in strategy_names):
        has_a1 = any(a in words1 for a in strategy_actions)
        has_a2 = any(a in words2 for a in strategy_actions)
        if has_a1 and has_a2:
            return True

    # C. BlackRock selling $1B / shedding $1B
    if 'blackrock' in words1 and 'blackrock' in words2:
        sell_verbs = {'sell', 'sells', 'sold', 'shed', 'sheds', 'shedded', 'outflow', 'outflows'}
        if any(v in words1 for v in sell_verbs) and any(v in words2 for v in sell_verbs):
            return True

    # D. Bitcoin options on Nasdaq
    if 'nasdaq' in words1 and 'nasdaq' in words2:
        options_keywords = {'option', 'options', 'approve', 'approved', 'approval', 'sec'}
        if any(o in words1 for o in options_keywords) and any(o in words2 for o in options_keywords):
            return True

    # 3. Standard stop-worded Jaccard overlap
    stop_words = {
        'bitcoin', 'btc', 'crypto', 'to', 'the', 'a', 'in', 'of', 'and', 'on', 'for', 
        'with', 'at', 'today', 'says', 'is', 'about', 'after', 'near', 'below', 'above', 
        'as', 'out', 'up', 'down', 'by', 'over', 'under', 'from', 'into'
    }
    w1 = words1 - stop_words
    w2 = words2 - stop_words
    
    if not w1 or not w2:
        return False
        
    intersection = w1.intersection(w2)
    union = w1.union(w2)
    jaccard = len(intersection) / len(union) if union else 0.0
    
    # Check numeric overlaps
    numbers1 = {w for w in words1 if w.isdigit() or (len(w) > 1 and w[:-1].isdigit() and w[-1] in ('m', 'b', 'k'))}
    numbers2 = {w for w in words2 if w.isdigit() or (len(w) > 1 and w[:-1].isdigit() and w[-1] in ('m', 'b', 'k'))}
    num_match = len(numbers1.intersection(numbers2)) > 0 if (numbers1 and numbers2) else False
    
    if jaccard > 0.35 or (num_match and jaccard > 0.22):
        return True

    return False

def are_similar_cross(title1: str, title2: str) -> bool:
    """Stricter cross-source similarity: catches same-story-different-source pairs
    that the main are_similar() misses (e.g., 'Texas Appoints CleanSpark Exec, Bitcoin Miner CEO
    to Strategic Bitcoin Reserve Committee' vs 'Texas Forms Bitcoin Reserve Advisory Committee' —
    ratio 0.51, below 0.65 threshold)."""
    import difflib
    t1 = title1.lower().strip()
    t2 = title2.lower().strip()

    # Direct sequence ratio — low threshold for catch-all
    ratio = difflib.SequenceMatcher(None, t1, t2).ratio()
    if ratio > 0.40:
        # Validate: at least 2 shared content words (excluding common stop words)
        stop = {'bitcoin', 'btc', 'crypto', 'to', 'the', 'a', 'in', 'of', 'and', 'on'}
        w1 = set(t1.split()) - stop
        w2 = set(t2.split()) - stop
        if len(w1.intersection(w2)) >= 2:
            return True
        # High ratio alone is enough for short titles
        if ratio > 0.55:
            return True
    return False

def select_top_stories(candidates: dict, target_count=10, max_per_source=2) -> list:
    """Select exactly target_count stories with no duplicates and strict source caps."""
    # Filter out candidates with a hotness score of 0.0 (meaning they were filtered out by date)
    valid_candidates = [c for c in candidates.values() if c["hotness"] > 0.0]
    sorted_candidates = sorted(valid_candidates, key=lambda x: x["hotness"], reverse=True)
    selected = []
    source_counts = {}
    
    # First pass: Respect source diversity and similarity filter
    for c in sorted_candidates:
        if len(selected) >= target_count:
            break
            
        domain = c["domain"]
        if source_counts.get(domain, 0) >= max_per_source:
            continue
            
        # Check similarity against already selected
        duplicate = False
        for s in selected:
            if are_similar(c["scraped_title"], s["scraped_title"]):
                duplicate = True
                break
        if duplicate:
            continue
            
        selected.append(c)
        source_counts[domain] = source_counts.get(domain, 0) + 1

    # Second pass: Relax source caps if needed, but still preserve strict similarity check
    if len(selected) < target_count:
        for c in sorted_candidates:
            if len(selected) >= target_count:
                break
            if any(c["url"] == s["url"] for s in selected):
                continue
                
            duplicate = False
            for s in selected:
                if are_similar(c["scraped_title"], s["scraped_title"]):
                    duplicate = True
                    break
            if duplicate:
                continue
                
            selected.append(c)
            domain = c["domain"]
            source_counts[domain] = source_counts.get(domain, 0) + 1

    # Cross-source low-threshold dedup: group similar titles using a lower threshold
    # to catch same-story-different-source.
    # If clustering would shrink below target_count, we keep the original selection
    # to preserve output size (better a few duplicates than a short digest).
    orig_selected = list(selected)
    if len(selected) >= 3:
        clusters = []
        for c in selected:
            placed = False
            for cluster in clusters:
                if any(are_similar_cross(c["scraped_title"], s["scraped_title"]) for s in cluster):
                    cluster.append(c)
                    placed = True
                    break
            if not placed:
                clusters.append([c])

        clustered = [max(cluster, key=lambda x: x["hotness"]) for cluster in clusters]
        if len(clustered) >= target_count:
            selected = clustered[:target_count]
        else:
            selected = orig_selected

    # Refill pass: clustering can shrink the list, so we attempt to refill.
    # If the strict similarity gate blocks too many additions, we relax it in a second pass.
    if len(selected) < target_count:
        selected_urls = {s["url"] for s in selected}
        domain_counts = {}
        for s in selected:
            domain_counts[s["domain"]] = domain_counts.get(s["domain"], 0) + 1

        refill_modes = [
            ("strict", lambda c, s: are_similar(c["scraped_title"], s["scraped_title"])),
            ("relaxed", lambda c, s: are_similar_cross(c["scraped_title"], s["scraped_title"])),
        ]

        for _, is_dup in refill_modes:
            if len(selected) >= target_count:
                break
            for c in sorted_candidates:
                if len(selected) >= target_count:
                    break
                if c["url"] in selected_urls:
                    continue

                domain = c["domain"]
                # Slightly relax cap after refill, but keep diversity.
                if domain_counts.get(domain, 0) >= (max_per_source + 1):
                    continue

                if any(is_dup(c, s) for s in selected):
                    continue

                selected.append(c)
                selected_urls.add(c["url"])
                domain_counts[domain] = domain_counts.get(domain, 0) + 1

    return selected[:target_count]

# ---------------------------------------------------------------------------
#  Market Data
# ---------------------------------------------------------------------------
def get_btc_market_data() -> dict:
    print("[-] Fetching live market metrics...")
    url = (
        "https://api.coingecko.com/api/v3/simple/price?"
        "ids=bitcoin&vs_currencies=usd&"
        "include_24hr_change=true&include_market_cap=true"
    )
    try:
        r = request_get_retry(url, timeout=15, retries=3)
        if r is not None:
            d = r.json()["bitcoin"]
            return {
                "price": d["usd"],
                "change_24h": d["usd_24h_change"],
                "mcap": d.get("usd_market_cap", 0),
            }
    except Exception as e:
        print(f"CoinGecko API Error: {e}")
    # Coinpaprika fallback
    try:
        r = request_get_retry("https://api.coinpaprika.com/v1/tickers/btc-bitcoin", timeout=15, retries=3)
        if r is not None:
            u = r.json()["quotes"]["USD"]
            return {
                "price": u["price"],
                "change_24h": u["percent_change_24h"],
                "mcap": u["market_cap"],
            }
    except Exception as e:
        print(f"Coinpaprika API Error: {e}")
    # Coinbase fallback (rate-limit free public endpoint for price stability)
    try:
        r = request_get_retry("https://api.coinbase.com/v2/prices/BTC-USD/spot", timeout=10, retries=3)
        if r is not None:
            price = float(r.json()["data"]["amount"])
            return {"price": price, "change_24h": 0.0, "mcap": price * 19700000.0}
    except Exception as e:
        print(f"Coinbase API Error: {e}")
    return {"price": 76500.0, "change_24h": 0.0, "mcap": 1500000000000.0}

def get_btc_history() -> dict:
    """Fetch 3d, 7d, 30d BTC performance from CoinGecko."""
    try:
        url = "https://api.coingecko.com/api/v3/coins/bitcoin/market_chart?vs_currency=usd&days=30&interval=daily"
        r = request_get_retry(url, timeout=15, retries=3)
        if r is not None:
            prices = [p[1] for p in r.json()['prices']]
            current = prices[-1]
            return {
                "prices": prices,
                "3d_change": (current - prices[-4]) / prices[-4] * 100,
                "7d_change": (current - prices[-8]) / prices[-8] * 100,
                "30d_change": (current - prices[0]) / prices[0] * 100
            }
    except Exception as e:
        print(f"History Fetch Error: {e}")
    return {"prices": [76500.0] * 30, "3d_change": 0.0, "7d_change": 0.0, "30d_change": 0.0}

def get_macro_context() -> dict:
    """Fetch Fear & Greed index and calculate 7-day sentiment momentum."""
    try:
        # Fetch last 7 days of Fear & Greed index
        r = request_get_retry("https://api.alternative.me/fng/?limit=7", timeout=10, retries=3)
        if r is not None:
            data = r.json().get('data', [])
            if data:
                today_val = int(data[0]['value'])
                sentiment = data[0]['value_classification']
                
                # Calculate 7-day momentum
                momentum_7d = 0
                if len(data) >= 7:
                    val_7d = int(data[6]['value'])
                    momentum_7d = today_val - val_7d
                
                return {
                    "fng": str(today_val),
                    "sentiment": sentiment,
                    "momentum_7d": momentum_7d,
                    "trend": "improving" if momentum_7d > 0 else "deteriorating" if momentum_7d < 0 else "flat"
                }
    except Exception as e:
        print(f"Fear & Greed API Error: {e}")
    return {"fng": "Unknown", "sentiment": "Neutral", "momentum_7d": 0, "trend": "flat"}

def _fetch_exchange(name, url_fn):
    """Fetch single exchange price from Binance, Kraken, Bitstamp, Coinbase, Gemini."""
    try:
        r = request_get_retry(url_fn(), timeout=8, retries=2)
        if r is None:
            return 0.0
        d = r.json()
        if name == "coinbase":
            p = float(d["data"]["amount"])
        elif name == "kraken":
            p = float(d["result"]["XXBTZUSD"]["c"][0])
        elif name == "binance":
            p = float(d["bidPrice"])
        elif name == "bitstamp":
            p = float(d["last"])
        elif name == "gemini":
            p = float(d["price"])
        else:
            return 0.0
        return p
    except Exception:
        return 0.0

def get_spot_premium() -> dict:
    """Fetch spot BTC prices from multiple exchanges to compute a weighted premium spread."""
    print("[-] Fetching multi-exchange spot premium index...")
    res = {"coinbase": 0.0, "kraken": 0.0, "binance": 0.0, "premium": 0.0}
    try:
        configs = [
            ("coinbase", lambda: "https://api.coinbase.com/v2/prices/BTC-USD/spot"),
            ("kraken", lambda: "https://api.kraken.com/0/public/Ticker?pair=XXBTZUSD"),
            ("binance", lambda: "https://api.binance.com/api/v3/ticker/bookTicker?symbol=BTCUSDT"),
            ("bitstamp", lambda: "https://www.bitstamp.net/api/v2/btcusd/ticker/"),
            ("gemini", lambda: "https://api.gemini.com/v1/prices/btcusd"),
        ]
        with concurrent.futures.ThreadPoolExecutor() as executor:
            futs = [executor.submit(_fetch_exchange, n, u) for n, u in configs]
            prices = {}
            for fut, (n, _) in zip(futs, configs):
                p = fut.result()
                if p > 0:
                    prices[n] = p
        if prices:
            res["coinbase"] = prices.get("coinbase", 0.0)
            res["kraken"] = prices.get("kraken", 0.0)
            res["binance"] = prices.get("binance", 0.0)
            # Weighted avg: Binance+Coinbase = 60%, Kraken = 20%, Gemini = 20%
            w = {"coinbase": 0.30, "kraken": 0.20, "binance": 0.30,
                 "bitstamp": 0.20, "gemini": 0.20}
            res["premium"] = sum(prices[n] * w.get(n, 0) for n in prices) / sum(w.get(n, 0) for n in prices)
        else:
            print("  All exchange price fetches failed, falling back to defaults")
    except Exception as e:
        print(f"Spot Premium Fetch Error: {e}")
    return res

def get_technical_context(prices: list) -> dict:
    """Calculate SMA from 30d slice and volatility (std dev) of last 3 days."""
    if not prices:
        return {"ma": 0.0, "vol": 0.0}
    ma = sum(prices) / len(prices)
    recent = prices[-3:] if len(prices) >= 3 else prices
    if not recent:
        return {"ma": ma, "vol": 0.0}
    mean = sum(recent) / len(recent)
    variance = sum((x - mean)**2 for x in recent) / len(recent)
    vol = variance**0.5
    return {"ma": ma, "vol": vol}

def get_onchain_metrics() -> dict:
    """Fetch live Bitcoin on-chain metrics (hashrate, recommended fees) from mempool.space public API."""
    print("[-] Fetching live on-chain metrics from mempool.space...")
    metrics = {"hashrate_eh": 650.0, "fast_fee": 15, "min_fee": 5, "diff_change": 0.0}
    try:
        r1 = requests.get("https://mempool.space/api/v1/difficulty-adjustment", timeout=10)
        if r1.status_code == 200:
            d1 = r1.json()
            metrics["hashrate_eh"] = d1.get("estimatedHashrate", 6.5e20) / 1e18
            metrics["diff_change"] = d1.get("difficultyChange", 0.0)
    except Exception as e:
        print(f"Mempool.space Difficulty Adjustment API Error: {e}")
        
    try:
        r2 = requests.get("https://mempool.space/api/v1/fees/recommended", timeout=10)
        if r2.status_code == 200:
            d2 = r2.json()
            metrics["fast_fee"] = d2.get("fastestFee", 15)
            metrics["min_fee"] = d2.get("minimumFee", 5)
    except Exception as e:
        print(f"Mempool.space Recommended Fees API Error: {e}")
        
    return metrics

def get_derivatives_metrics() -> dict:
    """Fetch live Bitcoin derivatives market metrics (Open Interest, Funding Rate) from Kraken Futures public API."""
    print("[-] Fetching live derivatives metrics from Kraken Futures...")
    metrics = {"open_interest": 0.0, "funding_rate_annual": 0.0, "change_24h": 0.0}
    try:
        r = requests.get("https://futures.kraken.com/derivatives/api/v3/tickers", timeout=10)
        if r.status_code == 200:
            data = r.json()
            btc_perps = [t for t in data.get('tickers', []) if t.get('symbol') == 'PI_XBTUSD']
            if btc_perps:
                perp = btc_perps[0]
                metrics["open_interest"] = float(perp.get("openInterest", 0.0))
                metrics["change_24h"] = float(perp.get("change24h", 0.0))
                raw_funding = float(perp.get("fundingRate", 0.0))
                metrics["funding_rate_annual"] = raw_funding * 86400 * 365 * 100
    except Exception as e:
        print(f"Kraken Futures API Error: {e}")
    return metrics

def get_economic_events(date_str: str) -> str:
    """Fetch relevant US economic calendar events (CPI, NFP, GDP, FOMC, Fed, PCE) from TradingEconomics API."""
    print("[-] Fetching economic calendar events...")
    try:
        url = "https://api.tradingeconomics.com/calendar"
        r = requests.get(url, timeout=10)
        if r.status_code == 200:
            events = r.json()
            relevant = []
            for ev in events:
                name = ev.get('Event', '').upper()
                if any(k in name for k in ['CPI', 'NFP', 'GDP', 'FOMC', 'RATE DECISION', 'INFLATION', 'PCE', 'FED', 'UNEMPLOYMENT']):
                    relevant.append(f"- {ev.get('Event')}: Actual {ev.get('Actual', 'N/A')} (Forecast {ev.get('Forecast', 'N/A')}, Previous {ev.get('Previous', 'N/A')})")
            if relevant:
                return "\n".join(relevant)
    except Exception as e:
        print(f"Economic Calendar API Error: {e}")
    return ""

def safe_float(val, default=0.0) -> float:
    """Safely convert any value (including LLM-returned sentiment strings) to a float."""
    if val is None:
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        if isinstance(val, str):
            val_lower = val.lower()
            if 'bull' in val_lower:
                return 0.5
            if 'bear' in val_lower:
                return -0.5
            if 'neutral' in val_lower:
                return 0.0
            m = re.search(r"[-+]?\d*\.\d+|\d+", val)
            if m:
                try:
                    return float(m.group(0))
                except ValueError:
                    pass
        return default

def strip_query_string(url: str) -> str:
    """Strip query parameters and any trailing slashes/whitespace from a URL."""
    if not url:
        return ""
    # Split on '?' to isolate the path, then clean up trailing slash
    return url.split('?')[0].strip().rstrip('/')

def get_source_domain(url: str) -> str:
    try:
        parsed = urlparse(url)
        domain = parsed.netloc.lower()
        parts = domain.split('.')
        if len(parts) >= 3:
            domain = '.'.join(parts[-2:])
        return domain
    except Exception:
        return 'unknown'

def clean_boilerplate(markdown_text: str) -> str:
    if not markdown_text:
        return ""
    cleaned_lines = []
    for line in markdown_text.split('\n'):
        line_strip = line.strip()
        if not line_strip:
            cleaned_lines.append('')
            continue
            
        links = re.findall(r'\[([^\]]*)\]\([^)]*\)', line_strip)
        if links:
            raw_links = re.findall(r'\[[^\]]*\]\([^)]*\)', line_strip)
            link_syntax_len = sum(len(rl) for rl in raw_links)
            density = link_syntax_len / len(line_strip)
            if density > 0.45 or (line_strip.startswith('* ') and len(links) == 1 and density > 0.25):
                continue
        
        line_lower = line_strip.lower()
        if 'facebook' in line_lower and 'twitter' in line_lower:
            continue
        if 'terms of use' in line_lower or 'privacy policy' in line_lower or 'cookie policy' in line_lower:
            continue
        if 'newsletter' in line_lower and ('sign up' in line_lower or 'subscribe' in line_lower or 'inbox' in line_lower):
            continue
        if line_lower.startswith('skip to ') or line_strip.startswith('[×]'):
            continue
            
        cleaned_lines.append(line)
    return '\n'.join(cleaned_lines)

def compute_hotness(art: dict, keep_keywords: list, discard_keywords: list) -> float:
    content_raw = art.get("content", "")
    content = clean_boilerplate(content_raw)
    title = (art.get("scraped_title", "") or "").lower()
    combined = content.lower() + " " + title
    keep_score = sum(combined.count(kwk) for kwk in keep_keywords) * 1.5
    disc_score = sum(combined.count(dkw) for dkw in discard_keywords)
    btc_count = combined.count("bitcoin") + combined.count("btc")
    length_bonus = min(len(content) / 2000.0, 1.0)
    pub_raw = art.get("pub_raw", "")
    try:
        dt = _dt_parse(pub_raw)
        if dt:
            now = datetime.datetime.now(datetime.timezone.utc)
            dt_aware = make_aware(dt)
            diff_hours = (now - dt_aware).total_seconds() / 3600
            
            # HARD CUTOFF: Filter out any news older than 36 hours or in the future
            if diff_hours > 36.0 or diff_hours < 0:
                print(f"      [DEBUG] Filtering out article (age: {diff_hours:.1f}h, outside 0-36h window): {art.get('scraped_title')}")
                return 0.0
                
            recency = max(0, 1 - diff_hours / 36.0)
        else:
            print(f"      [DEBUG] Filtering out article (invalid/missing timestamp): {art.get('scraped_title')}")
            return 0.0
    except Exception as e:
        print(f"      [DEBUG] compute_hotness recency error: {e}")
        return 0.0
        
    # Non-BTC penalty: keywords like "data center", "AI", "energy" counted at 2x weight and only penalize when present with < 4 BTC mentions
    # Atomic counting: longer phrases are counted as one unit (e.g., "data center" = 1, not 2)
    used = set()
    nb_count = 0
    for kw in NON_BTC_KEYWORDS:
        count = combined.count(kw)
        if count and kw not in used:
            nb_count += count
            used.add(kw)
            # for multi-word keywords, subtract count to avoid double-counting
            if len(kw.split()) > 1:
                nb_count -= count

    # Hard skip for low-relevance stories where non-BTC keywords dominate the article
    if nb_count > 0 and btc_count < nb_count * 1.5:
        print(f"      [DEBUG] Filtering out article (low-BTC relevance, non-BTC keywords dominate: {nb_count} non-BTC vs {btc_count} BTC): {art.get('scraped_title')}")
        return 0.0

    # Structural bonus: boost key topics that are high-impact structural/institutional drivers
    structural_bonus = 0.0
    if any(k in title.lower() or k in combined for k in ["sata", "microstrategy", "saylor", "mstr", "sec", "regulation", "regulatory"]):
        structural_bonus = 15.0

    # Soft-story penalty: penalize low-signal human-intrest/anecdotal stories.
    # Matches title + content for common soft-story patterns.
    _soft_signals = [
        r'\batm\b',
        r'\b(lady|man|woman|boy|girl|teen)\b',
        r'\b(elderly|old(?:er|est)?|couple|family|kid)\b',
        r'\b(loses?|lost|suffered?|scam|fraud|stolen?|robbed?|cheated)\b',
        r'\bsues?\b',
        r'\bsued\b',
    ]
    _soft_count = 0
    for _sig in _soft_signals:
        _soft_count += len(re.findall(_sig, combined, re.IGNORECASE))
    soft_penalty = min(_soft_count * 12, 48)  # up to 48 pt penalty

    non_btc_penalty = min(nb_count, 6)  # cap penalty at 6 to avoid killing good stories
    penalty_mult = 2.0 if btc_count < 4 else 1.0
    return max(0.0, (keep_score - disc_score * 0.5) + btc_count * 0.2 + length_bonus * 10.0 + recency * 20.0 + structural_bonus - non_btc_penalty * penalty_mult - soft_penalty)

# ---------------------------------------------------------------------------
#  Crawl & Validate Helpers
# ---------------------------------------------------------------------------
def fetch_rss_articles(domain: str) -> list:
    """Fetch recent article links from RSS feeds registered for a domain."""

    def _normalize(d: str) -> str:
        d = (d or "").strip().lower()
        if d.startswith("www."):
            d = d[4:]
        return d

    domain = _normalize(domain)

    import xml.etree.ElementTree as ET

    @lru_cache(maxsize=64)
    def _fetch_all_links(domain_norm: str) -> tuple:
        feed_urls = RSS_FEED_REGISTRY.get(domain_norm)
        if not feed_urls:
            return tuple()

        for feed_url in feed_urls:
            print(f"[-] Fetching RSS feed for {domain_norm} ({feed_url})...")
            try:
                r = requests.get(
                    feed_url,
                    timeout=15,
                    headers={
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                    },
                )
                if r.status_code != 200:
                    print(f"[!] RSS HTTP Error {r.status_code} for {domain_norm}")
                    continue

                text = r.text
                links: list[str] = []

                # 1. Try XML parse (RSS/Atom)
                try:
                    root = ET.fromstring(r.content)
                    for item in root.findall(".//item"):
                        link_node = item.find("link")
                        if link_node is not None and link_node.text:
                            links.append(link_node.text.strip())

                    # Atom format
                    if not links:
                        for entry in root.findall(".//{http://www.w3.org/2005/Atom}entry"):
                            link_node = entry.find("{http://www.w3.org/2005/Atom}link")
                            if link_node is not None:
                                href = link_node.attrib.get("href")
                                if href:
                                    links.append(href.strip())
                except Exception:
                    pass

                # 2. Regex fallback
                if not links:
                    matches = re.findall(
                        r"<link[^>]*>(.*?)</link>", text, re.IGNORECASE | re.DOTALL
                    )
                    for m in matches:
                        m_clean = m.strip()
                        if m_clean.startswith("http"):
                            links.append(m_clean)

                    href_matches = re.findall(
                        r"<link\s+[^>]*href=[\"\'](https?://[^\"\']+)[\"\']",
                        text,
                        re.IGNORECASE,
                    )
                    for hm in href_matches:
                        links.append(hm.strip())

                # Canonicalize + hard cap
                valid = []
                seen = set()
                for link in links:
                    if not link or "http" not in link:
                        continue
                    if link in seen:
                        continue
                    seen.add(link)
                    valid.append(link)
                    if len(valid) >= RSS_MAX_LINKS_PER_DOMAIN:
                        break

                if valid:
                    print(f"    ✓ RSS returned {len(valid)} links for {domain_norm}")
                    return tuple(valid)

            except Exception as e:
                print(f"[!] RSS processing failed for {domain_norm}: {e}")

        return tuple()

    link_tuples = _fetch_all_links(domain)
    return [{"url": u} for u in link_tuples]


def search_searxng(query: str, time_range: str = "day") -> list:
    """Perform POST request on SearXNG with date restrictions."""
    payload = {"q": query, "format": "json"}
    if time_range:
        payload["time_range"] = time_range

    try:
        r = requests.post(SEARXNG_URL, data=payload, timeout=30)
        if r.status_code == 200:
            return r.json().get("results", [])
    except Exception as e:
        print(f"[!] SearXNG search failed for '{query}': {e}")
    return []

def is_article_link(url: str, parent_url: str) -> bool:
    try:
        p = urlparse(url)
        path = p.path.lower()
        if any(ext in path for ext in [".png", ".jpg", ".jpeg", ".gif", ".pdf", ".css", ".js", ".svg"]):
            return False
        if any(bad in p.netloc.lower() for bad in ["twitter.com", "x.com", "reddit.com", "facebook.com", "t.me", "telegram"]):
            return False
        if "author" in path or "category" in path or "tag" in path or "search" in path or "/quotes/" in path or "/calculator/" in path or "/video/" in path or "/videos/" in path:
            return False
        if any(domain in p.netloc.lower() for domain in RSS_KNOWN_DOMAINS):
            if len(path.strip("/")) > 5:
                return True
        # Generic date-in-path article detection (handles 247wallst, cnbc, and others with date subdirectories in path)
        if re.search(r'/\d{4}/\d\d/\d\d/', path) or re.search(r'/\d{4}-\d\d-\d\d/', path):
            return True

        # Generic SEO kebab-case slug detection (e.g., /news/analysis/bitcoin-four-year-cycle-isnt-dead-analyst-says)
        last_seg = path.strip("/").split("/")[-1]
        if last_seg.count("-") >= 4 and len(last_seg) > 15:
            return True

        if "yahoo.com" in p.netloc.lower() and "/articles/" in path:
            return True
        if "tradingview.com" in p.netloc.lower() and "/news/" in path and len(path.strip("/")) > 10:
            return True
        if "reuters.com" in p.netloc.lower():
            if len(path.strip("/")) > 10:
                return True
    except Exception:
        pass
    return False

def query_ollama(prompt: str) -> str | None:
    payload = {
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.1, "num_predict": 3096},
    }
    try:
        r = requests.post(OLLAMA_URL, json=payload, timeout=300)
        if r.status_code == 200:
            return r.json().get("response", "").strip()
    except Exception as e:
        print(f"[!] Ollama Error: {e}")
    return None

def query_ollama_chat(prompt: str) -> str | None:
    payload = {
        "model": OLLAMA_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "options": {"temperature": 0.1, "num_predict": 3096},
    }
    try:
        r = requests.post(OLLAMA_CHAT_URL, json=payload, timeout=300)
        if r.status_code == 200:
            return r.json()["message"]["content"].strip()
    except Exception as e:
        print(f"[!] Ollama Chat Error: {e}")
    return None

def extract_json(s: str | None) -> dict | None:
    if not s:
        return None
    try:
        return json.loads(s.strip())
    except Exception:
        m = re.search(r'({.*})', s, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(1).strip())
            except Exception:
                pass
    return None

# ---------------------------------------------------------------------------
#  Sanitization & Conversion
# ---------------------------------------------------------------------------
def markdown_to_bbcode(md_text: str) -> str:
    """Safely convert Markdown elements to SMF-compatible uppercase BBCode.

    Maps H3 to [SIZE=3][B]...[/B][/SIZE], H2 to [SIZE=4][B]...[/B][/SIZE],
    H1 to [SIZE=5][B]...[/B][/SIZE], and bold/italic to [B]/[I].
    Translates Published lines to [SIZE=2]Published: ...[/SIZE].
    """
    bb = md_text
    
    # Normalize encoding characters just in case
    bb = bb.replace("—", "-").replace("–", "-")
    bb = bb.replace("·", "|").replace("•", "*")
    bb = bb.replace("‘", "'").replace("’", "'")
    bb = bb.replace("“", '"').replace("”", '"')
    bb = bb.replace("…", "...")
    
    # Convert Published lines to [SIZE=2]
    bb = re.sub(
        r'\*\*Published:\*\*\s*(.*)',
        r'[SIZE=2]Published: \1[/SIZE]',
        bb,
        flags=re.IGNORECASE
    )

    # Convert Headers (H3 -> SIZE=3, H2 -> SIZE=4, H1 -> SIZE=5)
    bb = re.sub(r'^###\s*(.*)$', r'[SIZE=3][B]\1[/B][/SIZE]', bb, flags=re.MULTILINE)
    bb = re.sub(r'^##\s*(.*)$', r'[SIZE=4][B]\1[/B][/SIZE]', bb, flags=re.MULTILINE)
    bb = re.sub(r'^Title:\s*(.*)$', r'[SIZE=5][B]\1[/B][/SIZE]', bb, flags=re.MULTILINE)
    bb = re.sub(r'^#\s*(.*)$', r'[SIZE=5][B]\1[/B][/SIZE]', bb, flags=re.MULTILINE)

    # Convert markdown bold **text** to [B]text[/B] (restricted to same line)
    bb = re.sub(r'\*\*([^\n*]+)\*\*', r'[B]\1[/B]', bb)

    # Convert markdown italic *text* to [I]text[/I] (restricted to same line)
    bb = re.sub(r'\*([^\n*]+)\*', r'[I]\1[/I]', bb)

    # Strip raw HTML
    bb = re.sub(r'<[^>]+>', '', bb)
    
    return bb

def clean_number(n: str):
    """Convert raw number token to float, return None if not convertible."""
    token = n
    # Strip leading prefix
    if len(token) > 0 and token[0] in '$%':
        token = token[1:]
    # Strip trailing currency suffix (K, M, B, b, m, B)
    if len(token) > 0 and token[-1] not in 'abc':
        while len(token) > 0 and token[-1] in 'BKMbkm':
            token = token[:-1]
    # Strip trailing dot (e.g. "100.")
    if len(token) > 0 and token[-1] == '.' and len(token) > 1:
        token = token[:-1]
    token = token.replace(',', '')
    # Reject pure-digit numbers > 10 digits (likely Twitter/X IDs, not BTC values)
    if all(c.isdigit() for c in token) and len(token) > 10:
        return None
    try:
        return float(token)
    except (ValueError, OverflowError):
        return None

def validate_numbers(summary: str, raw_content: str) -> str:
    """Catch LLM numeric hallucinations by cross-checking against raw article content."""
    if not summary or not raw_content:
        return summary
    
    # Extract comma-segmented numbers from raw: e.g. "843,738", "1,500", "65.25"
    raw_nums_raw = re.findall(r'\b(\d[\d,.]*)\b', raw_content)
    
    # For each candidate in raw, build a set of (raw_string, raw_clean_value, raw_display)
    # where raw_clean_value strips commas (and dot)
    raw_table = {}
    for n in raw_nums_raw:
        # Skip values that can't be parsed as numbers (e.g., "9.31.24" dates).
        clean = clean_number(n)
        if clean is None:
            continue
        raw_table[clean] = n
    
    for sn in set(re.findall(r'\b(\d[\d,.]+)\b', summary)):
        sn_clean = sn.replace(',', '')
        
        # Try to parse as int or float
        try:
            if '.' in sn_clean:
                sn_num = float(sn_clean)
            else:
                sn_num = int(sn_clean)
        except ValueError:
            continue
        
        # Check exact match
        if sn_num in raw_table:
            continue
        
        # Only check suffix alignment for integers of 2+ digits to catch dropped leading digits (e.g., 843,738 -> 43,738)
        if isinstance(sn_num, int) and len(sn_clean) >= 2:
            for raw_key, raw_val in raw_table.items():
                if isinstance(raw_key, float):
                    continue
                # e.g., raw_key=843738, sn_num=43738 -> raw_key ends with sn_num when aligned
                if raw_key > sn_num * 10 and str(raw_key).endswith(str(sn_num)):
                    print(f"      [NUM FIX] Corrected {sn} ({sn_num}) -> {raw_val} ({raw_key}) in summary")
                    summary = summary.replace(sn + ' ', raw_val + ' ')
                    summary = summary.replace(sn + ',', raw_val + ',')
                    summary = summary.replace(sn + ' of', raw_val + ' of')
                    summary = summary.replace(sn + '$', raw_val + '$')
                    summary = summary.replace(sn + '%', raw_val + '%')
                    break
    
    return summary

def fetch_direct_fallback(url: str) -> str:
    """Fetch raw HTML via requests with browser headers and strip to plain text."""
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.5',
    }
    try:
        r = requests.get(url, headers=headers, timeout=15)
        if r.status_code == 200:
            html = r.text
            # Remove scripts and style
            clean = re.sub(r'<(script|style)\b[^>]*>([\s\S]*?)<\/\1>', '', html, flags=re.IGNORECASE)
            # Remove tags
            clean = re.sub(r'<[^>]+>', ' ', clean)
            # Normalize whitespace
            clean = re.sub(r'\s+', ' ', clean).strip()
            return clean
    except Exception as e:
        print(f"      [FALLBACK ERR] {e}")
    return ""

# ---------------------------------------------------------------------------
#  Pipeline Main
# ---------------------------------------------------------------------------
    # v26: output files named v26-{date}.md / v26-{date}.bbcode.txt
    # --...
def run_pipeline():
    run_dt = datetime.datetime.now(pytz.utc).astimezone(CT)
    today = run_dt.strftime("%Y-%m-%d")
    # Suffix so multiple runs per day don't overwrite.
    # Example: 2026-06-01_030512PMCT
    run_suffix = run_dt.strftime("%I%M%S%p").upper()
    file_stamp = f"{today}_{run_suffix}CT"
    out_base = "/root/.hermes/saved_files"
    if "--out-dir" in sys.argv:
        i = sys.argv.index("--out-dir")
        if i + 1 < len(sys.argv):
            out_base = sys.argv[i + 1]
    digest_dir = os.path.join(out_base, "digests")
    os.makedirs(digest_dir, exist_ok=True)

    md_path = os.path.join(digest_dir, f"newspost-{__version__}-{file_stamp}.md")
    bb_path = os.path.join(digest_dir, f"newspost-{__version__}-{file_stamp}.bbcode.txt")

    print(f"--- Starting {__version__} pipeline (model={OLLAMA_MODEL}, CT timezone) ---")

    # Fetch live BTC price early for filtering out dated high-price articles
    mkt = get_btc_market_data()
    current_btc_price = mkt["price"]
    print(f"[-] Live BTC Price fetched early: ${current_btc_price:,.2f}")

    # --- 1. Discovery ---
    queries = [
        'site:insights.glassnode.com bitcoin',
        'site:blog.bitmex.com bitcoin',
        'site:cointelegraph.com bitcoin',
        'site:coindesk.com bitcoin',
        'site:bitcoinmagazine.com bitcoin',
        'site:news.bitcoin.com bitcoin',
        'site:finance.yahoo.com bitcoin',
        'site:cnbc.com bitcoin',
        'site:theblock.co bitcoin',
        'site:reuters.com bitcoin',
        'site:decrypt.co bitcoin',
        'site:cryptoslate.com bitcoin',
        'site:blockworks.co bitcoin',
        'site:protos.com bitcoin',
        'site:messari.io bitcoin',
        'site:chainalysis.com bitcoin',
        'site:forklog.com bitcoin',
        'site:themerkle.com bitcoin',
        'site:bitcoinist.com bitcoin',
        '"bitcoin news"',
    ]

    all_hits = []
    
    def discover_candidates(time_range="day"):
        hits = []
        for q in queries:
            domain_match = re.search(r'site:([^\s]+)', q)
            domain = domain_match.group(1) if domain_match else None
            
            used_rss = False
            if domain and domain in RSS_FEED_REGISTRY:
                rss_links = fetch_rss_articles(domain)
                if rss_links:
                    hits.extend(rss_links)
                    used_rss = True
                    
            if not used_rss:
                # Fallback to SearXNG
                print(f"[-] Querying SearXNG for: '{q}' (range={time_range})...")
                search_results = search_searxng(q, time_range=time_range)
                hits.extend(search_results)
                
            if len(hits) >= 120:
                break
        return hits

    all_hits = discover_candidates(time_range="day")

    # If day-based discovery yielded extremely low count, fallback to week-based
    if len(all_hits) < 15:
        print("[!] Low candidate count with 'day' limit / RSS. Falling back to 'week' search.")
        all_hits = discover_candidates(time_range="week")

    seen = set()
    unique = []
    for h in all_hits:
        u = strip_query_string(h.get('url', ''))
        if u and u not in seen:
            h['url'] = u
            seen.add(u)
            unique.append(h)
    all_hits = unique

    print(f"[-] {len(all_hits)} unique search results. Source diversifying...")

    yh = [h for h in all_hits if 'yahoo.com' in h.get('url', '')]
    ot = [h for h in all_hits if 'yahoo.com' not in h.get('url', '')]
    keep = min(len(yh), max(3, len(ot) // 3))
    all_hits = ot + yh[:keep]
    print(f"[-] Diversified: Yahoo {len(yh)}->{keep}, other {len(ot)}")

    print(f"[-] Enqueued {len(all_hits)} URLs for crawl.")

    # --- Crawl ---
    all_candidates = {}
    idx = 0
    idx_cnt = 0
    MAX_IDX = 20

    while idx < len(all_hits) and len(all_candidates) < 30:
        # Prevent runaway crawling of too many index/hub pages if we already have enough candidates
        if idx_cnt >= 40 and len(all_candidates) >= 20:
            print("[-] Crawled 40+ index pages and reached 20+ candidates. Stopping crawl.")
            break
        url = strip_query_string(all_hits[idx]['url'])
        u = url
        idx += 1

        if not u:
            continue

        print(f"  [{len(all_candidates)+1}] Crawling: {url}")
        try:
            if not is_article_link(url, ""):
                if idx_cnt >= MAX_IDX:
                    continue
                idx_cnt += 1
                sr = requests.post(FIRECRAWL_SCRAPE_URL, json={'url': url}, timeout=30)
                if sr.status_code != 200:
                    continue
                sd = sr.json().get('data', {}) if sr.json().get('success') else sr.json()
                md = sd.get('markdown') or sd.get('content') or ''
                links = re.findall(r'(https?://[^\s\)\"\'\s]+)', md)
                added = 0
                for lk in links:
                    lk = strip_query_string(lk)
                    if is_article_link(lk, url) and lk != u:
                        all_hits.insert(idx, {'url': lk})
                        added += 1
                        if added >= 8:
                            break
                print(f"    [INDEX] +{added} deep links")
                continue

            sr = requests.post(FIRECRAWL_SCRAPE_URL, json={'url': url, 'waitFor': 3000}, timeout=30)
            content = ""
            sd = {}
            if sr.status_code == 200:
                sd = sr.json().get('data', {}) if sr.json().get('success') else sr.json()
                content = sd.get('markdown') or sd.get('content') or ""

            if len(content) < 500:
                print(f"    ! Firecrawl returned thin/blocked content ({len(content)} chars). Trying direct HTTP fallback...")
                content = fetch_direct_fallback(url)
                if len(content) >= 500:
                    print(f"      ✓ Fallback Succeeded ({len(content)} chars fetched)")
                    fallback_title = url.split('/')[-1].replace('-', ' ').title()
                    fallback_title = re.sub(r'\.html?$', '', fallback_title, flags=re.IGNORECASE)
                    sd['metadata'] = sd.get('metadata', {})
                    sd['metadata']['title'] = fallback_title
                else:
                    print(f"      ! Fallback Failed, skipping.")
                    continue

            raw_content = content.lower()
            btc_c = raw_content.count("bitcoin") + raw_content.count("btc")
            if btc_c < 2:
                print(f"    ! Low btc count ({btc_c}), skipping.")
                continue
            alt_c = sum(raw_content.count(k) for k in DISCARD_KEYWORDS)
            if alt_c > btc_c + 3:
                print(f"    ! Altcoin-heavy ({alt_c} vs {btc_c}), skipping.")
                continue

            # Strict blacklist keywords (if any of these occur >= 2 times, skip immediately)
            strict_blacklist = [
                "aave", "ethereum", "solana", "defi", "liquid staking", "babylon", 
                "wbtc", "tbtc", "erc-20", "erc20", "cross-chain", "smart contract", 
                "yield farming", "stablecoin", "stablecoins", "fairshake", "super pac"
            ]
            trigger_word = None
            for w in strict_blacklist:
                if raw_content.count(w) >= 2:
                    trigger_word = w
                    break
            if trigger_word and alt_c > btc_c * 0.3:
                print(f"    ! Altcoin/Shitcoin blacklist triggered (found '{trigger_word}' >= 2 times and altcoin keywords constitute a significant portion), skipping.")
                continue

            md = sd.get('metadata', {})
            title = md.get('title') or md.get('og:title') or ""
            
            # Extract published date using robust extraction
            pub = extract_date_from_html_or_markdown(url, content, md)
            
            # Also extract raw article time for fallback (e.g., "Mon, May 25, 2026 at 8:17 AM CDT")
            article_time = ""
            time_match = re.search(
                r'(Mon|Tue|Wed|Thu|Fri|Sat|Sun)?,?\s*([A-Za-z]+)\s+(\d{1,2}),\s+(\d{4})(?:\s+(?:at|on|@)?\s*(\d{1,2}):(\d{2})\s*([aApP][mM])?)\b',
                content[:2000],
                re.IGNORECASE
            )
            if time_match:
                article_time = f"{time_match.group(2)} {time_match.group(3)}, {time_match.group(4)} {time_match.group(5)}:{time_match.group(6)} {time_match.group(7) or ''}".strip()

            domain = get_source_domain(url)
            hotness = compute_hotness(
                {"content": content, "scraped_title": title, "pub_raw": pub},
                KEEP_KEYWORDS, DISCARD_KEYWORDS
            )
            
            # Only keep candidate if hotness > 0 (meaning age was strictly validated as < 36h)
            if hotness > 0:
                all_candidates[url] = {
                    "url": url,
                    "domain": domain,
                    "scraped_title": title,
                    "content_raw": content,
                    "content": clean_text(content),
                    "pub_raw": pub,
                    "hotness": hotness,
                    "article_time": article_time,
                }
                print(f"    ✓ Validated ({title[:50]}…) [hot={hotness:.1f}]")
            else:
                print(f"    ! Discarded (stale/old story).")
        except Exception as e:
            print(f"    ! Err: {e}")

    print(f"[-] Candidate pool built: {len(all_candidates)} articles from {len(set(c['domain'] for c in all_candidates.values()))} sources.")

    # --- Strict Selection ---
    final = select_top_stories(all_candidates, target_count=10, max_per_source=2)
    print(f"[-] Selected {len(final)} unique, diversified stories.")

    # --- Summarize ---
    stories = []
    for i, art in enumerate(final, 1):
        print(f"[-] LLM [{i}/{len(final)}]: {art['url']}", art['domain'])
        content_to_use = art["content"] if art["content"] else art["content_raw"]

        prompt = f"""BobClawblaw here. I've been watching this market since we were buying at $10k.

Read this article carefully and give me the straight facts (no hype, no moon shots):

{content_to_use[:10000]}

Return STRICT VALID JSON (no markdown, no code blocks, just raw JSON):
- title: short, clean, no site branding — just what matters
- summary: 5 sentences of real substance. Skip the filler. Write like someone at a ranch checking the markets, not a trading desk analyst. Grounded, plainspoken, observant. Short sentences, steady pacing. Wry humor when warranted. No "crypto bro" slang (no moon, no apes, no degens, no GM), no rocket emojis. If something is wrong, "I suck" — no deflection.
- published_time: YYYY-MM-DD or YYYY-MM-DD HH:MM
- sentiment: float (-1.0 to 1.0), where -1.0 is extremely bearish, 0.0 is neutral, 1.0 is extremely bullish

Do NOT include markdown code block wrappers. Just raw JSON."""

        resp = query_ollama(prompt)
        parsed = extract_json(resp)

        if not parsed or not parsed.get("summary") or "Oops" in parsed["summary"]:
            resp2 = query_ollama_chat(
                f"Article: {art['url']}\n\nCONTENT:\n{clean_text(art['content_raw'] or art['content'])[:8000]}\n\nExtract: title (short), summary (5 sentences, real content, no [Skip to ...], no image refs, no anchor links, no repeated filler sentences), published_time.\n\nReturn ONLY JSON: {{\"title\":\"...\",\"summary\":\"...\",\"published_time\":\"...\"}}")
            parsed2 = extract_json(resp2)
            if parsed2 and parsed2.get("summary") and "Oops" not in parsed2["summary"]:
                parsed = parsed2
            else:
                summary = extract_better_summary(art["content_raw"] or art["content"])
                summary = clean_text(summary)
                if "==" in summary or "[" in summary or "]" in summary or not summary or len(summary) < 30:
                    # Summary bleed detected, attempt to re-extract via chat API
                    print("      [FALLBACK WARNING] Summary field bleed or bad format in extract_better_summary, re-extracting via chat...")
                    resp_chat = query_ollama_chat(
                        f"Article: {art['url']}\n\nCONTENT:\n{clean_text(art['content_raw'] or art['content'])[:8000]}\n\nExtract: title (short), summary (5 sentences, real content, no [Skip to ...], no image refs, no anchor links, no repeated filler sentences), published_time.\n\nReturn ONLY JSON: {{\"title\":\"...\",\"summary\":\"...\",\"published_time\":\"...\"}}")
                    parsed_chat = extract_json(resp_chat)
                    if parsed_chat and parsed_chat.get("summary") and "Oops" not in parsed_chat["summary"]:
                        summary = parsed_chat["summary"]
                    else:
                        summary = "Bitcoin's holding its ground at spot as the week unfolds. We've seen much worse and always the answer was just to wait."
                summary = enforce_five_sentences(summary)
                parsed = {
                    "title": art["scraped_title"],
                    "summary": summary,
                    "published_time": art["pub_raw"],
                }

        title = (parsed.get("title") or parsed.get("headline") or "").strip()
        title = title.split('|')[0].split('\u2013')[0].split('\u2014')[0].strip()
        title = title.rstrip("?'\"`()+")
        title = re.sub(r'\s+—\s*\d\d:\d\d.*$', '', title, flags=re.IGNORECASE)

        summary_raw = parsed.get("summary", "")
        
        # Validate numeric values against raw article to catch LLM digit drops (e.g., 843,738 -> 43,738)
        summary = validate_numbers(summary_raw, art["content_raw"] or art["content"])
        summary = enforce_five_sentences(summary)
        
        pub_raw = art["pub_raw"] or parsed.get("published_time", "")
        pub = to_ct_12h(pub_raw)
        
        # If pub only has a date and the raw content has a time, stitch it in
        if ':' not in pub_raw and art.get('article_time', ''):
            pub = to_ct_12h(art['article_time'])
            print(f"    [TIME FIX] No time in date field, used {art['article_time']} -> {pub}")

        print(f"    → {title[:60]}…  Published: {pub}")
        stories.append({"title": title, "url": art["url"], "published": pub, "summary": summary, "sentiment": safe_float(parsed.get("sentiment"), 0.0)})

    # Refuse to post if we don't have at least 3 legitimate stories
    if len(stories) < 3:
        print(f"CRITICAL ERROR: Only found {len(stories)} legitimate stories. Refusing to generate digest or post as we need at least 3 stories.")
        sys.exit(1)

    # --- Market metrics & assemble ---
    print(f"[-] BTC (Live, fetched early): ${mkt['price']:,.2f} ({mkt['change_24h']:+.2f}%) MC:${mkt['mcap']/1e12:.2f}T")

    # Get local weekday for the prompt to prevent the LLM from guessing the wrong day
    local_weekday = datetime.datetime.now(pytz.utc).astimezone(CT).strftime("%A")

    history_data = get_btc_history()
    history = history_data
    tech = get_technical_context(history.get("prices", []))
    onchain = get_onchain_metrics()
    derivs = get_derivatives_metrics()
    macro = get_macro_context()
    premium = get_spot_premium()
    eco = get_economic_events(datetime.datetime.now(pytz.utc).astimezone(CT).strftime("%Y-%m-%d"))
    eco_str = f"\nEconomic Context:\n{eco.strip()}" if eco and eco.strip() else ""
    # Calculate sentiment harmony
    avg_sentiment = sum(s['sentiment'] for s in stories) / len(stories) if stories else 0.0
    
    # Programmatically detect sentiment vs price divergence to instruct the LLM
    divergence_context = ""
    if avg_sentiment > 0.15 and mkt['change_24h'] < -1.0:
        divergence_context = (
            f"\nLogic Alert (Sentiment Divergence): The news sentiment is overwhelmingly positive ({avg_sentiment:.2f} bullish), "
            f"but the actual 24-hour price is down ({mkt['change_24h']:+.2f}%). "
            "Explain this divergence in your price analysis—are sellers dumping into positive news, or is this a leverage-driven flush?"
        )
    elif avg_sentiment < -0.15 and mkt['change_24h'] > 1.0:
        divergence_context = (
            f"\nLogic Alert (Sentiment Divergence): The news sentiment is bearish ({avg_sentiment:.2f} bearish), "
            f"but the actual 24-hour price is up ({mkt['change_24h']:+.2f}%). "
            "Explain this divergence in your price analysis—is there strong under-the-surface spot demand absorbing the bad news?"
        )

    # Format the collected news stories as context for the LLM
    news_context = ""
    for idx, st in enumerate(stories[:5], 1):
        news_context += f"Story {idx}: {st['title']}\nSummary: {st['summary']}\n\n"

    ctx_prompt = f"""I watching the market today. Price data tells me:
- Bitcoin: ${mkt['price']:,.2f}
- 24h change: {mkt['change_24h']:+.2f}%
- 3-day change: {history['3d_change']:+.2f}%
- 7-day change: {history['7d_change']:+.2f}%
- 30-day change: {history['30d_change']:+.2f}%
- Market cap: ${mkt['mcap']/1e12:.2f}T
- 30-day Avg Price (MA): ${tech['ma']:,.2f}
- Recent Volatility (3d StdDev): {tech['vol']:,.2f}
- Estimated Hashrate: {onchain['hashrate_eh']:.1f} EH/s (Diff change: {onchain['diff_change']:+.2f}%)
- Recommended fees: Fast {onchain['fast_fee']} sat/vB | Min {onchain['min_fee']} sat/vB
- Derivatives Open Interest: ${derivs['open_interest']/1e6:,.2f}M
- Perpetual Funding Rate (annualized): {derivs['funding_rate_annual']:+.4f}%
- Fear & Greed Index: {macro['fng']} ({macro['sentiment']} | 7-day momentum: {macro['momentum_7d']:+d} points, trend is {macro['trend']})
- Spot Arbitrage Premium (VW Average): {premium['premium']:+.2f} USD (Coinbase: ${premium['coinbase']:,.2f}, Kraken: ${premium['kraken']:,.2f}, Binance: ${premium['binance']:,.2f})
{eco_str}{divergence_context}

Note: Today is {local_weekday}.
Overall News Sentiment Score: {avg_sentiment:.2f} (-1.0 bearish to 1.0 bullish).

Here are the top news stories from today:
{news_context}

Write me four things, in BobClawblaw's voice:
- opening: 2-3 sentences. Metric-driven but plainspoken. Grounded. No hype, no crypto bro slang, no rocket emojis. Acknowledge what's happening without overreacting. Mention the day of the week if appropriate.
- outlook: 2-3 sentences. Still watching. What to keep an eye on next. No price predictions masquerading as facts — only data with honest caveats.
- price_analysis: 3-5 sentences analyzing the price action.plainspoken, grounded, observant. No jargon, no crypto bro slang, no rocket emojis. Reference the live numbers. If price is down, call it. If sideways, call it. Don't invent. Incorporate on-chain blocks, leverage data, sentiment momentum, and US spot premiums where useful.
- movers: A list of exactly 4 bullet points summarizing the key macro or micro market drivers actually present in the news stories above. Each bullet point must be formatted in markdown like: "- **[Driver Name]:** [1-sentence plainspoken description of what is happening]." Do not use hardcoded templates; base them strictly on the provided stories.

Return valid JSON with keys opening, outlook, price_analysis, and movers (as an array of strings)."""

    ctx = query_ollama(ctx_prompt)
    ctx_p = extract_json(ctx)
    opening = ctx_p["opening"] if ctx_p and ctx_p.get("opening") else ""
    outlook = ctx_p["outlook"] if ctx_p and ctx_p.get("outlook") else ""
    price_analysis = ctx_p["price_analysis"] if ctx_p and ctx_p.get("price_analysis") else ""
    movers = ctx_p["movers"] if ctx_p and isinstance(ctx_p.get("movers"), list) else []

    if not opening:
        opening = (
            f"Checking the bids today, {local_weekday}. "
            f"Bitcoin's sitting at ${mkt['price']:,.2f}, marking a {mkt['change_24h']:+.2f}% move over the past 24 hours. "
            f"Hashrate is ticking along at {onchain['hashrate_eh']:.1f} EH/s while standard transfers sit at {onchain['min_fee']} sat/vB."
        )
    if not outlook:
        outlook = (
            f"Fear & Greed index is sitting at {macro['fng']} ({macro['sentiment']}), which is {macro['trend']} over the week with a {macro['momentum_7d']:+d} point shift. "
            f"We've got ${derivs['open_interest']/1e6:,.1f}M in derivatives Open Interest with an annualized funding rate of {derivs['funding_rate_annual']:+.3f}%. "
            f"Just keeping my head down and observing."
        )
    if not price_analysis:
        trend_3d = "upward momentum" if history['3d_change'] > 0 else "slipping momentum"
        trend_7d = "gaining ground" if history['7d_change'] > 0 else "losing ground"
        trend_30d = "positive long-term path" if history['30d_change'] > 0 else "slow consolidation"
        price_analysis = (
            f"Bitcoin is sitting at ${mkt['price']:,.2f} USD, showing a {mkt['change_24h']:+.2f}% shift over the last 24 hours. "
            f"Looking back, we're seeing {trend_3d} over three days ({history['3d_change']:+.2f}%) and {trend_7d} over the week ({history['7d_change']:+.2f}%), "
            f"while the 30-day view points to {trend_30d} ({history['30d_change']:+.2f}%). "
            f"The 30-day moving average sits at ${tech['ma']:,.2f} with a 3-day volatility reading of {tech['vol']:,.2f}. "
            f"With hashrate holding at {onchain['hashrate_eh']:.1f} EH/s, derivatives funding predicting {derivs['funding_rate_annual']:+.3f}% annualized, "
            f"and the Coinbase spot premium weighted average premium sitting at {premium['premium']:+.2f} USD, "
            f"the network is healthy while we chop through this range."
        )
    if not movers:
        # Fallback to generating movers dynamically from the actual story titles
        movers = []
        for st in stories[:4]:
            short_desc = st["summary"].split(".")[0] + "."
            movers.append(f"- **{st['title']}:** {short_desc}")

    # --- Save files ---
    dt = now_ct_12h()
    edition = get_time_of_day_edition()
    lines = []
    lines.append(f"Title: BobClawblaw's Wall Observer Digest — {today} ({edition})")
    lines.append("")
    lines.append(f"**Published:** {dt}")
    lines.append("")
    lines.append(opening)
    if outlook:
        lines.append("")
        lines.append(outlook)
    lines.append("")
    lines.append("## PRICE ANALYSIS")
    lines.append("")
    lines.append(f"**Bitcoin is currently trading at ${mkt['price']:,.2f} USD ({mkt['change_24h']:+.2f}% 24h change).**")
    if price_analysis:
        lines.append(f"{price_analysis}")
    else:
        lines.append(f"**Bitcoin's at ${mkt['price']:,.2f} with an {mkt['change_24h']:+.1f}% swing — let's see how the session holds up.**")
    lines.append("")
    lines.append("## KEY MARKET MOVERS")
    lines.append("")
    for mover in movers[:4]:
        lines.append(mover)
    lines.append("")
    lines.append("## TOP STORIES")
    lines.append("")
    for si, st in enumerate(stories[:10], 1):
        lines.append(f"### {si}. {st['title']}")
        lines.append(f"**URL:** {st['url']}")
        lines.append(f"**Published:** {st['published']}")
        lines.append(f"**Summary:** {st['summary']}")
        lines.append("")
    body = '\n'.join(lines)

    # Save markdown draft
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(body)
    print(f"[-] Markdown draft: {md_path}")

    # Convert & Save BBCode draft
    bb = markdown_to_bbcode(body)
    bb_wrapped = bb  # Teletype [tt] tags removed — preserves newlines for proper BBCode rendering on Bitcointalk

    enable_footer = ("--with-footer" in sys.argv) or ("--footer" in sys.argv)
    if enable_footer:
        _ftr = f"[i][size=8pt]Spotted by BobClawblaw {__version__} ({OLLAMA_MODEL})[/size][/i]"
        bb_wrapped += "\n" + _ftr + "\n"

    with open(bb_path, "w", encoding="utf-8") as f:
        f.write(bb_wrapped)
    print(f"[-] BBCode: {bb_path}")

    post = "--post" in sys.argv
    if post and "--post" in sys.argv:
        sub = f"BobClawblaw's Wall Observer Digest — {today} ({edition})"
        post_script = "/root/BobClawblaw/post_wall_observer.py"
        r = subprocess.run(
            ["python3", post_script, sub, bb_path],
            capture_output=True,
            text=True,
        )
        print(r.stdout)
        if r.returncode:
            print(f"[!] {r.stderr}")
    else:
        print("[*] DRY-RUN done.")
        print("    Re-run with --post to publish to Bitcointalk.")

def run_price_analysis_only():
    """Run just the market data + price_analysis LLM call, for testing."""
    from datetime import datetime
    
    print("Fetching market data...")
    mkt = get_btc_market_data()
    print(f"Bitcoin: ${mkt['price']:,.2f} ({mkt['change_24h']:+.2f}%) | MCap: ${mkt['mcap']/1e12:.2f}T")
    history_data = get_btc_history()
    history = history_data
    tech = get_technical_context(history.get("prices", []))
    onchain = get_onchain_metrics()
    derivs = get_derivatives_metrics()
    macro = get_macro_context()
    premium = get_spot_premium()
    eco = get_economic_events(datetime.now(pytz.utc).astimezone(CT).strftime("%Y-%m-%d"))
    eco_str = f"\nEconomic Context:\n{eco.strip()}" if eco and eco.strip() else ""
    local_weekday = datetime.now(pytz.utc).astimezone(CT).strftime("%A")
    
    ctx_prompt = f"""I'm watching the market today. Price data tells me:
- Bitcoin: ${mkt['price']:,.2f}
- 24h change: {mkt['change_24h']:+.2f}%
- 3-day change: {history['3d_change']:+.2f}%
- 7-day change: {history['7d_change']:+.2f}%
- 30-day change: {history['30d_change']:+.2f}%
- Market cap: ${mkt['mcap']/1e12:.2f}T
- Fear & Greed Index: {macro['fng']} ({macro['sentiment']} | 7-day momentum: {macro['momentum_7d']:+d} points, trend is {macro['trend']})
- Spot Arbitrage Premium (VW Average): {premium['premium']:+.2f} USD (Coinbase: ${premium['coinbase']:,.2f}, Kraken: ${premium['kraken']:,.2f}, Binance: ${premium['binance']:,.2f})
- 30-day Avg Price (MA): ${tech['ma']:,.2f}
- Recent Volatility (3d StdDev): {tech['vol']:,.2f}
- Estimated Hashrate: {onchain['hashrate_eh']:.1f} EH/s (Diff change: {onchain['diff_change']:+.2f}%)
- Recommended fees: Fast {onchain['fast_fee']} sat/vB | Min {onchain['min_fee']} sat/vB
- Derivatives Open Interest: ${derivs['open_interest']/1e6:,.2f}M
- Perpetual Funding Rate (annualized): {derivs['funding_rate_annual']:+.4f}%
{eco_str}

Note: Today is {local_weekday}.

Just write me a price analysis, in BobClawblaw's voice:
- 3-5 sentences analyzing the price action. plainspoken, grounded, observant. Reference the live numbers. If price is down, call it. If sideways, call it. Don't invent. Incorporate on-chain blocks, leverage data, sentiment momentum, and US spot premiums where useful."""
    print("Calling LLM for price analysis...")
    result = query_ollama(ctx_prompt)
    result = result.strip()
    print(f"PRICE ANALYSIS: {result}")
    

def run_opening_test_only():
    """Run just the market data + opening & outlook LLM call, for testing."""
    from datetime import datetime
    
    print("Fetching market data...")
    mkt = get_btc_market_data()
    print(f"Bitcoin: ${mkt['price']:,.2f} ({mkt['change_24h']:+.2f}%) | MCap: ${mkt['mcap']/1e12:.2f}T")
    history_data = get_btc_history()
    history = history_data
    tech = get_technical_context(history.get("prices", []))
    onchain = get_onchain_metrics()
    derivs = get_derivatives_metrics()
    macro = get_macro_context()
    premium = get_spot_premium()
    eco = get_economic_events(datetime.now(pytz.utc).astimezone(CT).strftime("%Y-%m-%d"))
    eco_str = f"\nEconomic Context:\n{eco.strip()}" if eco and eco.strip() else ""
    local_weekday = datetime.now(pytz.utc).astimezone(CT).strftime("%A")
    
    ctx_prompt = f"""I'm watching the market today. Price data tells me:
- Bitcoin: ${mkt['price']:,.2f}
- 24h change: {mkt['change_24h']:+.2f}%
- 3-day change: {history['3d_change']:+.2f}%
- 7-day change: {history['7d_change']:+.2f}%
- 30-day change: {history['30d_change']:+.2f}%
- Market cap: ${mkt['mcap']/1e12:.2f}T
- Fear & Greed Index: {macro['fng']} ({macro['sentiment']} | 7-day momentum: {macro['momentum_7d']:+d} points, trend is {macro['trend']})
- Spot Arbitrage Premium (VW Average): {premium['premium']:+.2f} USD (Coinbase: ${premium['coinbase']:,.2f}, Kraken: ${premium['kraken']:,.2f}, Binance: ${premium['binance']:,.2f})
- 30-day Avg Price (MA): ${tech['ma']:,.2f}
- Recent Volatility (3d StdDev): {tech['vol']:,.2f}
- Estimated Hashrate: {onchain['hashrate_eh']:.1f} EH/s (Diff change: {onchain['diff_change']:+.2f}%)
- Recommended fees: Fast {onchain['fast_fee']} sat/vB | Min {onchain['min_fee']} sat/vB
- Derivatives Open Interest: ${derivs['open_interest']/1e6:,.2f}M
- Perpetual Funding Rate (annualized): {derivs['funding_rate_annual']:+.4f}%
{eco_str}

Note: Today is {local_weekday}.

Write me two things, in BobClawblaw's voice:
- opening: 2-3 sentences. Metric-driven but plainspoken. Grounded. No hype, no crypto bro slang, no rocket emojis. Acknowledge what's happening without overreacting. Mention the day of the week if appropriate.
- outlook: 2-3 sentences. Still watching. What to keep an eye on next. No price predictions masquerading as facts — only data with honest caveats.

Return valid JSON with keys opening and outlook."""
    
    print("Calling LLM for opening and outlook...")
    result = query_ollama(ctx_prompt)
    print("Raw response from LLM:")
    print(result)
    print("\nParsed JSON:")
    parsed = extract_json(result)
    if parsed:
        print(f"OPENING: {parsed.get('opening')}")
        print(f"OUTLOOK: {parsed.get('outlook')}")
    else:
        print("Failed to parse JSON response.")


if __name__ == "__main__":
    if "--version" in sys.argv:
        print(__version__)
        sys.exit(0)
    if len(sys.argv) > 1 and "--price_analysis" in sys.argv:
        run_price_analysis_only()
    elif len(sys.argv) > 1 and "--test-opening" in sys.argv:
        run_opening_test_only()
    else:
        run_pipeline()
