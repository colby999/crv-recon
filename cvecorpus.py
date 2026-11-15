"""CVE corpus layer: instant local lookup from the operator's corpus vault
(bug_corpus FTS over 360k GHSA + 226k NVD rows) plus best-effort corpus
refresh (git pull on the cvelistV5 / advisory-database checkouts).

Read-only on the vault — the radar never writes the operator's corpus DB.
Everything is optional: with no vault on disk this module degrades to no-ops
and the program page simply shows scanner-driven CVE rows only.
"""
import json
import os
import re
import sqlite3
import subprocess
import unicodedata
from pathlib import Path

HERE = Path(__file__).resolve().parent

_VAULT_DB_CANDIDATES = (
    r'..\corpus-vault\vault.db',
    r'..\..\corpus-vault\vault.db',
    r'corpus-vault\vault.db',
)

# name -> subfolder inside the vault (refresh targets, completeness counters)
CORPUS_CHECKOUTS = {
    'cvelistV5': 'cvelistV5',
    'advisory-database': 'advisory-database',
    'exploitdb': 'exploitdb',
}


def vault_db_path(env_key='CVE_VAULT_DB'):
    """First existing corpus vault DB from the candidate list (env override)."""
    p = os.environ.get(env_key, '')
    if p and os.path.isfile(p):
        return p
    for c in _VAULT_DB_CANDIDATES:
        p = c if os.path.isabs(c) else os.path.normpath(os.path.join(HERE, c))
        if os.path.isfile(p):
            return p
    return None


def corpus_dirs():
    """Existing corpus checkout dirs keyed by name: {name: path|None}."""
    vault = Path(vault_db_path()).parent if vault_db_path() else None
    out = {}
    for name, sub in CORPUS_CHECKOUTS.items():
        cands = []
        if vault:
            cands.append(vault / sub)
        cands.append(HERE.parent / 'corpus-vault' / sub)
        cands.append(os.path.expanduser(os.path.join('~', 'corpus-vault', sub)))
        hit = None
        for c in cands:
            if os.path.isdir(c):
                hit = str(c)
                break
        out[name] = hit
    return out


def v5_db_path(env_key='CVE_V5_DB'):
    """Local searchable cvelistV5 index (built by seed_cvelist_v5). Radar-local
    data file; env override for alternate layouts."""
    p = os.environ.get(env_key, '')
    if p and os.path.isfile(p):
        return p
    p = os.path.join(str(HERE), 'data', 'cve_v5.db')
    return p if os.path.isfile(p) else None


