"""SQLite layer: schema, upserts, change log. Stdlib only."""
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / 'data' / 'radar.db'


def now_iso():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def connect(path=None):
    p = Path(path) if path else DB_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    cx = sqlite3.connect(str(p), timeout=30)
    cx.row_factory = sqlite3.Row
    return cx


def init_db(path=None):
    cx = connect(path)
    cx.executescript((ROOT / 'schema.sql').read_text(encoding='utf-8'))
    cols = {r[1] for r in cx.execute('PRAGMA table_info(programs)').fetchall()}
    for col in ('end_date TEXT', 'seed INTEGER DEFAULT 0', 'logo_url TEXT',
                'focus TEXT DEFAULT ' + "''", 'blurb TEXT DEFAULT ' + "''",
                'last_deep TEXT DEFAULT ' + "''",
                'upstream_listed TEXT DEFAULT ' + "''",
                'upstream_updated TEXT DEFAULT ' + "''"):
        name = col.split()[0]
        if name not in cols:
            cx.execute('ALTER TABLE programs ADD COLUMN %s' % col)
    scols = {r[1] for r in cx.execute('PRAGMA table_info(scans)').fetchall()}
    if scols and 'mode' not in scols:
        cx.execute("ALTER TABLE scans ADD COLUMN mode TEXT DEFAULT 'light'")
    if scols and 'started' not in scols:
        cx.execute("ALTER TABLE scans ADD COLUMN started TEXT DEFAULT ''")
    if scols and 'findings' not in scols:
        cx.execute("ALTER TABLE scans ADD COLUMN findings INTEGER DEFAULT 0")
    if scols and 'findings_json' not in scols:
        cx.execute("ALTER TABLE scans ADD COLUMN findings_json TEXT DEFAULT ''")
    cx.commit()
    return cx


def upsert_program(cx, p, ts=None):
    """Insert or refresh a program row. Returns (row_id, is_new)."""
    ts = ts or now_iso()
    cur = cx.execute('SELECT id, first_seen FROM programs WHERE platform=? AND slug=?',
                     (p['platform'], p['slug']))
    row = cur.fetchone()
    tags = json.dumps(p.get('tags', []))
    langs = json.dumps(p.get('languages', []))
    if row is None:
        cur = cx.execute(
            '''INSERT INTO programs(platform,slug,name,url,bounty_min,bounty_max,reward_raw,
               tags,languages,state,end_date,seed,logo_url,focus,blurb,
               upstream_listed,upstream_updated,
               vdp,is_contest,report_count,first_seen,last_seen)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (p['platform'], p['slug'], p['name'], p['url'], p.get('bounty_min'),
             p.get('bounty_max'), p.get('reward_raw', ''), tags, langs,
             p.get('state', 'active'), p.get('end_date'), int(bool(p.get('seed', 0))),
             p.get('logo_url'), p.get('focus', ''), p.get('blurb', ''),
             p.get('upstream_listed', ''), p.get('upstream_updated', ''),
             int(bool(p.get('vdp', 0))),
             int(bool(p.get('is_contest', 0))), int(p.get('report_count', 0)), ts, ts))
        return cur.lastrowid, True
    cx.execute(
        '''UPDATE programs SET name=?,url=?,bounty_min=?,bounty_max=?,reward_raw=?,tags=?,
           languages=?,state=?,end_date=?,seed=?,logo_url=?,focus=?,blurb=?,
           upstream_listed=COALESCE(NULLIF(?, ''), upstream_listed),
           upstream_updated=COALESCE(NULLIF(?, ''), upstream_updated),
           vdp=?,is_contest=?,report_count=?,last_seen=?
           WHERE id=?''',
        (p['name'], p['url'], p.get('bounty_min'), p.get('bounty_max'),
         p.get('reward_raw', ''), tags, langs, p.get('state', 'active'),
         p.get('end_date'), int(bool(p.get('seed', 0))),
         p.get('logo_url'), p.get('focus', ''), p.get('blurb', ''),
         p.get('upstream_listed', ''), p.get('upstream_updated', ''),
         int(bool(p.get('vdp', 0))), int(bool(p.get('is_contest', 0))),
         int(p.get('report_count', 0)), ts, row['id']))
    return row['id'], False


def sync_targets(cx, program_id, identities, ts=None, eligible_map=None):
    """Diff target identity set. eligible_map: {identity: bool} or None (all eligible).
    Returns list of (kind, identity) events.

    An EMPTY identity set means "no data this pass" (catalog lanes never carry
    targets) — it must never clear live scope. Removals only fire when a
    source actually provided a non-empty list that shrank."""
    ts = ts or now_iso()
    if not identities:
        return []
    eligible_map = eligible_map or {}
    cur = cx.execute('SELECT identity FROM targets WHERE program_id=? AND in_scope=1', (program_id,))
    live = {r['identity'] for r in cur.fetchall()}
    new = set(identities) - live
    gone = live - set(identities)
    events = []
    for ident in sorted(new):
        ttype = guess_type(ident)
        elig = int(eligible_map.get(ident, True))
        cx.execute('''INSERT INTO targets(program_id,identity,eligible,in_scope,target_type,first_seen,last_seen)
                      VALUES(?,?,?,1,?,?,?) ON CONFLICT(program_id,identity) DO UPDATE
                      SET in_scope=1, eligible=excluded.eligible, last_seen=excluded.last_seen''',
                   (program_id, ident, elig, ttype, ts, ts))
        events.append(('target_added', ident))
    for ident in sorted(gone):
        cx.execute('UPDATE targets SET in_scope=0 WHERE program_id=? AND identity=?',
                   (program_id, ident))
        events.append(('target_removed', ident))
    cx.execute('UPDATE targets SET last_seen=? WHERE program_id=? AND in_scope=1', (ts, program_id))
    return events


def guess_type(ident):
    s = ident.strip().lower()
    if s.startswith(('http://', 'https://')) or ('.' in s and '/' not in s and ' ' not in s):
        return 'domain' if '*' not in s else 'wildcard'
    if s.startswith(('0x', 'bc1', 'tb1')) or len(s) in (40, 42, 44) and s.replace('0x', '').isalnum():
        return 'smart contract'
    if s.startswith(('github.com',)) or s.endswith(('.git',)):
        return 'source code'
    if '/' in s and '.' in s:
        return 'api'
    return 'other'


def log_change(cx, kind, program_id, detail=''):
    cx.execute('INSERT INTO changes(ts,kind,program_id,detail) VALUES(?,?,?,?)',
               (now_iso(), kind, program_id, detail))


def record_scrape(cx, platform, count, status='ok'):
    cx.execute('INSERT INTO scrapes(platform,ts,count,status) VALUES(?,?,?,?)',
               (platform, now_iso(), count, status))
    cx.commit()


def prune_history(cx, max_changes=20000, max_scrapes=2000, max_scans=500):
    """Cap append-only tables so months of refreshes don't bloat the DB.
    Keeps the newest rows. Returns dict of rows deleted."""
    out = {}
    for table, cap in (('changes', max_changes), ('scrapes', max_scrapes),
                       ('scans', max_scans)):
        n = cx.execute('SELECT COUNT(*) c FROM %s' % table).fetchone()['c']
        if n > cap:
            cx.execute('DELETE FROM %s WHERE id NOT IN '
                       '(SELECT id FROM %s ORDER BY id DESC LIMIT ?)' % (table, table),
                       (cap,))
            out[table] = n - cap
    cx.commit()
    return out
