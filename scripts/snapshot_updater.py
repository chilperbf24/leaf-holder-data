#!/usr/bin/env python3
"""
LEAF holder snapshot updater - pure script, no LLM tokens.
Fetches crc.garden ledger, computes holder ranking, saves JSON,
and pushes to GitHub for the public website.
Run via systemd timer every 10 minutes.
"""
import json
import time
import os
import urllib.request
import subprocess
import base64
from collections import defaultdict
from datetime import datetime, timezone

# Base directory: script's location (works both locally and in GitHub Actions)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
IN_ACTIONS = os.environ.get('GITHUB_ACTIONS') == 'true'

LEDGER_URL = "https://crc.garden/api/activity"
OUTPUT = os.path.join(BASE_DIR, "ledger_snapshot.json")
# In Actions, holders.json goes to repo root (parent of script dir if in scripts/)
# Locally, we push via API so this is just for reference
if IN_ACTIONS:
    # Script is in repo root or scripts/ dir; holders.json goes to repo root
    _repo_root = os.environ.get('GITHUB_WORKSPACE', BASE_DIR)
    HOLDERS_OUTPUT = os.path.join(_repo_root, "holders.json")
else:
    HOLDERS_OUTPUT = None
GITHUB_REPO_OWNER = "chilperbf24"
GITHUB_REPO = "leaf-holder-data"
GITHUB_PATH = "holders.json"

def fetch(url, retries=3):
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            return json.load(urllib.request.urlopen(req, timeout=30))
        except Exception as e:
            if i == retries - 1:
                raise
            time.sleep(5)

