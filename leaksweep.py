"""Constant leak-sweep mode: rotates through every program that has
GitHub-shaped in-scope targets (newest first), queueing repo-mode scans
(clone worktree, no history — the modest tier) through the normal scan
machinery, so bars, the /scans hub, findings badges and history all apply.

Discipline: in-scope identities only, one scan at a time, sleep between
scans (LEAKSWEEP_SLEEP seconds, default 20), state persists in
data/leaksweep.json and resumes on server start. Read-only on targets:
find & report, never authenticate with anything found.
"""
import json
import os
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATE_PATH = Path(os.environ.get('LEAKSWEEP_STATE') or (HERE / 'data' / 'leaksweep.json'))
SLEEP_BETWEEN = max(5, int(os.environ.get('LEAKSWEEP_SLEEP', '20')))
MAX_TARGETS_PER_PROGRAM = 3

_lock = threading.Lock()
_thread = None
_wake = threading.Event()


def _load():
    try:
        st = json.loads(STATE_PATH.read_text(encoding='utf-8'))
        if isinstance(st, dict):
            st.setdefault('running', False)
            st.setdefault('cursor', 0)
            st.setdefault('scanned', 0)
            st.setdefault('findings', 0)
            return st
    except Exception:
        pass
    return {'running': False, 'cursor': 0, 'scanned': 0, 'findings': 0,
            'started': '', 'last_scan': ''}


def _save(st):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        STATE_PATH.write_text(json.dumps(st), encoding='utf-8')
    except OSError:
        pass


def set_include_out_scope(val):
    with _lock:
        st = _load()
        st['include_out_scope'] = bool(val)
        _save(st)
    return st


def state():
    with _lock:
        st = _load()
        st['thread_alive'] = _thread is not None and _thread.is_alive()
        return st


def start(db_path):
    """Start the rotating sweep (no-op when already running). Returns the
    state dict."""
    global _thread
    with _lock:
        st = _load()
        if _thread is not None and _thread.is_alive():
            st['thread_alive'] = True
            return st
        st['running'] = True
        st['thread_alive'] = True
        if not st.get('started'):
            st['started'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        _save(st)
        _thread = threading.Thread(target=_loop, args=(db_path,), daemon=True)
        _thread.start()
        return st


def stop():
    with _lock:
        st = _load()
        st['running'] = False
        _save(st)
    _wake.set()
    return st


def maybe_resume(db_path):
    """Server-start hook: resume automatically when the state says running."""
    if _load().get('running'):
        return start(db_path)
    return None


def _github_targets(cx, pid, include_out_scope=True):
    """GitHub-shaped identities of one program. The leak scan deliberately
    includes out-of-scope targets too (operator directive): leaked keys on
    public GitHub are how keys are found, and scope exclusions bind
    submissions, not scanning-insight."""
    import scanhook
    out = []
    if include_out_scope:
        cur = cx.execute('SELECT identity FROM targets WHERE program_id=?', (pid,))
    else:
        cur = cx.execute('SELECT identity FROM targets WHERE program_id=? '
                         'AND in_scope=1 AND eligible=1', (pid,))
    for r in cur.fetchall():
        norm = scanhook.normalize_cve_identity(r['identity'])
        if norm:
            out.append((r['identity'], norm))
    return out


def _next_program(cx, cursor, include_out_scope=True):
    """Next program after the cursor with a github-shaped target, wrapping
    to the oldest when the end is reached. Returns (pid, targets) or None."""
    for lo, hi in ((cursor, 10 ** 9), (0, cursor + 1)):
        for r in cx.execute('SELECT id FROM programs WHERE id > ? AND id <= ? '
                            'ORDER BY id LIMIT 200', (lo, hi)).fetchall():
            t = _github_targets(cx, r['id'], include_out_scope)
            if t:
                return r['id'], t
    return None


def _loop(db_path):
    import db as _db
    import scanhook
    while True:
        with _lock:
            st = _load()
            if not st.get('running'):
                return
            include_oos = st.get('include_out_scope', True)
        _wake.wait(timeout=SLEEP_BETWEEN)
        _wake.clear()
        cx = _db.connect(db_path)
        try:
            nxt = _next_program(cx, st.get('cursor', 0), include_oos)
        finally:
            cx.close()
        if not nxt:
            st = _load()
            st['cursor'] = 0  # full pass complete — wrap around
            _save(st)
            continue
        pid, targets = nxt
        for ident, norm in targets[:MAX_TARGETS_PER_PROGRAM]:
            with _lock:
                if not _load().get('running'):
                    return
            # org-shaped targets sweep the whole org + its members (the
            # classic employee-leak surface); repo-shaped sweep just the repo
            mode = 'leakorg' if '/' not in norm else 'leak'
            try:
                sid, err = scanhook.start_scan(db_path, pid, ident, mode=mode)
            except Exception:
                sid, err = None, 'harness error'
            if err or not sid:
                break
            # wait for this scan to finish (or stop to be requested)
            while True:
                with _lock:
                    if not _load().get('running'):
                        return
                time.sleep(5)
                cx2 = _db.connect(db_path)
                try:
                    row = cx2.execute('SELECT status, findings FROM scans '
                                      'WHERE id=?', (sid,)).fetchone()
                finally:
                    cx2.close()
                if row is None or row['status'] in ('done', 'error'):
                    break
            st = _load()
            st['scanned'] = st.get('scanned', 0) + 1
            st['findings'] = st.get('findings', 0) + (row['findings'] if row else 0)
            st['last_scan'] = {'scan_id': sid, 'program_id': pid,
                               'identity': ident,
                               'ts': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}
            st['cursor'] = pid
            _save(st)
            time.sleep(SLEEP_BETWEEN)