def seed_cvelist_v5(db_path=None, src_root=None, limit=None):
    """Build/refresh the local cvelistV5 FTS index from the vault checkout.
    One row per CVE record: id, state, description excerpt, vendor/product,
    published. Idempotent (INSERT OR IGNORE on cve_id). Returns row count.
    425k records parse in minutes; run it in the background on first build."""
    db = db_path or os.path.join(str(HERE), 'data', 'cve_v5.db')
    if not src_root:
        dirs = corpus_dirs()
        src_root = dirs.get('cvelistV5')
    if not src_root or not os.path.isdir(src_root):
        return None
    os.makedirs(os.path.dirname(db), exist_ok=True)
    cx = sqlite3.connect(db)
    try:
        cx.executescript(
            'PRAGMA journal_mode=WAL;'
            'CREATE TABLE IF NOT EXISTS v5_cves('
            ' cve_id TEXT PRIMARY KEY, state TEXT, summary TEXT,'
            ' vendor TEXT, product TEXT, published TEXT);'
            "CREATE VIRTUAL TABLE IF NOT EXISTS v5_cves_fts USING fts5("
            ' summary, vendor, product, cve_id,'
            " content='v5_cves', content_rowid='rowid');")
        cves_root = os.path.join(src_root, 'cves')
        if not os.path.isdir(cves_root):
            return None
        seen = 0
        inserted = 0
        batch = []
        for year in sorted(os.listdir(cves_root)):
            yd = os.path.join(cves_root, year)
            if not os.path.isdir(yd):
                continue
            for chunk in sorted(os.listdir(yd)):
                cd = os.path.join(yd, chunk)
                if not os.path.isdir(cd):
                    continue
                for fn in sorted(os.listdir(cd)):
                    if not (fn.startswith('CVE-') and fn.endswith('.json')):
                        continue
                    if limit is not None and seen >= limit:
                        cx.commit()
                        n = cx.execute('SELECT COUNT(*) FROM v5_cves').fetchone()[0]
                        return n
                    try:
                        d = json.load(open(os.path.join(cd, fn), encoding='utf-8'))
                    except Exception:
                        continue
                    meta = d.get('cveMetadata') or {}
                    cve_id = meta.get('cveId')
                    if not cve_id:
                        continue
                    seen += 1
                    cont = d.get('containers') or {}
                    cna = cont.get('cna') or {}
                    desc = ''
                    for dv in (cna.get('descriptions') or []):
                        if dv.get('value'):
                            desc = dv['value']
                            break
                    vendor = product = ''
                    aff = (cna.get('affected') or [])
                    if aff:
                        vendor = (aff[0].get('vendor') or '')[:120]
                        product = (aff[0].get('product') or '')[:120]
                    if not vendor:
                        for adp in (cont.get('adp') or []):
                            a2 = (adp.get('affected') or [])
                            if a2 and a2[0].get('vendor'):
                                vendor = (a2[0].get('vendor') or '')[:120]
                                product = (a2[0].get('product') or '')[:120]
                                break
                    batch.append((cve_id, meta.get('state') or '',
                                  desc[:300], vendor, product,
                                  (meta.get('datePublished') or '')[:10]))
                    if len(batch) >= 2000:
                        inserted += cx.executemany(
                            'INSERT OR IGNORE INTO v5_cves VALUES(?,?,?,?,?,?)',
                            batch).rowcount
                        batch = []
                        cx.commit()
        if batch:
            inserted += cx.executemany(
                'INSERT OR IGNORE INTO v5_cves VALUES(?,?,?,?,?,?)', batch).rowcount
        cx.commit()
        cx.execute("INSERT INTO v5_cves_fts(v5_cves_fts) VALUES('rebuild')")
        cx.commit()
        n = cx.execute('SELECT COUNT(*) FROM v5_cves').fetchone()[0]
        return n
    finally:
        cx.close()


# Brand-name aliases: single-letter or renamed vendors whose raw token is
# noisy/missing in advisory text. token(lowercase) -> [phrase variants].
VENDOR_ALIASES = {
    'x': ['twitter', 'xai'],
    'xai': ['x ai'],
    'x ai': ['xai'],
}


def name_variants(name):
    """FTS phrase variants for a program name: the exact phrase first (when
    discriminating — at least 5 chars or multi-token), then the longest
    meaningful token(s) plus brand aliases. Single-letter tokens are REPLACED
    by their aliases ('X' -> twitter/xai, not 44k of letter-x noise); precise
    short tokens keep raw + aliases ('xAI' -> xai, 'x ai'). Unicode names are
    NFKC-normalized."""
    n = unicodedata.normalize('NFKC', name or '').replace('"', ' ').strip()
    out = []
    seen = set()
    if n and (len(n) >= 5 or ' ' in n):
        out.append(n)
        seen.add(n.lower())
    tokens = [t for t in re.split(r'[^A-Za-z0-9]+', n) if t]
    for t in sorted(set(tokens), key=len, reverse=True)[:1]:
        aliases = VENDOR_ALIASES.get(t.lower(), [])
        phrases = ([t] if len(t) >= 2 else []) + aliases
        for phrase in phrases:
            if phrase.lower() not in seen:
                seen.add(phrase.lower())
                out.append(phrase)
    return out


