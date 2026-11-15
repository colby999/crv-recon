"""bbCRV recon web app: program feed, detail, live target feed, platforms, JSON API."""
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone

from flask import Flask, jsonify, render_template, request

import db
from opportunity import score_program
from tracker import ingest
from scrapers import seed as seed_mod

PER_PAGE_DEFAULT = 25
LOG_PATH = os.environ.get('RADAR_LOG')

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[logging.StreamHandler()] + (
        [logging.FileHandler(LOG_PATH)] if LOG_PATH else []))
log = logging.getLogger('radar')


def _int(val, default, lo, hi):
    try:
        return min(hi, max(lo, int(val)))
    except (TypeError, ValueError):
        return default


def human_money(v):
    """$58000000.0 -> $58M, 1350000 -> $1.35M, 42000 -> $42k."""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return ''
    if v >= 1_000_000:
        s = ('%gM' % (v / 1_000_000))
    elif v >= 1_000:
        s = ('%gk' % (v / 1_000))
    else:
        s = '%g' % v
    return '$' + s
PLATFORMS = ['hackenproof', 'yeswehack', 'hackerone', 'bugcrowd', 'immunefi',
             'intigriti', 'cantina', 'sherlock']


_refresh_lock = threading.Lock()
_refresh_running = False


def row_program(r):
    d = dict(r)
    try:
        d['tags'] = json.loads(d.get('tags') or '[]')
    except Exception:
        d['tags'] = []
    try:
        d['languages'] = json.loads(d.get('languages') or '[]')
    except Exception:
        d['languages'] = []
    tier, score, why = score_program(d)
    d['tier'], d['score'], d['tier_why'] = tier, score, why
    from tags import is_new, rel_time
    d['is_new'] = is_new(d.get('upstream_listed') or d.get('first_seen'))
    d['listed_rel'] = (rel_time(d['upstream_listed'] + 'T00:00:00Z')
                       if d.get('upstream_listed') else rel_time(d.get('first_seen')))
    d['listed_is_upstream'] = bool(d.get('upstream_listed'))
    d['updated_rel'] = (rel_time(d['upstream_updated'] + 'T00:00:00Z')
                        if d.get('upstream_updated') else None)
    d['checked_rel'] = rel_time(d.get('last_seen'))
    d['ends_rel'] = rel_time(d.get('end_date')) if d.get('end_date') else None
    d['focus_list'] = [x for x in (d.get('focus') or '').split(',') if x]
    return d


def query_programs(cx, q='', platform='', tag='', vdp_mode='paid', sort='newest',
                   tier='', show='active', contest='', limit=2000):
    sql = 'SELECT * FROM programs WHERE 1=1'
    args = []
    if q:
        sql += (' AND (name LIKE ? OR slug LIKE ? OR url LIKE ? '
                'OR EXISTS (SELECT 1 FROM targets WHERE targets.program_id=programs.id '
                'AND targets.identity LIKE ?))')
        like = '%' + q + '%'
        args += [like, like, like, like]
    if platform:
        sql += ' AND platform=?'
        args.append(platform)
    if tag:
        sql += ' AND tags LIKE ?'
        args.append('%"' + tag + '"%')
    if vdp_mode == 'paid':
        sql += ' AND vdp=0'
    elif vdp_mode == 'vdp':
        sql += ' AND vdp=1'
    if contest == '1':
        sql += ' AND is_contest=1'
    elif contest == '0':
        sql += ' AND is_contest=0'
    if show == 'active':
        sql += " AND state NOT IN ('upcoming','ended')"
    elif show == 'upcoming':
        sql += " AND state='upcoming'"
    elif show == 'suspended':
        sql += " AND state='suspended'"
    elif show == 'ended':
        sql += " AND state='ended'"
    rows = [row_program(r) for r in cx.execute(sql, args).fetchall()]
    if tier:
        rows = [r for r in rows if (r['tier'] or '') == tier]
    if sort == 'bounty':
        rows.sort(key=lambda r: (r['bounty_max'] if r['bounty_max'] is not None else -1),
                  reverse=True)
    elif sort == 'name':
        rows.sort(key=lambda r: r['name'].lower())
    elif sort == 'reports':
        rows.sort(key=lambda r: r['report_count'] or 0, reverse=True)
    else:
        # newest = real platform dates only; unknown-date rows sink to the
        # bottom (a scrape timestamp is not a launch date)
        rows.sort(key=lambda r: r['upstream_listed'] or r['upstream_updated'] or '',
                  reverse=True)
    return rows[:limit]


