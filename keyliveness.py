"""Key liveness verification for leaked Ethereum private keys.

The safe ladder, per finding:
  1. Derive the address OFFLINE from the raw key (pure math — nothing of the
     target's is touched, no network, no key use).
  2. Check that address's balance + transaction count on a PUBLIC RPC —
     passive reads of public chain data, never a signature, never a transfer.
  3. Verdict:
       FUNDED  — balance > 0: a live key controlling money right now.
       HISTORY — nonce > 0: has signed on-chain before (live or recycled).
       EMPTY   — zero balance and zero nonce: test fixture or expired.
       UNVERIFIED — eth-keys missing, RPC failed, or not an eth key.

API-key findings are deliberately never auto-verified: that would mean
authenticating with the credential, which is gated behind program rules.
"""
import json
import os
import urllib.request

DEFAULT_RPC = 'https://eth.llamarpc.com'
FALLBACK_RPC = 'https://ethereum-rpc.publicnode.com'


def derive_address(raw):
    """Checksum address from a raw eth private key (offline, pure math).
    Accepts '0x'-prefixed or bare 64-hex strings; None on any failure or if
    eth-keys is unavailable."""
    s = (raw or '').strip().strip('"').strip("'")
    if s.startswith('0x') or s.startswith('0X'):
        s = s[2:]
    if len(s) != 64:
        return None
    try:
        from eth_keys import keys
        pk = keys.PrivateKey(bytes.fromhex(s))
        return pk.public_key.to_checksum_address()
    except Exception:
        return None


def _rpc_post(rpc, payload, timeout=20):
    req = urllib.request.Request(rpc, method='POST',
                                 data=json.dumps(payload).encode(),
                                 headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode('utf-8', 'ignore'))


def check_addresses(addresses, rpc=None, post_fn=None):
    """{address: {'balance_wei': int, 'nonce': int, 'verdict': str}} for the
    given checksum addresses. Batched JSON-RPC (2 calls per <=50 addresses).
    post_fn injectable for tests. All-RPC-failure -> UNVERIFIED for every
    address (never guesses)."""
    addr_list = [a for a in dict.fromkeys(addresses) if a]
    if not addr_list:
        return {}
    endpoints = [rpc or os.environ.get('CRV_ETH_RPC') or DEFAULT_RPC, FALLBACK_RPC]
    post = post_fn or _rpc_post
    for ep in endpoints:
        payload = []
        for i, a in enumerate(addr_list):
            payload.append({'jsonrpc': '2.0', 'id': i * 2,
                            'method': 'eth_getBalance',
                            'params': [a, 'latest']})
            payload.append({'jsonrpc': '2.0', 'id': i * 2 + 1,
                            'method': 'eth_getTransactionCount',
                            'params': [a, 'latest']})
        try:
            resp = post(ep, payload)
        except Exception:
            continue
        by_id = {}
        for item in resp if isinstance(resp, list) else []:
            if isinstance(item, dict) and 'id' in item:
                by_id[item['id']] = item
        if not by_id:
            continue
        out = {}
        for i, a in enumerate(addr_list):
            bal_r = by_id.get(i * 2, {})
            nonce_r = by_id.get(i * 2 + 1, {})
            try:
                bal = int(str(bal_r.get('result') or '0'), 16)
            except (TypeError, ValueError):
                bal = -1
            try:
                nonce = int(str(nonce_r.get('result') or '0'), 16)
            except (TypeError, ValueError):
                nonce = -1
            if bal < 0 or nonce < 0:
                out[a] = {'balance_wei': None, 'nonce': None,
                          'verdict': 'UNVERIFIED'}
            elif bal > 0:
                out[a] = {'balance_wei': bal, 'nonce': nonce, 'verdict': 'FUNDED'}
            elif nonce > 0:
                out[a] = {'balance_wei': 0, 'nonce': nonce, 'verdict': 'HISTORY'}
            else:
                out[a] = {'balance_wei': 0, 'nonce': 0, 'verdict': 'EMPTY'}
        return out
    return {a: {'balance_wei': None, 'nonce': None, 'verdict': 'UNVERIFIED'}
            for a in addr_list}


def check_scan(db_path, scan_id, rpc=None, post_fn=None):
    """Derive addresses for every eth-private-key row of one scan, run the
    on-chain checks, and write verdicts back into the stored findings JSON.
    Returns {address: verdict-dict} (empty when nothing checkable)."""
    import db as _db
    cx = _db.connect(db_path)
    try:
        r = cx.execute('SELECT findings_json FROM scans WHERE id=?',
                       (scan_id,)).fetchone()
    finally:
        cx.close()
    if not r or not r['findings_json']:
        return {}
    try:
        rows = json.loads(r['findings_json'])
    except Exception:
        return {}
    if not isinstance(rows, list):
        return {}
    addr_of = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        raw = row.get('raw')
        addr = row.get('address') or derive_address(raw)
        if addr and row.get('detector', '').lower().find('eth') >= 0:
            row['address'] = addr
            addr_of[addr] = row
    if not addr_of:
        _persist(db_path, scan_id, rows)
        return {}
    verdicts = check_addresses(list(addr_of.keys()), rpc=rpc, post_fn=post_fn)
    for addr, v in verdicts.items():
        row = addr_of[addr]
        row['verdict'] = v['verdict']
        row['balance_wei'] = v['balance_wei']
        row['nonce'] = v['nonce']
    _persist(db_path, scan_id, rows)
    return verdicts


def _persist(db_path, scan_id, rows):
    import db as _db
    try:
        cx = _db.connect(db_path)
        try:
            cx.execute('UPDATE scans SET findings_json=? WHERE id=?',
                       (json.dumps(rows), scan_id))
            cx.commit()
        finally:
            cx.close()
    except Exception:
        pass
