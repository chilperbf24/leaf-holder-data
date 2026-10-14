#!/usr/bin/env python3
"""
LEAF holder snapshot updater - data source: crc20.org (CRC-20 indexer).
Pure script, no LLM tokens.

Fetches crc20.org/api/v1/history (cursor pagination, tick=LEAF), computes
holder ranking, saves JSON for the public website.

Run via GitHub Actions every 15 minutes.

Field mapping (crc20.org -> internal):
  type=mint                    -> op mint,      to += amount
  type=transfer                -> op transfer,  from -= amount, to += amount
                                 template=market marks a marketplace trade
  type=swap   side=buy (AXBT->LEAF)  -> to += output_atoms
  type=swap   side=sell (LEAF->AXBT) -> from -= input_atoms
  type=liquidity action=deposit -> from -= LEAF deposit (locked in pool)
  type=liquidity action=payback -> to += LEAF (defensive; currently absent
                                 from the history listing upstream)
  The pool itself is tracked as pseudo-holder SWAP_POOL:4:AXBT:4:LEAF so its
  LEAF stays in the holder ranking and the total reconciles to 1B.

Amounts are atoms (8 decimals) as strings -> LEAF float.
"""
import json
import time
import os
import urllib.request
import subprocess
import base64
from collections import defaultdict
from datetime import datetime, timezone, timedelta

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
IN_ACTIONS = os.environ.get('GITHUB_ACTIONS') == 'true'

API = "https://crc20.org/api/v1"
TICK = "LEAF"
ATOMS_PER_LEAF = 100_000_000
# The AMM pool has no on-chain address in the new indexer (to/from are null
# on its side); track it as a pseudo-holder so pool-locked LEAF stays counted.
POOL_ADDR = "SWAP_POOL:4:AXBT:4:LEAF"

OUTPUT = os.path.join(BASE_DIR, "ledger_snapshot.json")

if IN_ACTIONS:
    _repo_root = os.environ.get('GITHUB_WORKSPACE', BASE_DIR)
    HOLDERS_OUTPUT = os.path.join(_repo_root, "holders.json")
else:
    HOLDERS_OUTPUT = None

GITHUB_REPO_OWNER = "chilperbf24"
GITHUB_REPO = "leaf-holder-data"
GITHUB_PATH = "holders.json"

EXPECTED_MINTS = 812
TOTAL_LO = 990_000_000
TOTAL_HI = 1_000_000_001  # allow 1 LEAF float dust


def _data_path(filename):
    return os.path.join(BASE_DIR, filename)


def fetch(url, retries=3):
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            return json.load(urllib.request.urlopen(req, timeout=30))
        except Exception:
            if i == retries - 1:
                raise
            time.sleep(5)


def fetch_history():
    """Paginate the full LEAF operation history via cursor."""
    items = []
    cursor = None
    while True:
        url = f"{API}/history?tick={TICK}&limit=100"
        if cursor:
            url += f"&cursor={cursor}"
        d = fetch(url)
        items.extend(d.get('items', []))
        cursor = d.get('next_cursor')
        if not cursor:
            break
        time.sleep(0.2)
    return items


def fetch_axbt_vaults():
    """Fetch AXBT vault operations (open/close/repay) for the activity feed.
    Display-only; does not affect LEAF balances."""
    items = []
    cursor = None
    while True:
        url = f"{API}/history?tick=AXBT&limit=100"
        if cursor:
            url += f"&cursor={cursor}"
        d = fetch(url)
        batch = d.get('items', [])
        items.extend(batch)
        cursor = d.get('next_cursor')
        if not cursor:
            break
        time.sleep(0.2)
        # only need recent vault activity; stop after 500 items
        if len(items) >= 500:
            break
    return items


_cb_cache = {}  # minute_epoch -> BTC USD close price
try:
    _pcp = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'price_cache.json')
    if os.path.exists(_pcp):
        _pcd = json.load(open(_pcp))
        for _k, _v in _pcd.items():
            _cb_cache[int(_k)] = float(_v)
        print('Loaded %d cached BTC prices' % len(_cb_cache))
