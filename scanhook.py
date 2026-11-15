"""External leak-scanner hook (operator-owned tool, never modified here).

Zero-config: if LEAK_SCAN_CMD is unset, the known crv-leak-scan checkout is
auto-detected. Modes: light (worktree only), full (all history), sniper
(single GitHub target, max depth), cves (CVE sweep, GitHub-shaped targets).
  LEAK_SCAN_CMD="python C:\\tools\\leakscan.py scan {target}"
{target} is replaced with the scope identity. No shell is used, argv only.
Quote paths containing spaces. Timeout via LEAK_SCAN_TIMEOUT (default 120s).
Results land in the scans table; the UI polls them.
"""
import json
import os
import re
import shlex
import subprocess
import threading

DEFAULT_TIMEOUT = 120
MODE_TIMEOUTS = {'light': DEFAULT_TIMEOUT, 'full': DEFAULT_TIMEOUT,
                 'sniper': DEFAULT_TIMEOUT,
                 'cves': 600,          # org sweeps list repos + batch advisories
                 'leak': 300,          # single-repo clone, worktree only
                 'leakorg': 1800}      # whole org + members, worktree only
MODES = {'light': '{target} --no-history --json',
         'full': '{target} --full-history --json',
         'sniper': '--sniper {target} --json',
         'cves': '--cves {target} --json',
         'leak': '--repo {target} --no-history --json --reveal',
         'leakorg': '--org {target} --members --no-history --json --reveal'}
DEFAULT_MODE = 'light'

# Portable auto-detect: checked in order until one exists on disk.
_SCANNER_CANDIDATES = (
    'scanner_path.txt',                    # one-line path file shipped with the app
    os.path.join('..', 'crv-leak-scan', 'dist', 'cli.js'),   # sibling of this folder
    os.path.join('crv-leak-scan', 'dist', 'cli.js'),         # dropped inside this folder
    os.path.join('..', '..', 'crv-leak-scan', 'dist', 'cli.js'),
    os.path.expanduser(os.path.join('~', 'crv-leak-scan', 'dist', 'cli.js')),
    os.path.expanduser(os.path.join('~', 'Desktop', 'crv-leak-scan', 'dist', 'cli.js')),
)


def _scanner_candidates():
    here = os.path.dirname(os.path.abspath(__file__))
    out = []
    for c in _SCANNER_CANDIDATES:
        p = c if os.path.isabs(c) else os.path.normpath(os.path.join(here, c))
        out.append(p)
    return out


def find_scanner():
    """First existing crv-leak-scan cli.js from the candidate list, else PATH.
    scanner_path.txt is special: a one-line file CONTAINING the scanner path
    (portable hand-off — recipient pastes their own path into it)."""
    here = os.path.dirname(os.path.abspath(__file__))
    for c in _scanner_candidates():
        p = c if os.path.isabs(c) else os.path.normpath(os.path.join(here, c))
        if c == 'scanner_path.txt':
            if os.path.isfile(p):
                inner = open(p, encoding='utf-8', errors='ignore').read().strip()
                if inner and os.path.isfile(inner):
                    return inner
            continue
        if os.path.isfile(p):
            return p
    # crv-leak-scan installed globally (npm i -g) → on PATH
    path_env = os.environ.get('PATH', '')
    for d in path_env.split(os.pathsep):
        p = os.path.join(d, 'crv-leak-scan')
        if os.path.isfile(p):
            return p
    return None


def scan_command():
    """Configured template, or the auto-detected crv-leak-scan default.
    The bare binary means build_argv appends mode flags (incl. target).
    Override mode flags entirely by setting LEAK_SCAN_CMD yourself."""
    tmpl = (os.environ.get('LEAK_SCAN_CMD') or '').strip()
    if tmpl:
        return tmpl
    found = find_scanner()
    if found:
        return 'node "%s"' % found
    return ''


def normalize_cve_identity(ident):
    """Scope identities come in many shapes (github URLs, domains, contracts,
    bare owners); the CVE sweep wants owner or owner/repo. GitHub URLs
    normalize (tree/blob paths stripped), bare GitHub-shaped owners and
    owner/repo pairs pass through, everything else -> None (rejected)."""
    s = (ident or '').strip()
    m = re.match(r'https?://github\.com/([A-Za-z0-9_.-]+)(?:/([A-Za-z0-9_.-]+))?',
                 s, re.I)
    if m:
        owner, repo = m.group(1), m.group(2)
        return owner + ('/' + repo if repo else '')
    if '/' not in s and re.match(r'^[A-Za-z0-9-]{1,39}$', s):
        return s          # bare GitHub-shaped owner
    if re.match(r'^[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9._-]+$', s):
        return s          # owner/repo
    return None