def last_refresh(cx):
    try:
        r = cx.execute('SELECT platform, MAX(ts) ts FROM scrapes GROUP BY platform').fetchall()
        return {x['platform']: x['ts'] for x in r}
    except Exception:
        return {}


def attach_scope(cx, rows, sample_n=3):
    """Add scope_n / scope_eligible / scope_sample to program dicts."""
    if not rows:
        return rows
    ids = [r['id'] for r in rows]
    q = ','.join('?' * len(ids))
    counts = {}
    for r in cx.execute(
            'SELECT program_id, COUNT(*) n, COALESCE(SUM(eligible),0) e '
            'FROM targets WHERE program_id IN (%s) AND in_scope=1 GROUP BY program_id' % q, ids):
        counts[r['program_id']] = (r['n'], r['e'])
    samples = {}
    for r in cx.execute(
            'SELECT program_id, identity FROM targets WHERE program_id IN (%s) '
            'AND in_scope=1 ORDER BY eligible DESC, identity LIMIT %d' % (q, len(ids) * sample_n), ids):
        samples.setdefault(r['program_id'], []).append(r['identity'])
    for r in rows:
        n, e = counts.get(r['id'], (0, 0))
        r['scope_n'], r['scope_eligible'] = n, e
        r['scope_sample'] = samples.get(r['id'], [])[:sample_n]
    return rows