except Exception as _e:
    print('Price cache load: %s' % _e)

def coinbase_1m_prices(minute_epochs):
    """Fetch Coinbase 1m BTC-USD close prices for the given minute epochs.
    Clusters nearby minutes into range fetches (300 candles max per call).
    Returns dict minute_epoch -> price. Missing minutes are absent."""
    global _cb_cache
    need = sorted(set(m for m in minute_epochs if m and m not in _cb_cache))
    if not need:
        return {m: _cb_cache[m] for m in minute_epochs if m in _cb_cache}
    # Cluster: span <= 250 min and gap <= 250 min (300 candle limit)
    clusters = []
    cs, ce = need[0], need[0]
    for m in need[1:]:
        if m - ce > 250 * 60 or m - cs > 250 * 60:
            clusters.append((cs, ce))
            cs = m
        ce = m
    clusters.append((cs, ce))
    for cs, ce in clusters:
        url = (f"https://api.exchange.coinbase.com/products/BTC-USD/candles"
               f"?start={cs - 60}&end={ce + 120}&granularity=60")
        _ok = False
        for _attempt in range(3):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "leaf/1.0"})
                d = json.load(urllib.request.urlopen(req, timeout=20))
                # d: [[time, low, high, open, close, volume], ...]
                _n = 0
                for c in d:
                    if isinstance(c, list) and len(c) >= 5:
                        _cb_cache[int(c[0])] = float(c[4])
                        _n += 1
                if _n > 0:
                    _ok = True
                    break
            except Exception as e:
                print(f"Coinbase fetch failed (attempt {_attempt+1}): {e}")
            time.sleep(1.5 * (_attempt + 1))
        if not _ok:
            print(f"Coinbase cluster {cs}-{ce} failed after retries")
        time.sleep(1.0)
    return {m: _cb_cache[m] for m in minute_epochs if m in _cb_cache}

