#!/usr/bin/env python3
"""buddyblocker.py — detect a "buddychain" from the local index DB.

This version does NOT fetch or parse forum HTML.
It reads /root/.hermes/bobclawblaw/wall_posts.db, looks at the most recent
posts, and finds the longest *current tail streak* of consecutive ChartBuddy
posts.

Default behavior: detection-only (no posting).

If you enable posting (--post):
- It posts only when the current tail streak of consecutive ChartBuddy posts is
  strictly greater than `--streak` (default: 4).
- For every consecutive ChartBuddy post beyond that threshold, it attempts a
  post with 33% probability.
- If the 33% roll fails, it skips posting for now.
- The BBCode header it would post uses `streak_len` to set the number of `B`s,
  so if ChartBuddy keeps posting and the streak grows, the next run’s header
  grows by 1 more `B`.

Use --post to enable posting (requires posting_util.py + credentials).
"""

from __future__ import annotations

import argparse
import colorsys
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


def rainbow(text: str) -> str:
    """Return BB-style per-character rainbow gradient (start -> finish)."""
    n = max(1, len(text))
    out: List[str] = []
    for i, ch in enumerate(text):
        t = 0.0 if n == 1 else i / (n - 1)
        # Hue sweep across the rainbow.
        hue = t * 300.0  # 0..300 avoids wrapping hue back to red.
        r, g, b = colorsys.hsv_to_rgb(hue / 360.0, 1.0, 1.0)
        col = f"#{int(r * 255):02X}{int(g * 255):02X}{int(b * 255):02X}"
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
    #   B-B-B-...-B-Buddy-Blocker!!!
    # i.e. dashes between the B's, and a dash between the last B and Buddy-Blocker.
    # Header grows with the current tail streak length.
    n = max(4, streak)
    header_text = ("-".join(["B"] * n)) + "-Buddy-Blocker!!!"
    header_raw = f"[ {header_text} ]"
    header = rainbow(header_raw)

    # Flat post: don't quote/copy ChartBuddy content.
    # NO extra lines in the output body.
    return header



def maybe_post(chain: List[Dict[str, Any]], streak: int) -> bool:
    if post_with_login is None:
        raise RuntimeError("--post requested but posting_util.post_with_login is unavailable")

    body = build_message(chain, streak)
    subj = "[B-B-B-B Buddyblocker!]!!!"
    # Keep the board wiring consistent with original buddyblocker.
    return post_with_login(THREAD, subj, body, board="57")


def main() -> None:
    ap = argparse.ArgumentParser()
    # After this many consecutive ChartBuddy posts in the *tail*, we start
    # applying the rarity rule.
    # Default: after 4 consecutive posts, each successive post has a 33 percent chance.
    ap.add_argument(
        "--streak",
        type=int,
        default=4,
        help="Tail streak threshold. No posting at exactly this value; attempt posting with 33 percent when streak > this.",
    )
    ap.add_argument(
        "--post",
        action="store_true",
        help="Actually post to the forum when the 33 percent rule fires.",
    )
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument(
        "--no-post",
        action="store_true",
        help="Detection-only. Overrides --post.",
    )
    ap.add_argument(
        "--dry",
        action="store_true",
        help="Alias for --no-post; write bbcode to /tmp instead of posting.",
    )
    ap.add_argument(
        "--force",
        action="store_true",
        help="Force posting even if the 33 percent roll fails (for testing).",
    )
    ap.add_argument(
        "--ignore-dedup",
        action="store_true",
        help="Ignore the dedup file for the current top_msg_id (for testing).",
    )
    ap.add_argument(
        "--no-reindex-after-post",
        action="store_true",
        help="Do not run wall_observer_indexer.py immediately after posting (testing).",
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

    b_count_if_posted = max(4, streak_len)
    print(f"[buddy] If it posts now, header B-count would be: {b_count_if_posted}")

    print(f"[buddy] Tail streak detected: streak_len={streak_len} top_msg_id={top_msg_id}")

    # Print excerpt details.
    for i, p in enumerate(chain, 1):
        author = p.get("author")
        subject = (p.get("subject") or "").replace("\n", " ")
        print(f"  [{i}/{streak_len}] msg_id={p.get('msg_id')} author={author} subject={subject[:60]}")

    # New rule:
    # - No posting at exactly `chance_start`.
    # - For every successive consecutive post beyond `chance_start`,
    #   post with 33 percent probability.
    if streak_len <= chance_start:
        print(f"[buddy] No firing: need > {chance_start} (33% rule starts after {chance_start}).")
        sys.exit(0)

    P_FIRE = 0.33

    # Dedup: roll once per newest msg_id.
    dedup = load_dedup()
    if (not args.ignore_dedup) and dedup.get("top_msg_id") == top_msg_id:
        print(f"[buddy] Already rolled for top_msg_id={top_msg_id}; skipping.")
        sys.exit(0)

    if args.force:
        fired = True
    else:
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
        print("[buddy] No post this run. If ChartBuddy keeps posting and the streak grows, next run’s header will have one more 'B' (it tracks streak_len).")
        sys.exit(0)

    msg = build_message(chain, streak_len)

    # Output message text
    out_path = "/tmp/buddyblocker_out.bbcode"
    if args.dry or args.no_post or not args.post:
        Path(out_path).write_text(msg)
        if args.dry:
            print(f"[buddy] Dry run. Wrote: {out_path}")
        elif args.no_post:
            print(f"[buddy] No-post mode. Wrote: {out_path}")
        else:
            print(f"[buddy] Posting disabled (missing --post). Wrote: {out_path}")
        sys.exit(0)

    ok = maybe_post(chain, streak_len)
    if ok:
        print("[buddy] Posted.")
        if not args.no_reindex_after_post:
            try:
                subprocess.run(
                    ["python3", "/root/BobClawblaw/wall_observer_indexer.py"],
                    check=False,
                    timeout=600,
                )
                print("[buddy] Reindexed wall observer after posting.")
            except Exception as e:
                print(f"[buddy] Reindex after post failed: {e}")
    else:
        print("[buddy] Post did not succeed.")



if __name__ == "__main__":
    main()