def main():
    # Paginate through all activity
    all_records = []
    offset = 0
    while True:
        d = fetch(f"{LEDGER_URL}?offset={offset}&limit=100")
        data = d.get('data', [])
        all_records.extend(data)
        if not d.get('has_more'):
            break
        offset = d.get('next_offset', offset + 100)
        time.sleep(0.3)
        if len(all_records) > 10000:
            break

    # Note: 812 mints total exactly 1B (790 fractional + 22 whole).
    # Do NOT filter by amount - all mints are valid.
    # (Earlier issue with 1,000,741,542 was from duplicate records, not fractional amounts)

    # Compute balances
    balances = defaultdict(float)
    mint_count = defaultdict(int)
    tx_count = defaultdict(int)

    for r in all_records:
        op = r.get('op')
        try:
            amt = float(r.get('amount') or 0)
        except:
            continue
        if op == 'mint':
            to_addr = r.get('to_address')
            if to_addr:
                balances[to_addr] += amt
                mint_count[to_addr] += 1
                tx_count[to_addr] += 1
        elif op == 'transfer':
            from_addr = r.get('from_address')
            to_addr = r.get('to_address')
            if from_addr:
                balances[from_addr] -= amt
                tx_count[from_addr] += 1
            if to_addr:
                balances[to_addr] += amt
                tx_count[to_addr] += 1

    # Load staking status
    try:
        with open(os.path.join(BASE_DIR, 'staking_status.json')) as f:
            staking_map = json.load(f)
    except:
        staking_map = {}
    # 0=未知, 1=质押中, 2=已解锁
    staking_code = {'未知': 0, '质押中': 1, '已解锁': 2}

    # Fetch market trades to identify buy/sell
    # API: https://crc.garden/api/marketplace/stats?ticker=LEAF
    # Persistent history: trade_history.json accumulates txids across runs
    import os
    HISTORY_FILE = os.path.join(os.path.dirname(__file__), 'trade_history.json')
    trade_txids = set()
    trade_prices = {}  # txid -> price_usd_per_token
    # Load historical
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, 'r') as f:
                hist = json.load(f)
                trade_txids.update(hist.get('txids', []))
                trade_prices.update(hist.get('prices', {}))
            print(f"Loaded {len(trade_txids)} historical trade txids")
        except Exception as e:
            print(f"History load failed: {e}")
    # Fetch current
    new_count = 0
    try:
        mkt = fetch("https://crc.garden/api/marketplace/stats?ticker=LEAF")
        mdata = mkt.get('data', {})
        for pp in mdata.get('price_points', []):
            txid = pp.get('transaction_id')
            if txid and txid not in trade_txids:
                new_count += 1
            if txid:
                trade_txids.add(txid)
                if pp.get('price_usd_per_token'):
                    trade_prices[txid] = pp.get('price_usd_per_token')
        for candle in mdata.get('candles', []):
            for txid in (candle.get('transactionId') or '').split('|'):
                txid = txid.strip()
                if txid and txid not in trade_txids:
                    new_count += 1
                if txid:
                    trade_txids.add(txid)
        print(f"Market trades: {len(trade_txids)} total ({new_count} new)")
        # Save updated history
        with open(HISTORY_FILE, 'w') as f:
            json.dump({'txids': sorted(trade_txids), 'prices': trade_prices,
                       'updated': datetime.now(timezone.utc).isoformat()}, f)
    except Exception as e:
        print(f"Market fetch failed: {e}")

    # Build per-address ALL transactions (for paginated modal view)
    # Format: [txid_short, op, amount, block, direction, is_trade, timestamp, counterparty]
    # counterparty: the other address (sender for 'in', receiver for 'out')
    addr_txs = defaultdict(list)
    for r in sorted(all_records, key=lambda x: x.get('block_height', 0), reverse=True):
        txid = r.get('txid')
        op = r.get('op')
        amt = r.get('amount')
        bh = r.get('block_height')
        ts = r.get('timestamp', '')
        txid_short = txid[:16] if txid else ''
        is_trade = 1 if txid in trade_txids else 0
        if op == 'mint':
            to_addr = r.get('to_address')
            if to_addr:
                addr_txs[to_addr].append([txid_short, 'mint', amt, bh, 'mint', 0, ts, ''])
        elif op == 'transfer':
            frm = r.get('from_address')
            to = r.get('to_address')
            if frm:
                addr_txs[frm].append([txid_short, 'transfer', amt, bh, 'out', is_trade, ts, to or ''])
            if to:
                addr_txs[to].append([txid_short, 'transfer', amt, bh, 'in', is_trade, ts, frm or ''])

    holders = [
        [addr, round(bal, 2), mint_count[addr], tx_count[addr],
         staking_code.get(staking_map.get(addr, '未知'), 0)]
        for addr, bal in balances.items()
        if bal > 0.01
    ]
    holders.sort(key=lambda x: -x[1])
    total = sum(h[1] for h in holders)

    # Staking sets for unlock detection
    # minters: all addresses that ever minted (for unlock detection)
    # staking_addrs: currently staking (for status display)
    staking_addrs = {addr for addr, st in staking_map.items() if st == '质押中'}
    minters = set()
    for r in all_records:
        if r.get('op') == 'mint' and r.get('to_address'):
            minters.add(r.get('to_address'))
    first_out = {}
    for r in sorted(all_records, key=lambda x: x.get('block_height', 0)):
        if r.get('op') == 'transfer':
            frm = r.get('from_address')
            if frm and frm not in first_out:
                first_out[frm] = r.get('txid')

    # 7-day holding change: reconstruct from activity timestamps
    # EXCLUDES unlock events (first transfer out from staking) - only counts buys/sells and plain transfers
    # balance_7d_ago = current - mints_7d - received_7d + sent_7d (excluding unlocks)
    from datetime import timedelta
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=7)
    change_7d = defaultdict(float)  # addr -> net change (positive = increased)
    for r in all_records:
        ts_str = r.get('timestamp', '')
        if not ts_str:
            continue
        try:
            ts = datetime.fromisoformat(ts_str.replace('Z', '+00:00'))
        except:
            continue
        if ts < cutoff:
            continue
        op = r.get('op')
        amt = r.get('amount', 0)
        try:
            amt = float(amt) if amt else 0
        except:
            amt = 0
        if op == 'mint':
            to_addr = r.get('to_address')
            if to_addr:
                change_7d[to_addr] += amt
        elif op == 'transfer':
            frm = r.get('from_address')
            to = r.get('to_address')
            txid = r.get('txid')
            # Skip unlock events (first transfer out from a minter)
            is_unlock = (frm in minters and first_out.get(frm) == txid)
            if is_unlock:
                continue
            if frm:
                change_7d[frm] -= amt
            if to:
                change_7d[to] += amt
    # Add 7d change as 6th field: [addr, bal, mints, txs, stake, change_7d]
    holders = [h + [round(change_7d.get(h[0], 0), 2)] for h in holders]

    # Latest activity: FULL history from CRC-20 start, with persistent history DB
    # Format: [txid, op, address, amount, block, is_trade, timestamp, from_addr, to_addr, is_unlock]
    import os
    ACT_HISTORY_FILE = os.path.join(os.path.dirname(__file__), 'activity_history.json')
    # Load existing history
    hist_records = {}
    if os.path.exists(ACT_HISTORY_FILE):
        try:
            with open(ACT_HISTORY_FILE, 'r') as f:
                hist_data = json.load(f)
                for r in hist_data.get('records', []):
                    # Key by txid+op+from+to to deduplicate
                    key = f"{r.get('txid')}|{r.get('op')}|{r.get('from_address')}|{r.get('to_address')}"
                    hist_records[key] = r
            print(f"Loaded {len(hist_records)} historical activity records")
        except Exception as e:
            print(f"Activity history load failed: {e}")
    # Merge current records
    new_act = 0
    for r in all_records:
        key = f"{r.get('txid')}|{r.get('op')}|{r.get('from_address')}|{r.get('to_address')}"
        if key not in hist_records:
            hist_records[key] = r
            new_act += 1
    print(f"Activity history: {len(hist_records)} total ({new_act} new)")
    # Save updated history
    try:
        with open(ACT_HISTORY_FILE, 'w') as f:
            json.dump({'records': list(hist_records.values()),
                       'updated': datetime.now(timezone.utc).isoformat()}, f)
    except Exception as e:
        print(f"Activity history save failed: {e}")
    # Build full sorted list for website
    all_hist = sorted(hist_records.values(), key=lambda x: x.get('block_height', 0), reverse=True)
    # Compute unlock info for full history
    latest_compact = []
    for r in all_hist:
        txid = r.get('txid')
        frm = r.get('from_address', '')
        # Unlock = first outgoing transfer from an address that minted
        is_unlock = 1 if (r.get('op') == 'transfer' and frm in minters
                          and first_out.get(frm) == txid) else 0
        latest_compact.append(
            [txid, r.get('op'), r.get('to_address') or frm,
             r.get('amount'), r.get('block_height'),
             1 if txid in trade_txids else 0,
             r.get('timestamp', ''), frm, r.get('to_address', ''), is_unlock]
        )

    # Moved vs unmoved: address ever involved in ANY transfer (sent OR received)?
    # Unmoved = pure minters who never touched a transfer
    # This matches the dashboard definition (57.7% unmoved / 42.3% moved)
    transfer_addrs = set()
    for r in all_records:
        if r.get('op') == 'transfer':
            if r.get('from_address'):
                transfer_addrs.add(r.get('from_address'))
            if r.get('to_address'):
                transfer_addrs.add(r.get('to_address'))
    moved_bal = sum(h[1] for h in holders if h[0] in transfer_addrs)
    unmoved_bal = total - moved_bal
    moved_cnt = sum(1 for h in holders if h[0] in transfer_addrs)
    unmoved_cnt = len(holders) - moved_cnt
    top3_pct = sum(h[1] for h in holders[:3]) / total * 100 if total else 0
    top10_pct = sum(h[1] for h in holders[:10]) / total * 100 if total else 0
    mint_cnt = sum(1 for r in all_records if r.get('op') == 'mint')
    transfer_cnt = sum(1 for r in all_records if r.get('op') == 'transfer')

    now = datetime.now(timezone.utc).isoformat()
    snapshot = {
        'u': now,                                        # updated_at
        'bh': max((r.get('block_height', 0) for r in all_records), default=0),  # block height
        'tr': len(all_records),                          # total records
        'th': len(holders),                              # total holders
        'ts': round(total, 2),                           # total supply
        'h': holders,                                    # [address, balance, mint_count, tx_count, staking(0/1/2)]
        'la': latest_compact,                            # [txid, op, address, amount, block]
        'txs': {addr: txs for addr, txs in addr_txs.items() if txs},  # per-address recent txs
        'stats': {
            'moved_bal': round(moved_bal, 2),
            'unmoved_bal': round(unmoved_bal, 2),
            'moved_cnt': moved_cnt,
            'unmoved_cnt': unmoved_cnt,
            'top3_pct': round(top3_pct, 2),
            'top10_pct': round(top10_pct, 2),
            'mint_cnt': mint_cnt,
            'transfer_cnt': transfer_cnt,
        },
    }

    with open(OUTPUT, 'w') as f:
        json.dump(snapshot, f)

    print(f"[{now}] Saved {len(holders)} holders, total {total:,.2f}")

    # VALIDATION: protect against bad data
    # - mint count must be 812
    # - total must be close to 1B (999,999,999.99)
    if mint_cnt != 812:
        print(f"VALIDATION FAILED: mint count {mint_cnt} != 812, NOT pushing to GitHub")
        return
    if abs(total - 999999999.99) > 1000:
        print(f"VALIDATION FAILED: total {total:,.2f} not close to 1B, NOT pushing to GitHub")
        return
    print("Validation passed")
    if IN_ACTIONS:
        # In GitHub Actions: write holders.json to repo, workflow handles git push
        if HOLDERS_OUTPUT:
            with open(HOLDERS_OUTPUT, 'w') as f:
                json.dump(snapshot, f, separators=(',', ':'))
            print(f"Wrote {HOLDERS_OUTPUT}")
    else:
        push_to_github(snapshot, now)