def _v5_rows(name, db=None):
    """CVE-v5 rows matching any name variant (summary/vendor/product FTS)."""
    db = db or v5_db_path()
    if not db or not (name or '').strip():
        return []
    sql = ('''SELECT c.cve_id, c.state, c.summary, c.vendor, c.product, c.published
              FROM v5_cves c JOIN v5_cves_fts f ON c.rowid = f.rowid
              WHERE v5_cves_fts MATCH ?
              ORDER BY c.published DESC''')
    try:
        cx = sqlite3.connect('file:%s?mode=ro' % db.replace('\\', '/'), uri=True)
        try:
            out = []
            seen = set()
            for variant in name_variants(name):
                for cve_id, _state, summary, _vendor, _product, published in \
                        cx.execute(sql, ('"' + variant + '"',)).fetchall():
                    if cve_id in seen:
                        continue
                    seen.add(cve_id)
                    out.append({
                        'cve_id': cve_id,
                        'cve_ids': [cve_id],
                        'ghsa_id': '',
                        'url': 'https://nvd.nist.gov/vuln/detail/%s' % cve_id,
                        'summary': (summary or '').strip()[:300],
                        'source': 'cve-v5',
                        'published': published or '',
                    })
        finally:
            cx.close()
    except sqlite3.Error:
        return []
    return out