def scan_timeout(mode=None):
    """LEAK_SCAN_TIMEOUT wins when set; otherwise the mode's default — org
    CVE sweeps legitimately need minutes (repo list + advisory batches)."""
    env = (os.environ.get('LEAK_SCAN_TIMEOUT') or '').strip()
    if env:
        try:
            return max(5, int(env))
        except (TypeError, ValueError):
            pass
    return MODE_TIMEOUTS.get(mode, DEFAULT_TIMEOUT)


def build_argv(identity, mode=None):
    """Split the configured template; substitute {target}. Returns None if unset.
    Modes append the scanner's mode flags. If the template already contains
    {target} the flags are added bare; otherwise the mode string carries the
    target itself (auto-detected bare-binary template)."""
    tmpl = scan_command()
    if not tmpl:
        return None
    mode = mode if mode in MODES else DEFAULT_MODE
    argv = shlex.split(tmpl, posix=(os.name != 'nt'))
    cleaned = []
    for a in argv:
        if len(a) >= 2 and a[0] == a[-1] and a[0] in ('"', "'"):
            a = a[1:-1]
        cleaned.append(a.replace('{target}', identity))
    if not any(a.startswith('--') for a in argv):
        # substitute on the ARGV LIST — never shlex the identity back into a
        # string, or paths with spaces split into multiple targets.
        mode_parts = shlex.split(MODES[mode])
        if mode in ('sniper', 'cves', 'leak', 'leakorg'):
            # flag-consumes-target modes: drop any positional target from the
            # template and pass the identity as the flag's VALUE.
            cleaned = [x for x in cleaned if x != identity]
            cleaned += [identity if p == '{target}' else p for p in mode_parts]
            if mode == 'cves':
                # offline layer: feed the local advisory-database checkout when
                # one is detectable. Portable: no vault on disk -> argv unchanged
                # and the sweep stays API-only.
                try:
                    import cvecorpus
                    _corpus = (cvecorpus.corpus_dirs() or {}).get('advisory-database')
                    if _corpus:
                        cleaned += ['--cve-corpus', _corpus]
                except Exception:
                    pass
        elif '{target}' in tmpl:
            cleaned += [p for p in mode_parts if p != '{target}']
        else:
            cleaned += [identity if p == '{target}' else p for p in mode_parts]
    return cleaned


def summarize(output):
    """Best-effort one-line summary of scanner stdout.
    crv-leak-scan aware: reads summary.findings + bySeverity from --json."""
    text = (output or '').strip()
    if not text:
        return 'no output'
    try:
        d = json.loads(text)
        if isinstance(d, dict):
            s = d.get('summary')
            if isinstance(s, dict) and 'cves' in d and 'total' in s:
                # crv-leak-scan --cves shape: N cves with a bySeverity block
                sev = s.get('bySeverity') or {}
                bits = ['%d cves' % s.get('total', 0)]
                for k in ('critical', 'high', 'medium', 'low'):
                    if sev.get(k):
                        bits.append('%d %s' % (sev[k], k))
                return ', '.join(bits)
            if isinstance(s, dict) and 'findings' in s:
                sev = s.get('bySeverity') or {}
                bits = ['%d findings' % s.get('findings', 0)]
                for k in ('critical', 'high', 'medium'):
                    if sev.get(k):
                        bits.append('%d %s' % (sev[k], k))
                return ', '.join(bits)
            for key in ('summary', 'result', 'verdict', 'status'):
                if d.get(key):
                    return str(d[key])[:200]
            return json.dumps(d)[:200]
        return str(d)[:200]
    except Exception:
        pass
    line = text.splitlines()[0] if text.splitlines() else text
    return line[:200]