def minute_epoch(ts_iso):
    """Floor an ISO UTC timestamp to its minute epoch."""
    try:
        dt = datetime.fromisoformat(ts_iso.replace('Z', '+00:00'))
        return int(dt.timestamp() // 60 * 60)
    except Exception:
        return 0

def fetch_pool_info():
    """Fetch AXBT/LEAF pool data + BTC price.
    Returns dict with reserves, price, positions, totals, usd valuation.
    Non-fatal: returns {} on failure."""
    try:
        pool = fetch(f"{API}/pools/AXBT-LEAF")
        res = pool.get('reserves') or {}
        axbt_btc = atoms(res.get('AXBT')) / 100000000
        leaf_amt = atoms(res.get('LEAF')) / 100000000
        price = pool.get('price') or {}
        leaf_btc = float(price.get('axbt_per_leaf') or 0)
        pool_btc = axbt_btc + leaf_amt * leaf_btc
        btc_usd = 0
        try:
            cg = fetch("https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd")
            btc_usd = float(((cg.get('bitcoin') or {}).get('usd') or 0))
        except Exception:
            pass
        if not btc_usd:
            try:
                mp = fetch("https://mempool.space/api/v1/prices")
                btc_usd = float(mp.get('USD') or 0)
            except Exception:
                pass
        # positions (compact)
        positions = []
        for ps in (pool.get('positions') or [])[:50]:
            dep = ps.get('deposited') or {}
            clm = ps.get('claim') or {}
            positions.append([
                ps.get('owner', ''),
                round(float(ps.get('share') or 0), 2),
                round(atoms(dep.get('LEAF')) / 100000000, 2),
                round(atoms(dep.get('AXBT')) / 100000000, 8),
                round(atoms(clm.get('LEAF')) / 100000000, 2),
                round(atoms(clm.get('AXBT')) / 100000000, 8),
                ps.get('expiry_height') or 0,
            ])
        totals = pool.get('totals') or {}
        fees = totals.get('fees') or {}
        return {
            'axbt_btc': round(axbt_btc, 8),
            'leaf_amt': round(leaf_amt, 2),
            'leaf_sat': round(leaf_btc * 100000000, 3),
            'pool_btc': round(pool_btc, 8),
            'btc_usd': btc_usd,
            'pool_usd': round(pool_btc * btc_usd, 2) if pool_btc and btc_usd else 0,
            'positions': positions,
            'n_positions': pool.get('active_positions') or len(positions),
            'swaps': totals.get('swaps') or 0,
            'fees_btc': round((atoms(fees.get('AXBT')) + atoms(fees.get('LEAF')) * leaf_btc) / 100000000, 8),
        }
    except Exception as e:
        print(f"Pool info fetch failed (non-fatal): {e}")
        return {}


def atoms(v):
    try:
        return int(str(v))
    except (TypeError, ValueError):
        return 0


def main():
    items = fetch_history()
    print(f"Fetched {len(items)} history items from crc20.org")

    # Normalize to internal records
    records = []
    for it in items:
        t = it.get('type')
        tmpl = it.get('template')
        txid = it.get('txid')
        bh = it.get('block_height')
        ts = it.get('block_time', '')
        frm = it.get('from') or ''
        to = it.get('to') or ''
        is_trade = 1 if tmpl == 'market' else 0
        if t == 'mint':
            amt = atoms(it.get('amount')) / ATOMS_PER_LEAF
            if to:
                records.append({'txid': txid, 'op': 'mint', 'frm': '', 'to': to,
                                'amt': amt, 'block': bh, 'ts': ts, 'is_trade': 0})
        elif t == 'transfer':
            amt = atoms(it.get('amount')) / ATOMS_PER_LEAF
            _rec = {'txid': txid, 'op': 'transfer', 'frm': frm, 'to': to,
                    'amt': amt, 'block': bh, 'ts': ts, 'is_trade': is_trade}
            if is_trade:
                _mk = it.get('market') or {}
                _paid = atoms(_mk.get('buyer_paid_sat')) / 100000000
                if _paid > 0:
                    _rec['btc'] = _paid
            records.append(_rec)
        elif t == 'swap':
            sw = it.get('swap') or {}
            side = sw.get('side')
            if side == 'buy' and sw.get('output_tick') == 'LEAF' and to:
                amt = atoms(sw.get('output_atoms')) / ATOMS_PER_LEAF
                btc = atoms(sw.get('input_atoms')) / 100000000
                records.append({'txid': txid, 'op': 'swap', 'frm': POOL_ADDR, 'to': to,
                                'amt': amt, 'block': bh, 'ts': ts, 'is_trade': 0,
                                'btc': btc})
            elif side == 'sell' and sw.get('input_tick') == 'LEAF' and frm:
                amt = atoms(sw.get('input_atoms')) / ATOMS_PER_LEAF
                btc = atoms(sw.get('output_atoms')) / 100000000
                records.append({'txid': txid, 'op': 'swap', 'frm': frm, 'to': POOL_ADDR,
                                'amt': amt, 'block': bh, 'ts': ts, 'is_trade': 0,
                                'btc': btc})
        elif t == 'liquidity':
            liq = it.get('liquidity') or {}
            action = liq.get('action')
            dep = liq.get('deposit') or {}
            leaf_amt = atoms(dep.get('LEAF')) / ATOMS_PER_LEAF
            btc_dep = 0
            for _tok, _v in dep.items():
                if _tok != 'LEAF':
                    btc_dep = atoms(_v) / 100000000
                    break
            if action == 'deposit' and frm and leaf_amt > 0:
                records.append({'txid': txid, 'op': 'liquidity', 'frm': frm, 'to': POOL_ADDR,
                                'amt': leaf_amt, 'block': bh, 'ts': ts, 'is_trade': 0,
                                'btc': btc_dep})
            elif action == 'payback' and leaf_amt > 0:
                addr = to or frm
                if addr:
                    records.append({'txid': txid, 'op': 'liquidity', 'frm': POOL_ADDR, 'to': addr,
                                    'amt': leaf_amt, 'block': bh, 'ts': ts, 'is_trade': 0})
        # vault_* ops are AXBT-side; ignored for LEAF balances

    print(f"Normalized {len(records)} LEAF records")

    # Compute balances
    # Fetch liquidity paybacks from AXBT history (missing from LEAF history upstream)
    try:
        for _it in fetch_axbt_vaults():
            if _it.get('type') == 'liquidity':
                _liq = _it.get('liquidity') or {}
                if _liq.get('action') == 'payback':
                    _credited = _liq.get('credited') or {}
                    _leaf_pb = atoms(_credited.get('LEAF')) / 100000000
                    _pb_to = _it.get('to')
                    if _leaf_pb > 0 and _pb_to:
                        records.append({'txid': _it.get('txid'), 'op': 'liquidity',
                                        'frm': POOL_ADDR, 'to': _pb_to, 'amt': _leaf_pb,
                                        'block': _it.get('block_height'),
                                        'ts': _it.get('block_time', ''), 'is_trade': 0,
                                        'btc': atoms(_credited.get('AXBT')) / 100000000,
                                        'is_payback': 1})
        _pb_cnt = sum(1 for _r in records if _r.get('is_payback'))
        print(f"Added {_pb_cnt} liquidity paybacks from AXBT history")
    except Exception as _e:
        print(f"Payback fetch failed: {_e}")

    balances = defaultdict(float)
    mint_count = defaultdict(int)
    tx_count = defaultdict(int)
    for r in records:
        op, amt = r['op'], r['amt']
        if amt <= 0:
            continue
        if op == 'mint':
            balances[r['to']] += amt
            mint_count[r['to']] += 1
            tx_count[r['to']] += 1
        elif op in ('transfer', 'swap', 'liquidity'):
            if r['frm']:
                balances[r['frm']] -= amt
                tx_count[r['frm']] += 1
            if r['to']:
                balances[r['to']] += amt
                tx_count[r['to']] += 1

    # Load staking status (static input file in repo)
    try:
        with open(_data_path('staking_status.json')) as f:
            staking_map = json.load(f)
        print(f"Loaded {len(staking_map)} staking records")
    except Exception as e:
        print(f"STAKING LOAD FAILED: {e}")
        staking_map = {}
    # 0=未知, 1=质押中, 2=已解锁, 3=部分解锁
    staking_code = {'未知': 0, '质押中': 1, '已解锁': 2, '部分解锁': 3}

    try:
        with open(_data_path('partial_unlock_details.json')) as f:
            partial_details = json.load(f)
    except Exception:
        partial_details = {}

    # Build per-address tx lists
    # Format: [txid_short, op, amount, block, direction, is_trade, timestamp, counterparty]
    addr_txs = defaultdict(list)
    for r in sorted(records, key=lambda x: x['block'] or 0, reverse=True):
        txid, op, amt, bh, ts = r['txid'], r['op'], r['amt'], r['block'], r['ts']
        txid_short = txid[:16] if txid else ''
        frm, to, is_trade = r['frm'], r['to'], r['is_trade']
        if op == 'mint':
            if to:
                addr_txs[to].append([txid_short, 'mint', amt, bh, 'mint', 0, ts, ''])
        elif op == 'transfer':
            if frm:
                addr_txs[frm].append([txid_short, 'transfer', amt, bh, 'out', is_trade, ts, to])
            if to:
                addr_txs[to].append([txid_short, 'transfer', amt, bh, 'in', is_trade, ts, frm])
        elif op == 'swap':
            if frm:
                addr_txs[frm].append([txid_short, 'swap', amt, bh, 'out', 0, ts, to])
            if to:
                addr_txs[to].append([txid_short, 'swap', amt, bh, 'in', 0, ts, frm])
        elif op == 'liquidity':
            if frm:
                addr_txs[frm].append([txid_short, 'liquidity', amt, bh, 'out', 0, ts, to])
            if to:
                addr_txs[to].append([txid_short, 'liquidity', amt, bh, 'in', 0, ts, frm])

    holders = []
    for addr, bal in balances.items():
        if bal <= 0.01:
            continue
        code = staking_code.get(staking_map.get(addr, '未知'), 0)
        extra = None
        if code == 3 and addr in partial_details:
            d = partial_details[addr]
            extra = [d.get('unlocked', 0), d.get('locked', 0)]
        holders.append(
            [addr, round(bal, 2), mint_count[addr], tx_count[addr], code] +
            ([extra] if extra else [])
        )
    holders.sort(key=lambda x: -x[1])
    total = sum(h[1] for h in holders)

    # Staking sets for unlock detection
    staking_addrs = {addr for addr, st in staking_map.items() if st == '质押中'}
    minters = set()
    for r in records:
        if r['op'] == 'mint' and r['to']:
            minters.add(r['to'])
    first_out = {}
    for r in sorted(records, key=lambda x: x['block'] or 0):
        if r['op'] == 'transfer' and r['frm'] and r['frm'] not in first_out:
            first_out[r['frm']] = r['txid']

    # 7-day holding change (excludes unlock events: first transfer out from a minter)
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=7)
    change_7d = defaultdict(float)
    for r in records:
        ts_str = r['ts']
        if not ts_str:
            continue
        try:
            ts = datetime.fromisoformat(ts_str.replace('Z', '+00:00'))
        except ValueError:
            continue
        if ts < cutoff:
            continue
        op, amt = r['op'], r['amt']
        frm, to, txid = r['frm'], r['to'], r['txid']
        if op == 'mint':
            if to:
                change_7d[to] += amt
        else:
            is_unlock = (op == 'transfer' and frm in minters and first_out.get(frm) == txid)
            if is_unlock:
                continue
            if frm:
                change_7d[frm] -= amt
            if to:
                change_7d[to] += amt
    holders = [h + [round(change_7d.get(h[0], 0), 2)] for h in holders]

    # Latest activity, newest first
    # Format: [txid, op, address, amount, block, is_trade, timestamp, from_addr, to_addr, is_unlock]
    # USD at confirmation minute for swap/liquidity (via BTC side)
    _usd_minutes = {}
    for r in records:
        if r.get('btc', 0) > 0 and r.get('ts'):
            _usd_minutes[r['txid']] = minute_epoch(r['ts'])
    if _usd_minutes:
        _prices = coinbase_1m_prices(list(_usd_minutes.values()))
        for r in records:
            _m = _usd_minutes.get(r['txid'])
            if _m and _m in _prices and r.get('btc', 0) > 0:
                r['usd'] = round(r['btc'] * _prices[_m], 2)
        print(f"USD priced {sum(1 for r in records if r.get('usd'))} swap/liquidity records")
    latest_compact = []
    for r in sorted(records, key=lambda x: x['block'] or 0, reverse=True):
        frm, to = r['frm'], r['to']
        is_unlock = 1 if (r['op'] == 'transfer' and frm in minters
                          and first_out.get(frm) == r['txid']) else 0
        latest_compact.append(
            [r['txid'], r['op'], to or frm, r['amt'], r['block'],
             r['is_trade'], r['ts'], frm, to, is_unlock, r.get('usd')]
        )

    # Merge AXBT vault operations into activity feed (display only)
    # and build detailed vault list for the vault tab
    vaults_detail = []
    vault_open_count = 0
    vault_locked_btc = 0
    try:
        for it in fetch_axbt_vaults():
            t = it.get('type')
            if t not in ('vault_open', 'vault_close', 'vault_repay'):
                continue
            v = it.get('vault') or {}
            owner = v.get('owner') or it.get('from') or ''
            collateral = atoms(v.get('collateral_sats')) / 100000000
            debt = atoms(v.get('debt') or it.get('amount')) / 100000000
            if t == 'vault_open':
                amt = collateral
            elif t == 'vault_close':
                amt = collateral or atoms(it.get('amount')) / 100000000
            else:  # vault_repay
                amt = atoms(it.get('amount')) / 100000000
            if amt <= 0:
                continue
            latest_compact.append(
                [it.get('txid'), t, owner, round(amt, 8), it.get('block_height'),
                 0, it.get('block_time', ''), owner, '', 0]
            )
            vaults_detail.append(
                [it.get('txid'), t, owner, round(collateral, 8), round(debt, 8),
                 v.get('status', ''), it.get('block_height'), it.get('block_time', ''),
                 v.get('funding_txid') or '']
            )
        # USD at confirmation minute for vault ops (amount is BTC)
        _v_minutes = {}
        for _e in latest_compact:
            if isinstance(_e[1], str) and _e[1].startswith('vault_') and _e[6] and _e[3]:
                _v_minutes[_e[0]] = minute_epoch(_e[6])
        if _v_minutes:
            _vp = coinbase_1m_prices(list(_v_minutes.values()))
            for _e in latest_compact:
                _m = _v_minutes.get(_e[0])
                if _m and _m in _vp:
                    _e.append(round(_e[3] * _vp[_m], 2))
                else:
                    _e.append(None)
            print(f"USD priced {sum(1 for _e in latest_compact if len(_e) > 10 and _e[10])} vault ops")
        # ensure all entries have 11 elements
        for _e in latest_compact:
            while len(_e) < 11:
                _e.append(None)
        latest_compact.sort(key=lambda x: x[4] or 0, reverse=True)
        vaults_detail.sort(key=lambda x: x[6] or 0, reverse=True)
        # Correct open-vault accounting: match closes to opens by funding_txid
        # Process oldest-first so closes remove their opens
        _open_vaults = {}
        for _vd in reversed(vaults_detail):
            _fid = _vd[8] or _vd[0]
            if _vd[1] == 'vault_open':
                _open_vaults[_fid] = _vd[3]
            elif _vd[1] == 'vault_close':
                _open_vaults.pop(_fid, None)
        vault_open_count = len(_open_vaults)
        vault_locked_btc = round(sum(_open_vaults.values()), 8)
        print(f"Merged {len(vaults_detail)} AXBT vault ops; open: {vault_open_count}, locked: {vault_locked_btc} BTC")
    except Exception as e:
        print(f"AXBT vault merge failed (non-fatal): {e}")

    # Moved vs unmoved
    transfer_addrs = set()
    for r in records:
        if r['op'] == 'transfer':
            if r['frm']:
                transfer_addrs.add(r['frm'])
            if r['to']:
                transfer_addrs.add(r['to'])
    moved_bal = sum(h[1] for h in holders if h[0] in transfer_addrs)
    unmoved_bal = total - moved_bal
    moved_cnt = sum(1 for h in holders if h[0] in transfer_addrs)
    unmoved_cnt = len(holders) - moved_cnt
    top3_pct = sum(h[1] for h in holders[:3]) / total * 100 if total else 0
    top10_pct = sum(h[1] for h in holders[:10]) / total * 100 if total else 0
    mint_cnt = sum(1 for r in records if r['op'] == 'mint')
    transfer_cnt = sum(1 for r in records if r['op'] == 'transfer')
    swap_cnt = sum(1 for r in records if r['op'] == 'swap')
    liq_cnt = sum(1 for r in records if r['op'] == 'liquidity')

    pool_info = fetch_pool_info()
    print(f"Pool: {pool_info.get('pool_btc')} BTC = ${pool_info.get('pool_usd')}")

    now_iso = datetime.now(timezone.utc).isoformat()
    snapshot = {
        'u': now_iso,
        'bh': max((r['block'] or 0 for r in records), default=0),
        'tr': len(records),
        'th': len(holders),
        'ts': round(total, 2),
        'h': holders,
        'la': latest_compact,
        'vaults': vaults_detail,
        'vault_open_count': vault_open_count,
        'vault_locked_btc': vault_locked_btc,
        'pool_info': pool_info,
        'txs': {addr: txs for addr, txs in addr_txs.items() if txs},
        'stats': {
            'moved_bal': round(moved_bal, 2),
            'unmoved_bal': round(unmoved_bal, 2),
            'moved_cnt': moved_cnt,
            'unmoved_cnt': unmoved_cnt,
            'top3_pct': round(top3_pct, 2),
            'top10_pct': round(top10_pct, 2),
            'mint_cnt': mint_cnt,
            'transfer_cnt': transfer_cnt,
            'swap_cnt': swap_cnt,
            'liquidity_cnt': liq_cnt,
        },
    }

    with open(OUTPUT, 'w') as f:
        json.dump(snapshot, f)
    print(f"[{now_iso}] Saved {len(holders)} holders, total {total:,.2f}")

    # Save activity history (fresh rebuild each run)
    try:
        with open(_data_path('activity_history.json'), 'w') as f:
            json.dump({'records': records, 'updated': now_iso}, f)
    except Exception as e:
        print(f"Activity history save failed: {e}")

    # VALIDATION: protect against bad data
    # - mint count must be exactly 812 (fully covered by the new indexer)
    # - holder total must be within [990M, 1B]; the 7 liquidity payback ops are
    #   currently missing from the upstream history listing, so the computed
    #   total can sit slightly below 1B.
    #   NOTE: compare on round(total, 2) — raw float sums can exceed 1B by
    #   ~1e-7 of pure floating-point dust (seen live: 1000000000.0000005).
    total_r = round(total, 2)
    if mint_cnt != EXPECTED_MINTS:
        print(f"VALIDATION FAILED: mint count {mint_cnt} != {EXPECTED_MINTS}, NOT updating holders.json")
        return
    if not (TOTAL_LO <= total_r <= TOTAL_HI):
        print(f"VALIDATION FAILED: total {total_r:,.2f} outside [{TOTAL_LO:,}, {TOTAL_HI:,}], NOT updating holders.json")
        return
    print("Validation passed")

    if IN_ACTIONS:
        if HOLDERS_OUTPUT:
            with open(HOLDERS_OUTPUT, 'w') as f:
                json.dump(snapshot, f, separators=(',', ':'))
            print(f"Wrote {HOLDERS_OUTPUT}")
        # Persist price cache for next run
        try:
            _pcp2 = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'price_cache.json')
            with open(_pcp2, 'w') as f:
                json.dump({str(k): v for k, v in _cb_cache.items()}, f)
            print(f"Saved {len(_cb_cache)} BTC prices to cache")
        except Exception as _e:
            print(f"Price cache save: {_e}")
    else:
        push_to_github(snapshot, now_iso)


