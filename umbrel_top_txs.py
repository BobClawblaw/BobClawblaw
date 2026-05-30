#!/usr/bin/env python3
"""
umbrel_top_txs_v12.py

Query your Umbrel node's Mempool app (port 3006) for the top 10
highest-value BTC transactions (TXIDs) over the last 24 hours.
Links directly to specific transactions in mempool.space.
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import requests
import subprocess
import gzip
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Constants
CT = timezone(timedelta(hours=-6))
NOW = datetime.now(CT)
DATE = NOW.strftime("%Y-%m-%d")
DSTR = "%s %s" % (DATE, NOW.strftime("%-I:%M %p %Z"))
OUT_DIR = Path("/root/.hermes/bobclawblaw/digests")
MEMP_TX = "https://mempool.space/tx"
UB = "http://umbrel.local:3006"

def get_btc_price() -> float:
    try:
        r = requests.get("https://api.coinbase.com/v2/prices/BTC-USD/spot", timeout=10)
        return float(r.json().get("data", {}).get("amount", 76500.0))
    except:
        return 76500.0

def _get(path, timeout_s=30):
    cmd = ["curl", "-s", "-f", "-L", "-H", "Accept: application/json", "-m", str(timeout_s), f"{UB}{path}"]
    try:
        process = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout_s)
        if process.returncode != 0: return None
        raw = process.stdout
        if raw[:2] == b"\x1f\x8b": raw = gzip.decompress(raw)
        return json.loads(raw)
    except: return None

def fetch_block_tx_details(block_id, tx_count):
    """Retrieve transaction details page-by-page. Limits scanning to the first page (top 10 txs) of each block for optimal speed."""
    logging.info(f"Scanning largest transactions for block {block_id} ({tx_count} transactions)...")
    tx_details = []
    
    # We only scan the very first page (first 10 txs) of each block,
    # as blocks are ordered by fee/value and the largest transaction outputs/coinbase rewards live at the start.
    scan_limit = min(tx_count, 10)
    pages = list(range(0, scan_limit, 10))
    
    def fetch_page(offset):
        url_path = f"/api/v1/block/{block_id}/txs/{offset}"
        return _get(url_path)

    with ThreadPoolExecutor(max_workers=5) as executor:
        future_to_offset = {executor.submit(fetch_page, offset): offset for offset in pages}
        for future in as_completed(future_to_offset):
            res = future.result()
            if res:
                tx_details.extend(res)
                
    return tx_details

def fetch_top_txs(top_n=10):
    """Grab recent blocks over the last 24 hours, fetch transaction values, and return top N."""
    cutoff = NOW.timestamp() - 86400
    blocks = _get("/api/v1/blocks") or []
    
    # Ensure 24h coverage
    while blocks and blocks[-1].get("timestamp", 0) >= cutoff:
        oldest_height = blocks[-1].get("height")
        more = _get(f"/api/v1/blocks/{oldest_height}")
        if not more: break
        blocks.extend(more)

    # Deduplicate blocks by ID to prevent duplicate transactions
    seen_block_ids = set()
    dedup_blocks = []
    for b in blocks:
        bid = b.get("id")
        if bid and bid not in seen_block_ids:
            seen_block_ids.add(bid)
            dedup_blocks.append(b)

    blocks_24h = [b for b in dedup_blocks if b.get("timestamp", 0) >= cutoff]
    logging.info(f"Iterating through {len(blocks_24h)} blocks strictly inside the 24-hour window.")
    
    all_candidates = []
    seen_txids = set()
    
    for b in blocks_24h:
        block_id = b["id"]
        tx_count = b.get("tx_count", 0)
        timestamp = b.get("timestamp")
        
        txs = fetch_block_tx_details(block_id, tx_count)
        
        for tx in txs:
            txid = tx["txid"]
            if txid in seen_txids:
                continue
            seen_txids.add(txid)
            
            # Fetch the actual confirmed fee if the API returned -1 or incorrect value
            fee = tx.get("fee", 0)
            if fee <= 0:
                full_tx = _get(f"/api/v1/tx/{txid}")
                if full_tx:
                    fee = full_tx.get("fee", 0)

            val = sum(out.get("value", 0) for out in tx.get("vout", []))
            all_candidates.append({
                "txid": txid,
                "value": val,
                "fee": fee,
                "timestamp": timestamp
            })

    # Sort all transactions across all blocks by output value DESC
    sorted_txs = sorted(all_candidates, key=lambda x: x["value"], reverse=True)
    return sorted_txs[:top_n]

def format_compact_usd(val):
    if val >= 1_000_000_000:
        return f"${val / 1_000_000_000:.2f}B"
    elif val >= 1_000_000:
        return f"${val / 1_000_000:.2f}M"
    else:
        return f"${val:,.2f}"

def render_md(txs, price):
    lines = ["# Top %d Largest Transactions over the Last 24 Hours" % len(txs), DSTR, ""]
    for i, t in enumerate(txs, 1):
        val_btc = t["value"] / 1e8
        val_usd = int(val_btc * price)
        fee_btc = t["fee"] / 1e8
        fee_usd = fee_btc * price
        link = f"{MEMP_TX}/{t['txid']}"
        
        # Format block mining approximate timestamp to CT
        dt = datetime.fromtimestamp(t["timestamp"], timezone.utc).astimezone(CT)
        time_str = dt.strftime("%Y-%m-%d %I:%M %p CT")

        lines.append(f"## {i}. [{t['txid'][:32]}]({link})")
        lines.append(f"   - Date/Time: {time_str}")
        lines.append(f"   - Value: {val_btc:.4f} BTC (~${val_usd:,})")
        lines.append(f"   - Fee: {t['fee']:,} sats (~${fee_usd:,.2f})")
        lines.append("")
    return "\n".join(lines)

def render_bb(txs, price):
    bb = "[SIZE=4][B]Top %d Largest Transactions (24h)[/B][/SIZE]\n\n" % len(txs)
    bb += "[TABLE]\n[TR]"
    bb += "[TD][B]Rank[/B]  [/TD]"
    bb += "[TD][B]TXID[/B]      [/TD]"
    bb += "[TD][B]Mining Time (CT)[/B]  [/TD]"
    bb += "[TD][B]Value (BTC/USD)[/B]      [/TD]"
    bb += "[TD][B]Fee (sats/USD)[/B][/TD]"
    bb += "[/TR]\n"
    
    for i, t in enumerate(txs, 1):
        val_btc = t["value"] / 1e8
        val_usd = int(val_btc * price)
        val_str = f"{val_btc:,.4f} BTC ({format_compact_usd(val_usd)})"
        
        fee_btc = t["fee"] / 1e8
        fee_usd = fee_btc * price
        fee_str = f"{t['fee']:,} sats (${fee_usd:,.2f})"
        
        tx_link = f"[URL={MEMP_TX}/{t['txid']}]{t['txid'][:8]}...{t['txid'][-8:]}[/URL]"
        
        # Format transaction timestamp to CT
        dt = datetime.fromtimestamp(t["timestamp"], timezone.utc).astimezone(CT)
        time_str = dt.strftime("%m-%d %I:%M %p")
        
        bb += f"[TR]"
        bb += f"[TD]{i}  [/TD]"
        bb += f"[TD]{tx_link}      [/TD]"
        bb += f"[TD]{time_str}  [/TD]"
        bb += f"[TD]{val_str}      [/TD]"
        bb += f"[TD]{fee_str}[/TD]"
        bb += f"[/TR]\n"
        
    bb += "[/TABLE]"
    return bb

if __name__ == "__main__":
    price = get_btc_price()
    txs = fetch_top_txs(10)
    
    md_content = render_md(txs, price)
    bb_content = render_bb(txs, price)
    
    path_md = OUT_DIR / f"txs-v12-{DATE}.md"
    path_bb = OUT_DIR / f"txs-v12-{DATE}.bbcode.txt"
    
    with open(path_md, "w") as f: f.write(md_content)
    with open(path_bb, "w") as f: f.write(bb_content)
    
    print(bb_content)