def start_scan(db_path, program_id, identity, mode=None, manual=False):
    """Validate, insert pending row, run scanner in background. Returns scan id.
    manual=True allows scanning any target the operator pastes (their tool,
    their call); without it the identity must be in the program's scope."""
    import db as _db
    from datetime import datetime, timezone
    cx = _db.connect(db_path)
    try:
        prow = cx.execute('SELECT id FROM programs WHERE id=?', (program_id,)).fetchone()
        if not prow:
            return None, 'unknown program'
        if not manual:
            trow = cx.execute('SELECT identity FROM targets WHERE program_id=? AND identity=?',
                              (program_id, identity)).fetchone()
            if not trow:
                return None, 'identity not in scope for this program'
        ts = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        cur = cx.execute('INSERT INTO scans(program_id,identity,ts,status,mode) VALUES(?,?,?,?,?)',
                         (program_id, identity, ts, 'pending', mode if mode in MODES else DEFAULT_MODE))
        sid = cur.lastrowid
        cx.commit()
    finally:
        cx.close()
    threading.Thread(target=_run, args=(db_path, sid), daemon=True).start()
    return sid, None


def _run(db_path, scan_id):
    import db as _db
    pid, ident, argv, mode = _scan_ctx(db_path, scan_id)
    if argv is None:
        _finish(db_path, scan_id, 'error', 'LEAK_SCAN_CMD not configured', '')
        return
    _mark(db_path, scan_id, 'running', set_started=True)
    try:
        p = subprocess.run(argv, capture_output=True, text=True,
                           timeout=scan_timeout(mode))
        out = (p.stdout or '') + (('\n' + p.stderr) if p.stderr else '')
        # crv-leak-scan exit codes: 0 = ran (findings may still exist below
        # --fail-on threshold), 1 = findings at/above threshold (success),
        # 2 = usage/runtime error. Only 2+ is a harness-level failure.
        if p.returncode in (0, 1):
            note = '' if p.returncode == 0 else ' [fail-on threshold hit]'
            stored = 0
            try:
                stored = store_cves(db_path, pid, ident, p.stdout or '')
            except Exception:
                stored = -1  # scan verdict stands; CVE persistence failed
            suffix = '' if stored >= 0 else ' [cve store failed]'
            # parse findings BEFORE the 20k raw truncation — big reports lose
            # their findings array in `raw`, so the redacted rows are stored
            # as structured JSON for the viewer
            frows = findings_from_raw(p.stdout or '')
            _finish(db_path, scan_id, 'done',
                    summarize(p.stdout or '') + note + suffix, out[:20000],
                    findings=findings_count(p.stdout or ''),
                    findings_json=json.dumps(frows))
        else:
            _finish(db_path, scan_id, 'error',
                    'exit %d: %s' % (p.returncode, summarize(out)), out[:20000])
    except subprocess.TimeoutExpired:
        _finish(db_path, scan_id, 'error',
                'timed out after %ds' % scan_timeout(mode), '')
    except FileNotFoundError as e:
        _finish(db_path, scan_id, 'error', 'scanner not found: %s' % e, '')
    except Exception as e:  # never let a worker die silently
        _finish(db_path, scan_id, 'error', 'harness failure: %s' % e, '')


def _scan_ctx(db_path, scan_id):
    """(program_id, identity, argv, mode) for a queued scan row. The row's
    mode is authoritative — without it every scan would silently run light."""
    import db as _db
    cx = _db.connect(db_path)
    try:
        r = cx.execute('SELECT program_id, identity, mode FROM scans WHERE id=?',
                       (scan_id,)).fetchone()
        pid = r['program_id'] if r else None
        ident = r['identity'] if r else ''
        mode = (r['mode'] if r else None) or DEFAULT_MODE
    finally:
        cx.close()
    if mode not in MODES:
        mode = DEFAULT_MODE
    ident_for_argv = ident
    if mode in ('cves', 'leak', 'leakorg'):
        # scope rows store raw identities (github URLs, domains); these modes
        # want owner/repo or owner — normalize or reject loudly at queue time
        ident_for_argv = normalize_cve_identity(ident) or ident
    return pid, ident, build_argv(ident_for_argv, mode), mode


