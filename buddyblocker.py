#!/usr/bin/env python3
"""buddyblocker.py — detect a "buddychain" from the local index DB.

This version does NOT fetch or parse forum HTML.
It reads /root/.hermes/bobclawblaw/wall_posts.db, looks at the most recent
posts, and finds the longest *current tail streak* of consecutive ChartBuddy
posts.

Default behavior: detection-only (no posting).
Use --post to enable posting (requires posting_util.py + credentials).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict, Any, Optional
import random

# Optional (only needed if --post is used)
try:
    from posting_util import post_with_login  # type: ignore
except Exception:
    post_with_login = None


# ---- Config ----
DB_PATH = "/root/.hermes/bobclawblaw/wall_posts.db"
THREAD = "178336"
# The author name as stored by wall_observer_indexer.py
CHARTBUDDY_AUTHOR = "ChartBuddy"

DEDUP_JSON = "/tmp/buddyblocker_last_streak.json"
HEX = ["#EF3340", "#E48118", "#F8E41E", "#12C167", "#158CE0", "#D021E3"]


def rainbow(text: str, colors: List[str] = HEX) -> str:
    """Return BB-style color tags for BobClawblaw."""
    out = []
    for i, ch in enumerate(text):
        col = colors[i % len(colors)]
        out.append(f"[COLOR={col}]{ch}[/COLOR]")
    return "".join(out)


def load_dedup() -> Dict[str, Any]:
    try:
        return json.load(open(DEDUP_JSON))
    except Exception:
        return {}


def save_dedup(d: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(DEDUP_JSON), exist_ok=True)
    json.dump(d, open(DEDUP_JSON, "w"), indent=2)


def fetch_recent_posts(limit: int = 200) -> List[Dict[str, Any]]:
    if not os.path.exists(DB_PATH):
        raise FileNotFoundError(f"DB not found: {DB_PATH}")

    conn = sqlite3.connect(DB_PATH)
    try:
        rows = conn.execute(
            """SELECT msg_id, author, author_uid, page_num, subject, body
               FROM posts
               ORDER BY msg_id DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()

        out: List[Dict[str, Any]] = []
        for (msg_id, author, author_uid, page_num, subject, body) in rows:
            out.append(
                {
                    "msg_id": msg_id,
                    "author": author,
                    "author_uid": author_uid,
                    "page_num": page_num,
                    "subject": subject or "",
                    # keep a compact excerpt for the banner
                    "msg": (body or "").strip(),
                }
            )
        return out
    finally:
        conn.close()


def get_tail_chain(posts_desc: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Return the current tail streak of consecutive ChartBuddy posts.

    posts_desc must be newest-first.
    We count consecutive ChartBuddy posts starting at the newest post.
    This always returns the tail chain length (possibly empty).
    """
    chain: List[Dict[str, Any]] = []
    for p in posts_desc:
        if p.get("author") != CHARTBUDDY_AUTHOR:
            break
        chain.append(p)
    return chain


def build_message(chain: List[Dict[str, Any]], streak: int) -> str:
    # Header format requested:
    #   B-B-B-B-B-B-BBuddy-Blocker!!!
    # i.e. dashes between the B's, but no dash between the last B and Buddy-Blocker!!!
    n = max(4, streak)
    if n == 1:
        header = "BBuddy-Blocker!!!"
    else:
        header = ("-".join(["B"] * (n - 1))) + "-B" + "Buddy-Blocker!!!"

    parts = [header]
    for p in chain:
        msg = p.get("msg", "")
        parts.append(str(msg)[:120])

    return " ".join(parts)


def maybe_post(chain: List[Dict[str, Any]], streak: int) -> None:
    if post_with_login is None:
        raise RuntimeError("--post requested but posting_util.post_with_login is unavailable")

    body = build_message(chain, streak)
    subj = "[B-B-B-B Buddyblocker!]!!!"

    # Keep the board wiring consistent with original buddyblocker.
    post_with_login(THREAD, subj, body, board="57")


def main() -> None:
    ap = argparse.ArgumentParser()
    # After this many consecutive ChartBuddy posts in the *tail*, we start
    # applying the rarity rule.
    # Default: after 4 consecutive posts, each successive post has a 33% chance.
    ap.add_argument("--streak", type=int, default=4)
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument(
        "--no-post",
        action="store_true",
        help="Detection-only. Never post to the forum.",
    )
    ap.add_argument(
        "--dry",
        action="store_true",
        help="Alias for --no-post; write bbcode to /tmp instead of posting.",
    )
    args = ap.parse_args()

    # Read recent posts and detect current tail streak.
    posts = fetch_recent_posts(limit=args.limit)
    if not posts:
        print("[buddy] No posts in DB yet.")
        sys.exit(0)

    chain = get_tail_chain(posts)  # newest-first tail chain
    streak_len = len(chain)
    if streak_len == 0:
        print("[buddy] Tail streak is 0.")
        sys.exit(0)

    top_msg_id = chain[0].get("msg_id")
    chance_start = args.streak

    print(f"[buddy] Tail streak detected: streak_len={streak_len} top_msg_id={top_msg_id}")

    # Print excerpt details.
    for i, p in enumerate(chain, 1):
        author = p.get("author")
        subject = (p.get("subject") or "").replace("\n", " ")
        print(f"  [{i}/{streak_len}] msg_id={p.get('msg_id')} author={author} subject={subject[:60]}")

    # New rule:
    # - No posting at exactly `chance_start`.
    # - For every successive consecutive post beyond `chance_start`,
    #   post with 33% probability.
    if streak_len <= chance_start:
        print(f"[buddy] No firing: need > {chance_start} (33% rule starts after {chance_start}).")
        sys.exit(0)

    P_FIRE = 0.33

    # Dedup: roll once per newest msg_id.
    dedup = load_dedup()
    if dedup.get("top_msg_id") == top_msg_id:
        print(f"[buddy] Already rolled for top_msg_id={top_msg_id}; skipping.")
        sys.exit(0)

    fired = random.random() < P_FIRE

    now = datetime.now(timezone.utc).isoformat()
    dedup.update(
        {
            "top_msg_id": top_msg_id,
            "streak": streak_len,
            "triggered_at": now,
            "fired": fired,
            "p_fire": P_FIRE,
            "chance_start": chance_start,
        }
    )
    save_dedup(dedup)

    if not fired:
        print(f"[buddy] Buddychain would fire, but 33% roll failed (p={P_FIRE}).")
        sys.exit(0)

    msg = build_message(chain, streak_len)

    # Output message text
    out_path = "/tmp/buddyblocker_out.bbcode"
    if args.no_post or args.dry:
        Path(out_path).write_text(msg)
        print(f"[buddy] No-post mode. Wrote: {out_path}")
        sys.exit(0)

    maybe_post(chain, streak_len)
    print("[buddy] Posted.")



if __name__ == "__main__":
    main()