def create_app(db_path=None):
    app = Flask(__name__)
    app.config['DB_PATH'] = db_path or os.environ.get('RADAR_DB') or str(db.DB_PATH)
    app.jinja_env.filters['human_money'] = human_money

    def cx():
        c = db.connect(app.config['DB_PATH'])
        return c

    @app.route('/')
    def index():
        args = request.args
        c = cx()
        try:
            rows = query_programs(
                c, q=args.get('q', '').strip(), platform=args.get('platform', ''),
                tag=args.get('tag', ''), vdp_mode=args.get('vdp', 'paid'),
                sort=args.get('sort', 'newest'), tier=args.get('tier', ''),
                show=args.get('show', 'active'), contest=args.get('contest', ''))
            total = len(rows)
            page = _int(args.get('page', 1), 1, 1, 10 ** 6)
            per = _int(args.get('per', PER_PAGE_DEFAULT), PER_PAGE_DEFAULT, 1, 100)
            pages = max(1, (total + per - 1) // per)
            page = min(page, pages)
            progs = rows[(page - 1) * per:page * per]
            attach_scope(c, progs)
            plats = [r['platform'] for r in
                     c.execute('SELECT DISTINCT platform FROM programs ORDER BY 1').fetchall()]

            def page_url(pn):
                a2 = dict(args)
                a2['page'] = pn
                return '?' + '&'.join('%s=%s' % (k, v) for k, v in a2.items() if v != '')

            def page_window(page, pages, span=2):
                """Numbered pager window: current ±2, always 1 and last, … gaps."""
                if pages <= 1:
                    return []
                start = max(1, page - span)
                end = min(pages, page + span)
                win = []
                if start > 1:
                    win.append(1)
                    if start > 2:
                        win.append('…')
                win.extend(range(start, end + 1))
                if end < pages:
                    if end < pages - 1:
                        win.append('…')
                    win.append(pages)
                return win

            exp_args = dict(args)
            exp_args.pop('page', None)
            export_url = '/export.txt?' + '&'.join('%s=%s' % (k, v)
                                                   for k, v in exp_args.items() if v != '')
            return render_template('index.html', programs=progs, total=total, page=page,
                                   pages=pages, per=per, args=args, platforms=plats,
                                   view=args.get('view', 'grid'),
                                   updated=last_refresh(c), export_url=export_url,
                                   page_window=page_window(page, pages),
                                   page_url=page_url,
                                   prev_url=page_url(page - 1) if page > 1 else None,
                                   next_url=page_url(page + 1) if page < pages else None)
        finally:
            c.close()

    @app.route('/program/<int:pid>')
    def program(pid):
        c = cx()
        try:
            r = c.execute('SELECT * FROM programs WHERE id=?', (pid,)).fetchone()
            if not r:
                return render_template('404.html'), 404
            p = row_program(r)
            tg = c.execute("SELECT * FROM targets WHERE program_id=? ORDER BY eligible DESC, in_scope DESC, identity",
                           (pid,)).fetchall()
            from opportunity import score_target
            scored = []
            for t in tg:
                s = score_target(p.get('bounty_max'), p.get('report_count'), t['target_type'])
                label = 'Elite target' if s >= 85 else 'Strong target' if s >= 70 else 'Potential target' if s >= 50 else 'Normal'
                scored.append((t, s, label))
            in_scope = [(t, s, lb) for (t, s, lb) in scored if t['in_scope'] and t['eligible']]
            out_scope = [(t, s, lb) for (t, s, lb) in scored if not (t['in_scope'] and t['eligible'])]
            hist = c.execute("SELECT * FROM changes WHERE program_id=? ORDER BY id DESC LIMIT 50",
                             (pid,)).fetchall()
            sync_queued = False
            if not tg and p['platform'] in ('hackenproof', 'bugcrowd'):
                sync_queued = maybe_lazy_deep(app.config['DB_PATH'], p['platform'], p['slug'], pid)
            import scanhook
            scans = scanhook.list_scans(c, pid)
            scans = [dict(s, progress=scanhook.scan_progress(s)) for s in scans]
            import cvecorpus
            try:
                cpage = cvecorpus.corpus_cves_page(
                    p['name'], page=request.args.get('page', 1, type=int),
                    per_page=request.args.get('per_page', 25, type=int))
            except Exception:
                cpage = {'total': 0, 'page': 1, 'per_page': 25, 'pages': 0, 'rows': []}
            try:
                corpus_fresh = cvecorpus.freshness()
            except Exception:
                corpus_fresh = {}
            return render_template('program.html', p=p, in_scope=in_scope,
                                   out_scope=out_scope, history=hist,
                                   sync_queued=sync_queued, scans=scans,
                                   cves=scanhook.list_cves(c, pid),
                                   corpus_cves=cpage['rows'],
                                   corpus_total=cpage['total'],
                                   corpus_page=cpage['page'],
                                   corpus_pages=cpage['pages'],
                                   corpus_per_page=cpage['per_page'],
                                   corpus_fresh=corpus_fresh)
        finally:
            c.close()

    @app.route('/latest-targets')
    def latest_targets():
        kind = request.args.get('kind', '')
        c = cx()
        try:
            sql = ('SELECT changes.*, programs.name AS pname, programs.platform, '
                   'programs.upstream_listed FROM changes '
                   'LEFT JOIN programs ON programs.id=changes.program_id WHERE 1=1')
            args2 = []
            if kind:
                sql += ' AND kind=?'
                args2.append(kind)
            sql += ' ORDER BY id DESC LIMIT 200'
            rows = c.execute(sql, args2).fetchall()
            return render_template('targets.html', rows=rows, kind=kind)
        finally:
            c.close()

    @app.route('/platforms')
    def platforms():
        c = cx()
        try:
            stats = c.execute(
                   '''SELECT platform, COUNT(*) n, SUM(CASE WHEN vdp=1 THEN 1 ELSE 0 END) vdp_n,
                   MAX(bounty_max) max_b,
                   COUNT(*) FILTER (WHERE first_seen > date('now','-7 days')) new7,
                   COUNT(*) FILTER (WHERE first_seen > date('now','-30 days')) new30
                   FROM programs GROUP BY platform ORDER BY n DESC''').fetchall()
            return render_template('platforms.html', stats=stats)
        finally:
            c.close()

    @app.route('/api/programs')
    def api_programs():
        c = cx()
        try:
            rows = query_programs(
                c, q=request.args.get('q', ''), platform=request.args.get('platform', ''),
                tag=request.args.get('tag', ''), vdp_mode=request.args.get('vdp', 'paid'),
                sort=request.args.get('sort', 'newest'), tier=request.args.get('tier', ''),
                show=request.args.get('show', 'active'), contest=request.args.get('contest', ''))
            return jsonify({'count': len(rows), 'programs': rows})
        finally:
            c.close()

    @app.route('/api/changes')
    def api_changes():
        c = cx()
        try:
            rows = [dict(r) for r in c.execute(
                'SELECT * FROM changes ORDER BY id DESC LIMIT 200').fetchall()]
            return jsonify({'count': len(rows), 'changes': rows})
        finally:
            c.close()

    @app.route('/health')
    def health():
        return jsonify({'ok': True, 'ts': datetime.now(timezone.utc).isoformat()})

    @app.route('/tags')
    def tags_index():
        import json as _json
        from collections import Counter
        c = cx()
        try:
            cnt = Counter()
            for r in c.execute('SELECT tags FROM programs').fetchall():
                try:
                    for t in _json.loads(r['tags'] or '[]'):
                        cnt[t] += 1
                except Exception:
                    pass
            return render_template('tags.html',
                                   tags=sorted(cnt.items(), key=lambda x: (-x[1], x[0])))
        finally:
            c.close()

    @app.route('/export.txt')
    def export_txt():
        from flask import Response
        args = request.args
        c = cx()
        try:
            n = _int(args.get('n', 50), 50, 1, 50)
            rows = query_programs(
                c, q=args.get('q', ''), platform=args.get('platform', ''),
                tag=args.get('tag', ''), vdp_mode=args.get('vdp', 'paid'),
                sort=args.get('sort', 'newest'), tier=args.get('tier', ''),
                show=args.get('show', 'active'), contest=args.get('contest', ''), limit=n)[:n]
            body = '\n'.join('%s — %s' % (r['name'], r['url']) for r in rows) + '\n'
            return Response(body, mimetype='text/plain')
        finally:
            c.close()

    @app.route('/api/tags')
    def api_tags():
        import json as _json
        from collections import Counter
        c = cx()
        try:
            cnt = Counter()
            for r in c.execute('SELECT tags FROM programs').fetchall():
                try:
                    for t in _json.loads(r['tags'] or '[]'):
                        cnt[t] += 1
                except Exception:
                    pass
            return jsonify({'count': len(cnt), 'tags': dict(cnt)})
        finally:
            c.close()

    @app.route('/api/status')
    def api_status():
        c = cx()
        try:
            n = c.execute('SELECT COUNT(*) v FROM programs').fetchone()['v']
            t = c.execute('SELECT COUNT(*) v FROM targets WHERE in_scope=1').fetchone()['v']
            return jsonify({'ok': True, 'programs': n, 'live_targets': t,
                            'scrapes': last_refresh(c)})
        finally:
            c.close()

    @app.route('/api/programs/<int:pid>/targets')
    def api_program_targets(pid):
        c = cx()
        try:
            r = c.execute('SELECT * FROM programs WHERE id=?', (pid,)).fetchone()
            if not r:
                return jsonify({'error': 'not found'}), 404
            p = row_program(r)
            tg = [dict(x) for x in c.execute(
                'SELECT * FROM targets WHERE program_id=? ORDER BY eligible DESC, identity',
                (pid,)).fetchall()]
            return jsonify({'program': p, 'count': len(tg), 'targets': tg})
        finally:
            c.close()

    @app.route('/api/scan', methods=['POST'])
    def api_scan():
        import scanhook
        data = request.get_json(force=True, silent=True) or {}
        try:
            pid = int(data.get('program_id', 0))
        except (TypeError, ValueError):
            return jsonify({'error': 'program_id must be an integer'}), 400
        identity = (data.get('identity') or '').strip()[:300]
        mode = (data.get('mode') or '').strip().lower() or None
        if mode is not None and mode not in scanhook.MODES:
            return jsonify({'error': 'mode must be one of: %s' % ', '.join(scanhook.MODES)}), 400
        if not identity:
            return jsonify({'error': 'identity required'}), 400
        if mode == 'cves' and scanhook.normalize_cve_identity(identity) is None:
            return jsonify({'error': 'cve sweep needs a GitHub owner or owner/repo — '
                             '"%s" is not GitHub-shaped' % identity}), 400
        if not scanhook.scan_command():
            return jsonify({'error': 'no scanner found (set LEAK_SCAN_CMD)'}), 503
        manual = bool(data.get('manual'))
        sid, err = scanhook.start_scan(app.config['DB_PATH'], pid, identity, mode, manual)
        if err == 'unknown program':
            return jsonify({'error': err}), 404
        if err:
            return jsonify({'error': err}), 400
        return jsonify({'ok': True, 'scan_id': sid, 'status': 'pending',
                        'mode': mode or scanhook.DEFAULT_MODE}), 202

    @app.route('/api/scans')
    def api_scans():
        import scanhook
        c = cx()
        try:
            pid = request.args.get('program_id', '')
            if not pid.isdigit():
                return jsonify({'error': 'program_id required'}), 400
            return jsonify({'scans': scanhook.list_scans(c, int(pid))})
        finally:
            c.close()

    @app.route('/api/cves')
    def api_cves():
        """CVE rows for a program: scanner-attributed + one page of corpus
        hits (?page=N&per_page=M, newest first, honest totals)."""
        import scanhook
        import cvecorpus
        c = cx()
        try:
            pid = request.args.get('program_id', '')
            if not pid.isdigit():
                return jsonify({'error': 'program_id required'}), 400
            r = c.execute('SELECT name FROM programs WHERE id=?', (int(pid),)).fetchone()
            if not r:
                return jsonify({'error': 'unknown program'}), 404
            try:
                page = cvecorpus.corpus_cves_page(
                    r['name'], page=request.args.get('page', 1, type=int),
                    per_page=request.args.get('per_page', 25, type=int))
            except Exception:
                page = {'total': 0, 'page': 1, 'per_page': 25, 'pages': 0, 'rows': []}
            return jsonify({'cves': scanhook.list_cves(c, int(pid)),
                            'corpus': page['rows'], 'corpus_total': page['total'],
                            'corpus_page': page['page'], 'corpus_pages': page['pages']})
        finally:
            c.close()

    @app.route('/scans')
    def scans_hub():
        import scanhook
        c = cx()
        try:
            scans = scanhook.list_scans_all(c, limit=80)
        finally:
            c.close()
        running = sum(1 for s in scans if s['status'] == 'running')
        queued = sum(1 for s in scans if s['status'] == 'pending')
        return render_template('scans.html', scans=scans, running=running,
                               queued=queued, refresh_running=_refresh_running)

    @app.route('/leakscan')
    def leakscan():
        import leaksweep
        import scanhook
        c = cx()
        try:
            scans = scanhook.list_scans_all(c, limit=60)
        finally:
            c.close()
        withfind = [s for s in scans if (s.get('findings') or 0) > 0]
        st = leaksweep.state()
        running = sum(1 for s in scans if s['status'] == 'running')
        return render_template('leakscan.html', st=st, scans=scans,
                               withfind=withfind, running=running,
                               refresh_running=_refresh_running)

    @app.route('/api/leaksweep', methods=['GET', 'POST'])
    def api_leaksweep():
        import leaksweep
        import scanhook
        if request.method == 'GET':
            return jsonify(leaksweep.state())
        d = request.get_json(force=True, silent=True) or {}
        action = (d.get('action') or '').lower()
        if action == 'start':
            if not scanhook.scan_command():
                return jsonify({'error': 'no scanner found (set LEAK_SCAN_CMD)'}), 503
            st = leaksweep.start(app.config['DB_PATH'])
            return jsonify({'ok': True, 'state': st}), 202
        if action == 'stop':
            return jsonify({'ok': True, 'state': leaksweep.stop()})
        if action == 'include_out_scope':
            val = bool(d.get('value', True))
            leaksweep.set_include_out_scope(val)
            return jsonify({'ok': True, 'include_out_scope': val})
        return jsonify({'error': 'action must be start|stop|include_out_scope'}), 400

    @app.route('/api/scan/<int:sid>/findings')
    def api_scan_findings(sid):
        import scanhook
        c = cx()
        try:
            r = c.execute('SELECT raw, findings, findings_json FROM scans WHERE id=?',
                          (sid,)).fetchone()
        finally:
            c.close()
        if not r:
            return jsonify({'error': 'unknown scan'}), 404
        rows = []
        if r['findings_json']:
            try:
                rows = json.loads(r['findings_json'])
            except Exception:
                rows = []
        if not rows:
            rows = scanhook.findings_from_raw(r['raw'])
        return jsonify({'count': r['findings'] or 0, 'findings': rows})

    @app.route('/api/liveness', methods=['POST'])
    def api_liveness():
        import keyliveness
        d = request.get_json(force=True, silent=True) or {}
        raw_sid = d.get('scan_id')
        if raw_sid is None or not str(raw_sid).strip().isdigit():
            return jsonify({'error': 'scan_id required'}), 400
        sid = int(str(raw_sid).strip())
        rpc = d.get('rpc') or None
        try:
            verdicts = keyliveness.check_scan(app.config['DB_PATH'], sid, rpc=rpc)
        except Exception as e:
            return jsonify({'error': str(e)}), 500
        summary = {}
        for v in verdicts.values():
            summary[v['verdict']] = summary.get(v['verdict'], 0) + 1
        return jsonify({'ok': True, 'checked': len(verdicts), 'summary': summary,
                        'verdicts': verdicts})

    @app.route('/api/scans/all')
    def api_scans_all():
        import scanhook
        c = cx()
        try:
            return jsonify({'scans': scanhook.list_scans_all(c, limit=80)})
        finally:
            c.close()

    @app.route('/api/refresh', methods=['POST'])
    def api_refresh():
        """Kick the live scrapers in the background. 202; 409 if already running."""
        global _refresh_running
        with _refresh_lock:
            if _refresh_running:
                return jsonify({'ok': False, 'running': True}), 409
            _refresh_running = True

        def work():
            global _refresh_running
            try:
                n = refresh_once(live=True)
                import db as _dbmod
                _cxp = _dbmod.connect()
                try:
                    _dbmod.prune_history(_cxp)
                finally:
                    _cxp.close()
                log.info('manual refresh: %d new programs', n)
            except Exception:
                log.exception('manual refresh failed')
            finally:
                # Corpus freshness rides the same button: best-effort git pull
                # on the CVE corpora so the CVE section stays current.
                try:
                    import cvecorpus
                    pulls = cvecorpus.refresh_corpus()
                    log.info('corpus refresh: %s', pulls)
                except Exception:
                    log.exception('corpus refresh failed')
                with _refresh_lock:
                    _refresh_running = False

        threading.Thread(target=work, daemon=True).start()
        return jsonify({'ok': True, 'running': True, 'started': True}), 202

    @app.route('/api/refresh/status')
    def api_refresh_status():
        c = cx()
        try:
            return jsonify({'running': _refresh_running, 'scrapes': last_refresh(c)})
        finally:
            c.close()

    @app.route('/about')
    def about():
        return render_template('about.html')

    @app.route('/api')
    def api_docs():
        return render_template('api.html')

    @app.route('/llms.txt')
    def llms():
        from flask import Response
        c = cx()
        try:
            n = c.execute('SELECT COUNT(*) v FROM programs').fetchone()['v']
            plats = [r['platform'] for r in
                     c.execute('SELECT DISTINCT platform FROM programs ORDER BY 1').fetchall()]
            top = c.execute('SELECT name, platform, bounty_max FROM programs '
                            'ORDER BY bounty_max DESC LIMIT 20').fetchall()
            lines = ['# CRV recon', '',
                     'Free login-free bug bounty program radar (%d programs).' % n,
                     'Platforms: %s.' % ', '.join(plats), '',
                     '## Top bounties']
            lines += ['- %s (%s, max %s)' % (r['name'], r['platform'], r['bounty_max']) for r in top]
            lines += ['', 'API: /api/programs, /api/programs/<id>/targets, /api/changes, /api/tags, /api/status.',
                      'Export: /export.txt?n=50 with feed filters. Confirm scope upstream before testing.',
                      'Refresh (agent-accessible): POST /api/refresh → 202 kick live scrapers in background',
                      '(409 if already running); GET /api/refresh/status → {running, scrapes}.',
                      'Scans (agent-accessible): POST /api/scan {program_id, identity, mode: light|full|sniper}',
                      '→ 202 {scan_id}; GET /api/scans?program_id=<id> for results (status, summary, raw).',
                      'Scanner: crv-leak-scan (auto-detected; override with LEAK_SCAN_CMD). sniper mode expects a GitHub owner/repo or owner; light/full expect a local path.']
            return Response('\n'.join(lines) + '\n', mimetype='text/plain')
        finally:
            c.close()

    @app.errorhandler(404)
    def not_found(_e):
        return render_template('404.html'), 404

    @app.errorhandler(500)
    def boom(e):
        log.exception('request failed: %s', e)
        return render_template('500.html'), 500

    return app


def refresh_once(db_path=None, live=False, seed=False):
    """One ingest pass: optionally hit live scrapers. Seed samples are
    opt-in only (never in prod paths) — pass seed=True explicitly."""
    cx = db.connect(db_path) if db_path else db.connect()
    db.init_db(db_path) if db_path else db.init_db()
    total_new = 0
    if seed:
        total_new += ingest(cx, seed_mod.fetch(), 'seed')[0]
        log.info('seeded %d sample programs', total_new)
    if live:
        from scrapers import (hackenproof, yeswehack, hackerone, bugcrowd, immunefi,
                              cantina, sherlock, code4rena, codehawks, intigriti, bbradar)
        for name, fn in (('bbradar', bbradar.fetch),
                         ('hackenproof', hackenproof.fetch_all),
                         ('yeswehack', yeswehack.fetch),
                         ('hackerone', hackerone.fetch_all),
                         ('bugcrowd', bugcrowd.fetch_all),
                         ('immunefi', immunefi.fetch),
                         ('cantina', cantina.fetch),
                         ('sherlock', sherlock.fetch),
                         ('code4rena', code4rena.fetch),
                         ('codehawks', codehawks.fetch),
                         ('intigriti', intigriti.fetch)):
            try:
                n, _ = ingest(cx, fn(), name)
                total_new += n
                log.info('live scrape %s: %d new', name, n)
            except Exception as e:  # live lanes must never kill the loop
                db.record_scrape(cx, name, 0, 'error: %s' % e)
                log.warning('live scrape %s failed: %s', name, e)
    return total_new


def prune_seed(db_path=None):
    """Remove sample rows (seed=1, plus legacy example.com rows from before
    the seed flag existed) once live data has taken over."""
    cx = db.connect(db_path) if db_path else db.connect()
    n = cx.execute("DELETE FROM programs WHERE seed=1 OR url LIKE '%example.com%'").rowcount
    cx.execute("DELETE FROM targets WHERE program_id NOT IN (SELECT id FROM programs)")
    cx.commit()
    log.info('pruned %d seed programs', n)
    return n


def refresh_guarded(db_path=None, live=False):
    """Single-flight refresh: shared by the manual button and the scheduler so
    they never race. Returns (started, rows_new). started=False if one is
    already running."""
    global _refresh_running
    with _refresh_lock:
        if _refresh_running:
            return False, 0
        _refresh_running = True
    try:
        n = refresh_once(db_path, live=live)
        return True, n
    finally:
        with _refresh_lock:
            _refresh_running = False


def start_scheduler(app, minutes=30, live=False):
    def loop():
        while True:
            time.sleep(minutes * 60)
            try:
                started, n = refresh_guarded(app.config['DB_PATH'], live=live)
                if started:
                    log.info('scheduled refresh: %d new', n)
                    if live:
                        from tracker import auto_deep
                        cxd = db.connect(app.config['DB_PATH'])
                        try:
                            got = auto_deep(cxd)
                            import db as _dbmod
                            _dbmod.prune_history(cxd)
                        finally:
                            cxd.close()
                        log.info('scheduled auto-deep: %s', got)
                else:
                    log.info('scheduled refresh skipped: manual refresh running')
            except Exception:
                log.exception('scheduled refresh failed')
    t = threading.Thread(target=loop, daemon=True)
    t.start()
    return t


_deep_inflight = set()
_deep_lock = threading.Lock()


def maybe_lazy_deep(db_path, platform, slug, program_id):
    """Queue a one-shot scope fill when a scopeless detail page is viewed."""
    if platform not in ('hackenproof', 'bugcrowd'):
        return False
    with _deep_lock:
        if program_id in _deep_inflight:
            return False
        _deep_inflight.add(program_id)

    def work():
        try:
            from tracker import deep_one
            cxd = db.connect(db_path)
            try:
                deep_one(cxd, platform, slug)
            finally:
                cxd.close()
        except Exception:
            log.exception('lazy deep fill failed for %s/%s', platform, slug)
        finally:
            with _deep_lock:
                _deep_inflight.discard(program_id)

    threading.Thread(target=work, daemon=True).start()
    return True


def serve(application, port=5000):
    try:
        from waitress import serve as _serve
        log.info('serving with waitress on 127.0.0.1:%d', port)
        _serve(application, host='127.0.0.1', port=port)
    except ImportError:
        log.info('waitress missing, using flask dev server on 127.0.0.1:%d', port)
        application.run(host='127.0.0.1', port=port)


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('--live', action='store_true', help='also run live scrapers on boot')
    ap.add_argument('--port', type=int, default=5000)
    a = ap.parse_args()
    db.init_db()
    refresh_once(live=a.live)
    application = create_app()
    start_scheduler(application, live=a.live)
    import leaksweep
    leaksweep.maybe_resume(application.config['DB_PATH'])
    serve(application, port=a.port)