def store_cves(db_path, program_id, identity, stdout_text):
    """Upsert the crv-leak-scan --cves JSON into the cves table. Returns the
    row count touched (0 when the output is not a CVE sweep). Only CVE-keyed
    records are kept — the radar's CVEs are the IDs, per operator spec."""
    import db as _db
    text = (stdout_text or '').strip()
    if not text:
        return 0
    try:
        d = json.loads(text)
    except Exception:
        return 0
    if not isinstance(d, dict) or not isinstance(d.get('cves'), list):
        return 0
    ts = _db.now_iso()
    cx = _db.connect(db_path)
    n = 0
    try:
        for rec in d['cves']:
            if not isinstance(rec, dict):
                continue
            cve_id = str(rec.get('cveId') or '').strip().upper()
            if not cve_id.startswith('CVE-'):
                continue
            cx.execute('''INSERT INTO cves(program_id,identity,cve_id,ghsa_id,url,
                          summary,severity,score,published,source,ts)
                          VALUES(?,?,?,?,?,?,?,?,?,?,?)
                          ON CONFLICT(program_id,identity,cve_id) DO UPDATE SET
                          ghsa_id=excluded.ghsa_id, url=excluded.url,
                          summary=excluded.summary, severity=excluded.severity,
                          score=excluded.score, published=excluded.published,
                          source=excluded.source, ts=excluded.ts''',
                       (program_id, identity, cve_id,
                        str(rec.get('ghsaId') or ''),
                        str(rec.get('url') or ('https://github.com/advisories?query=' + cve_id)),
                        str(rec.get('summary') or '')[:300],
                        str(rec.get('severity') or '').lower(),
                        rec.get('cvssScore') if isinstance(rec.get('cvssScore'), (int, float)) else None,
                        str(rec.get('publishedAt') or ''),
                        'crv-leak-scan', ts))
            n += 1
        cx.commit()
    finally:
        cx.close()
    return n


def list_cves(cx, program_id, limit=200):
    """CVE rows for a program, newest first (operator spec: latest-first);
    severity ranks and CVSS break ties within the same date."""
    return [dict(r) for r in cx.execute(
        '''SELECT * FROM cves WHERE program_id=? ORDER BY
           published DESC,
           CASE lower(severity) WHEN 'critical' THEN 0 WHEN 'high' THEN 1
           WHEN 'medium' THEN 2 WHEN 'low' THEN 3 ELSE 4 END,
           score DESC, cve_id LIMIT ?''',
        (program_id, limit)).fetchall()]


def build_argv_at_run(db_path, scan_id):
    import db as _db
    cx = _db.connect(db_path)
    try:
        r = cx.execute('SELECT identity FROM scans WHERE id=?', (scan_id,)).fetchone()
        ident = r['identity'] if r else ''
    finally:
        cx.close()
    return build_argv(ident)


def _mark(db_path, scan_id, status, set_started=False):
    import db as _db
    cx = _db.connect(db_path)
    try:
        if set_started:
            cx.execute('UPDATE scans SET status=?, started=? WHERE id=?',
                       (status, _db.now_iso(), scan_id))
        else:
            cx.execute('UPDATE scans SET status=? WHERE id=?', (status, scan_id))
        cx.commit()
    finally:
        cx.close()