def push_to_github(snapshot, now):
    """Push snapshot to GitHub repo via REST API using PAT (no approval needed)."""
    try:
        token_file = "/home/hatch/.config/leaf/github_token"
        with open(token_file) as f:
            token = f.read().strip()
        if not token:
            print("No GitHub token, skipping push")
            return

        raw_json = json.dumps(snapshot, separators=(',', ':'))
        content_b64 = base64.b64encode(raw_json.encode()).decode()

        # Get current SHA
        req = urllib.request.Request(
            f"https://api.github.com/repos/{GITHUB_REPO_OWNER}/{GITHUB_REPO}/contents/{GITHUB_PATH}?ref=main",
            headers={"Authorization": f"token {token}", "User-Agent": "leaf-snapshot/1.0"}
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                sha = json.load(r)["sha"]
        except Exception:
            sha = None  # file doesn't exist yet

        # Push
        data = {
            "message": f"Update snapshot {now}",
            "content": content_b64,
            "branch": "main"
        }
        if sha:
            data["sha"] = sha

        req = urllib.request.Request(
            f"https://api.github.com/repos/{GITHUB_REPO_OWNER}/{GITHUB_REPO}/contents/{GITHUB_PATH}",
            data=json.dumps(data).encode(),
            headers={
                "Authorization": f"token {token}",
                "User-Agent": "leaf-snapshot/1.0",
                "Content-Type": "application/json"
            },
            method="PUT"
        )
        with urllib.request.urlopen(req, timeout=60) as r:
            result = json.load(r)
            print(f"GitHub push OK: {result['commit']['sha'][:8]}")
    except Exception as e:
        print(f"GitHub push error: {e}")

if __name__ == '__main__':
    main()