def push_to_github(snapshot, now):
    """Push snapshot to GitHub repo via REST API using PAT (local runs only)."""
    try:
        token_file = "/home/hatch/.config/leaf/github_token"
        with open(token_file) as f:
            token = f.read().strip()
        if not token:
            print("No GitHub token, skipping push")
            return
        raw_json = json.dumps(snapshot, separators=(',', ':'))
        content_b64 = base64.b64encode(raw_json.encode()).decode()
        req = urllib.request.Request(
            f"https://api.github.com/repos/{GITHUB_REPO_OWNER}/{GITHUB_REPO}/contents/{GITHUB_PATH}?ref=main",
            headers={"Authorization": f"token {token}", "User-Agent": "leaf-snapshot/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                sha = json.load(r)["sha"]
        except Exception:
            sha = None
        data = {"message": f"Update snapshot {now}", "content": content_b64, "branch": "main"}
        if sha:
            data["sha"] = sha
        req = urllib.request.Request(
            f"https://api.github.com/repos/{GITHUB_REPO_OWNER}/{GITHUB_REPO}/contents/{GITHUB_PATH}",
            data=json.dumps(data).encode(),
            headers={"Authorization": f"token {token}", "User-Agent": "leaf-snapshot/1.0",
                      "Content-Type": "application/json"},
            method="PUT")
        with urllib.request.urlopen(req, timeout=60) as r:
            result = json.load(r)
        print(f"GitHub push OK: {result['commit']['sha'][:8]}")
    except Exception as e:
        print(f"GitHub push error: {e}")


if __name__ == '__main__':
    main()