def scan_progress(row):
    """0-100 + honest semantics for the UI bars. The external scanner streams
    no progress, so 'running' is elapsed-time against the mode's budget
    (capped at 95% until the process actually exits); queued=0, done/error=100.
    `row` is a dict including status/started/ts/mode."""
    status = row.get('status') or 'pending'
    if status == 'pending':
        return 0
    if status in ('done', 'error'):
        return 100
    started = row.get('started') or row.get('ts') or ''
    try:
        from datetime import datetime, timezone
        t0 = datetime.strptime(started, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
        elapsed = (datetime.now(timezone.utc) - t0).total_seconds()
    except Exception:
        return 3
    budget = scan_timeout(row.get('mode'))
    return min(95, max(3, int(elapsed / budget * 100)))


def elapsed_seconds(row):
    """Seconds since the scan started running (0 when not running/done)."""
    if (row.get('status') or '') not in ('running', 'done', 'error'):
        return 0
    started = row.get('started') or row.get('ts') or ''
    try:
        from datetime import datetime, timezone
        t0 = datetime.strptime(started, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
        return max(0, int((datetime.now(timezone.utc) - t0).total_seconds()))
    except Exception:
        return 0


def list_scans_all(cx, limit=80):
    """Every scan across programs, newest first, with program names + UI
    progress fields — feeds the /scans hub and /api/scans/all."""
    out = []
    for r in cx.execute('''SELECT s.*, p.name AS pname FROM scans s
                           LEFT JOIN programs p ON p.id = s.program_id
                           ORDER BY s.id DESC LIMIT ?''', (limit,)).fetchall():
        d = dict(r)
        d['program_name'] = d.pop('pname') or 'unknown program'
        d['progress'] = scan_progress(d)
        d['elapsed'] = elapsed_seconds(d)
        out.append(d)
    return out


def _load_json_prefix(text):
    """stdout is pure JSON but `raw` stores stdout+stderr concatenated —
    trim trailing noise down to the last parseable closing brace."""
    t = (text or '').strip()
    try:
        return json.loads(t)
    except Exception:
        pass
    tries = 0
    i = len(t)
    while i > 0 and tries < 60:
        i = t.rfind('}', 0, i)
        if i < 0:
            break
        tries += 1
        try:
            return json.loads(t[:i + 1])
        except Exception:
            continue
    return None


def findings_from_raw(text, cap=40):
    """Redacted-safe finding rows from scanner --json output. Handles BOTH
    envelopes: local scans emit top-level findings; remote (repo/org) scans
    emit {summary, repos: [{fullName, findings: [...]}]}. Raw secrets never
    leave the scanner — only the masked rendering is kept."""
    d = _load_json_prefix(text)
    if not isinstance(d, dict):
        return []
    items = []
    top = d.get('findings')
    if isinstance(top, list):
        items.extend(f for f in top if isinstance(f, dict))
    for repo in (d.get('repos') or []):
        if isinstance(repo, dict) and isinstance(repo.get('findings'), list):
            rn = str(repo.get('fullName') or repo.get('repo') or '')
            for f in repo['findings']:
                if isinstance(f, dict):
                    f2 = dict(f, packageName=rn)
                    items.append(f2)
    out = []
    seen = set()
    for f in items:
        if len(out) >= cap:
            break
        key = (f.get('file'), f.get('line'), str(f.get('masked')))
        if key in seen:
            continue
        seen.add(key)
        row_out = {
            'detector': str(f.get('detectorName') or f.get('detector') or ''),
            'file': str(f.get('file') or ''),
            'line': f.get('line'),
            'severity': str(f.get('severity') or '').lower(),
            'masked': str(f.get('masked') or '')[:120],
            # --reveal runs on leak modes: the operator verifies liveness of
            # found keys, so the raw secret is kept locally, never published
            'raw': str(f.get('raw') or '')[:400],
            'score': f.get('score'),
            'scoreLabel': str(f.get('scoreLabel') or ''),
            'commit': str(f.get('commit') or '')[:12],
            'packageName': str(f.get('packageName') or ''),
        }
        # offline address derivation for eth keys — pure math, no network;
        # lets the viewer + liveness checker target the right wallet
        det_l = row_out['detector'].lower()
        if ('eth' in det_l or 'private key' in det_l) and row_out['raw']:
            try:
                from eth_keys import keys as _eth_keys
                h = row_out['raw'].strip()
                if h.startswith(('0x', '0X')):
                    h = h[2:]
                if len(h) == 64:
                    row_out['address'] = _eth_keys.PrivateKey(
                        bytes.fromhex(h)).public_key.to_checksum_address()
            except Exception:
                pass
        out.append(row_out)
    return out


def findings_count(text):
    d = _load_json_prefix(text)
    if isinstance(d, dict):
        s = d.get('summary')
        if isinstance(s, dict) and 'findings' in s:
            try:
                return int(s.get('findings') or 0)
            except (TypeError, ValueError):
                return 0
        f = d.get('findings')
        if isinstance(f, list):
            return len(f)
    return 0


def _finish(db_path, scan_id, status, summary, raw, findings=0, findings_json=''):
    import db as _db
    cx = _db.connect(db_path)
    try:
        cx.execute('UPDATE scans SET status=?, summary=?, raw=?, findings=?, '
                   'findings_json=? WHERE id=?',
                   (status, summary, raw, findings, findings_json, scan_id))
        cx.commit()
    finally:
        cx.close()


def list_scans(cx, program_id, limit=50):
    return [dict(r) for r in cx.execute(
        'SELECT * FROM scans WHERE program_id=? ORDER BY id DESC LIMIT ?',
        (program_id, limit)).fetchall()]