def corpus_cves_page(name, page=1, per_page=25, db_path=None):
    """One page of CVE rows for a program name across BOTH local corpora —
    the vault FTS (ghsa/nvd) and the seeded cvelistV5 index when present —
    merged newest first. Name variants (exact phrase + longest token) drive
    both layers so 'Cloudflare Public Bug Bounty' hits on 'cloudflare'."""
    db = db_path or vault_db_path()
    if not db or not (name or '').strip():
        return {'total': 0, 'page': 1, 'per_page': per_page, 'pages': 0, 'rows': []}
    page = max(1, int(page))
    per_page = max(1, min(int(per_page), 100))
    rows = _corpus_rows_all(name, db) + _v5_rows(name)
    rows.sort(key=lambda r: r['published'] or '', reverse=True)
    total = len(rows)
    rows = rows[(page - 1) * per_page: page * per_page]
    return {'total': total, 'page': page, 'per_page': per_page,
            'pages': (total + per_page - 1) // per_page, 'rows': rows}


def _corpus_rows_all(name, db):
    """All vault rows matching any name variant (light columns only), deduped
    by GHSA/source id, newest first."""
    sql = ('''SELECT b.source, b.source_id, b.title, b.cve_ids, b.disclosed_at
              FROM bug_corpus b JOIN bug_corpus_fts f ON b.rowid = f.rowid
              WHERE bug_corpus_fts MATCH ? AND b.source IN ('ghsa', 'nvd')
                AND b.cve_ids != ''
              ORDER BY b.disclosed_at DESC''')
    try:
        cx = sqlite3.connect('file:%s?mode=ro' % db.replace('\\', '/'), uri=True)
        try:
            out = []
            seen = set()
            for variant in name_variants(name):
                for row in cx.execute(sql, ('"' + variant + '"',)).fetchall():
                    key = row[1] or row[3]  # source_id, else cve_ids
                    if key in seen:
                        continue
                    seen.add(key)
                    rec = _corpus_row(row)
                    if rec:
                        out.append(rec)
        finally:
            cx.close()
    except sqlite3.Error:
        return []
    return out


def _corpus_row(row):
    src, sid, title, cve_ids, disclosed = row
    cves = [c.strip().upper() for c in str(cve_ids or '').replace(',', ' ').split()
            if c.strip().upper().startswith('CVE-')]
    if not cves:
        return None
    cve0 = cves[0]
    if src == 'ghsa' and str(sid).startswith('GHSA-'):
        url = 'https://github.com/advisories/%s' % sid
        label = 'corpus-ghsa'
    else:
        url = 'https://nvd.nist.gov/vuln/detail/%s' % cve0
        label = 'corpus-nvd'
    return {
        'cve_id': cve0,
        'cve_ids': cves,
        'ghsa_id': sid if src == 'ghsa' else '',
        'url': url,
        'summary': (title or '').strip()[:300],
        'source': label,
        'published': (disclosed or '')[:10],
    }


def corpus_cves(name, limit=50, db_path=None):
    """Compat wrapper: first page at `limit` per_page, flat row list."""
    d = corpus_cves_page(name, page=1, per_page=max(1, limit), db_path=db_path)
    return d['rows']


def refresh_corpus(checkouts=None, timeout=600):
    """Best-effort `git pull --ff-only` on each corpus checkout. Returns
    {name: 'ok'|'stale'|'missing'|'error: ...'} — never raises."""
    dirs = checkouts if checkouts is not None else corpus_dirs()
    out = {}
    for name, path in dirs.items():
        if not path or not os.path.isdir(path):
            out[name] = 'missing'
            continue
        if os.path.exists(os.path.join(path, '.git', 'index.lock')):
            # clone/pull in flight — never fight the lock, just skip this round
            out[name] = 'busy'
            continue
        try:
            p = subprocess.run(['git', '-C', path, 'pull', '--ff-only'],
                               capture_output=True, text=True, timeout=timeout)
            out[name] = 'ok' if p.returncode == 0 else 'stale'
        except Exception as e:
            out[name] = 'error: %s' % e
    return out


def freshness(checkouts=None):
    """Last local commit date per corpus checkout ({name: 'YYYY-MM-DD'|None}).
    Surfaced on the program page so 'corpus refresh rides the update button'
    is verifiable at a glance. Non-repo dirs degrade to None, never raise."""
    dirs = checkouts if checkouts is not None else corpus_dirs()
    out = {}
    for name, path in dirs.items():
        if not path or not os.path.isdir(path):
            out[name] = None
            continue
        try:
            p = subprocess.run(['git', '-C', path, 'log', '-1', '--format=%cs'],
                               capture_output=True, text=True, timeout=30)
            out[name] = p.stdout.strip()[:10] or None
        except Exception:
            out[name] = None
    return out


def _poc_tag(url):
    """Classify a CVE reference URL: 'edb' | 'repo-poc' | 'poc' | 'advisory'
    | None (plain refs are noise — not stored)."""
    u = (url or '').lower()
    if not u.startswith(('http://', 'https://')):
        return None
    if 'exploit-db.com' in u:
        return 'edb'
    if 'github.com/advisories' in u:
        return 'advisory'
    if 'github.com' in u and any(k in u for k in ('poc', 'exploit', 'cve-', 'vuln')):
        return 'repo-poc'
    if any(k in u for k in ('packetstorm', 'pastebin', '0day', 'exploit',
                            'seclists', 'gitlab.com', 'huntr.dev', 'vuldb')):
        return 'poc'
    return None


def seed_poc_refs(db_path=None, src_root=None, limit=None):
    """Harvest PoC-bearing references from every cvelistV5 record into the
    payloads index (data/payloads.db): cve_id -> tagged url. Plain marketing
    refs are skipped; exploit/poc/repo-poc/edb/advisory links are kept.
    Idempotent (INSERT OR IGNORE). Returns row count."""
    db = db_path or os.path.join(str(HERE), 'data', 'payloads.db')
    if not src_root:
        src_root = corpus_dirs().get('cvelistV5')
    if not src_root or not os.path.isdir(src_root):
        return None
    os.makedirs(os.path.dirname(db), exist_ok=True)
    cx = sqlite3.connect(db)
    try:
        cx.executescript(
            'PRAGMA journal_mode=WAL;'
            'CREATE TABLE IF NOT EXISTS poc_refs('
            ' cve_id TEXT NOT NULL, url TEXT NOT NULL, tag TEXT DEFAULT "",'
            ' UNIQUE(cve_id, url));'
            'CREATE INDEX IF NOT EXISTS idx_poc_refs_cve ON poc_refs(cve_id);'
            'CREATE INDEX IF NOT EXISTS idx_poc_refs_tag ON poc_refs(tag);')
        cves_root = os.path.join(src_root, 'cves')
        if not os.path.isdir(cves_root):
            return None
        seen = 0
        batch = []
        for year in sorted(os.listdir(cves_root)):
            yd = os.path.join(cves_root, year)
            if not os.path.isdir(yd):
                continue
            for chunk in sorted(os.listdir(yd)):
                cd = os.path.join(yd, chunk)
                if not os.path.isdir(cd):
                    continue
                for fn in sorted(os.listdir(cd)):
                    if not (fn.startswith('CVE-') and fn.endswith('.json')):
                        continue
                    if limit is not None and seen >= limit:
                        cx.commit()
                        return cx.execute('SELECT COUNT(*) FROM poc_refs').fetchone()[0]
                    try:
                        d = json.load(open(os.path.join(cd, fn), encoding='utf-8'))
                    except Exception:
                        continue
                    cve_id = (d.get('cveMetadata') or {}).get('cveId')
                    if not cve_id:
                        continue
                    seen += 1
                    cont = d.get('containers') or {}
                    refs = list(cna.get('references', []) if (cna := cont.get('cna')) else [])
                    for adp in (cont.get('adp') or []):
                        refs.extend(adp.get('references', []))
                    for ref in refs:
                        url = (ref or {}).get('url') or ''
                        tag = _poc_tag(url)
                        if tag:
                            batch.append((cve_id, url[:500], tag))
                    if len(batch) >= 4000:
                        cx.executemany('INSERT OR IGNORE INTO poc_refs VALUES(?,?,?)', batch)
                        batch = []
                        cx.commit()
        if batch:
            cx.executemany('INSERT OR IGNORE INTO poc_refs VALUES(?,?,?)', batch)
        cx.commit()
        return cx.execute('SELECT COUNT(*) FROM poc_refs').fetchone()[0]
    finally:
        cx.close()


def poc_refs_for(cve_id, db_path=None):
    """Tagged PoC/reference URLs for one CVE (payload lane lookup)."""
    db = db_path or (os.path.join(str(HERE), 'data', 'payloads.db')
                     if os.path.isfile(os.path.join(str(HERE), 'data', 'payloads.db')) else None)
    if not db or not (cve_id or '').strip():
        return []
    try:
        cx = sqlite3.connect('file:%s?mode=ro' % db.replace('\\', '/'), uri=True)
        try:
            rows = cx.execute('SELECT url, tag FROM poc_refs WHERE cve_id=? '
                              'ORDER BY tag', (cve_id.strip().upper(),)).fetchall()
        finally:
            cx.close()
    except sqlite3.Error:
        return []
    return [{'url': u, 'tag': t} for u, t in rows]


def edb_file_for(edb_id, src_root=None):
    """Absolute local path to the EDB exploit file for an edb-id, resolved
    through the clone's files_exploits.csv index. None when the archive is
    not cloned or the id is unknown — never raises."""
    import csv as _csv
    if not src_root:
        src_root = corpus_dirs().get('exploitdb')
    if not src_root:
        return None
    csv_path = os.path.join(src_root, 'files_exploits.csv')
    if not os.path.isfile(csv_path):
        return None
    want = str(edb_id).strip()
    try:
        with open(csv_path, encoding='utf-8', errors='replace', newline='') as fh:
            for row in _csv.DictReader(fh):
                if (row.get('id') or '').strip() == want:
                    p = os.path.join(src_root, row.get('file') or '')
                    return p if os.path.isfile(p) else None
    except OSError:
        return None
    return None


def corpus_stats():
    """Completeness counters for the operator's corpus: vault CVE-bearing rows
    plus, when cloned, on-disk cvelistV5 record count. Never raises."""
    stats = {'vault_db': vault_db_path() or '', 'vault_cve_rows': 0,
             'checkouts': {}}
    db = vault_db_path()
    if db:
        try:
            cx = sqlite3.connect('file:%s?mode=ro' % db.replace('\\', '/'), uri=True)
            try:
                stats['vault_cve_rows'] = cx.execute(
                    "SELECT COUNT(*) FROM bug_corpus WHERE cve_ids != ''").fetchone()[0]
            finally:
                cx.close()
        except sqlite3.Error:
            pass
    for name, path in corpus_dirs().items():
        if not path:
            stats['checkouts'][name] = None
            continue
        cves_root = os.path.join(path, 'cves')
        n = 0
        if os.path.isdir(cves_root):
            for _root, _dirs, files in os.walk(cves_root):
                n += sum(1 for f in files if f.endswith('.json'))
        stats['checkouts'][name] = {'path': path, 'json_files': n}
    return stats
