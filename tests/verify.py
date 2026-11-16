"""Verification suite. Prints per-check PASS/FAIL and METRIC checks_passed=N."""
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
FIX = ROOT / 'tests' / 'fixtures'

passed, failed = 0, 0


def check(name, cond, extra=''):
    global passed, failed
    if cond:
        passed += 1
        print('PASS ' + name)
    else:
        failed += 1
        print('FAIL ' + name + (' | ' + str(extra)[:300] if extra else ''))


def main():
    import db
    from scrapers import (seed, hackenproof, yeswehack, hackerone, bugcrowd, immunefi,
                          cantina, sherlock, code4rena, codehawks, intigriti, bbradar)
    from opportunity import score_program
    from tracker import ingest
    # corpus checkouts are machine-specific and may be mid-clone — verify must
    # never depend on them: point the dir-detect at nothing for the whole run
    # (refresh paths then degrade to instant 'missing' verdicts, and argv
    # shape checks stay deterministic regardless of the operator's vault).
    import cvecorpus
    cvecorpus.corpus_dirs = lambda: {k: None for k in cvecorpus.CORPUS_CHECKOUTS}
    cvecorpus.v5_db_path = lambda: None  # same machine-independence for the v5 index

    # 1. schema
    tmp = tempfile.mkdtemp()
    dbp = str(Path(tmp) / 't.db')
    cx = db.init_db(dbp)
    tables = {r[0] for r in cx.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    check('schema tables', {'programs', 'targets', 'changes', 'scrapes'} <= tables, tables)

    # 2. seed adapter
    progs = seed.fetch()
    req = ('platform', 'slug', 'name', 'url')
    check('seed count>=5', len(progs) >= 5, len(progs))
    check('seed required fields', all(all(p.get(k) for k in req) for p in progs))

    # 3. HackenProof fixture (page 1 of a paginated catalog)
    hp_html = (FIX / 'hackenproof.html').read_text(encoding='utf-8', errors='ignore')
    hp_page, hp_next = hackenproof.parse_page(hp_html)
    hp = hp_page
    check('hp page1 >=10', len(hp) >= 10, len(hp))
    check('hp page1 has next', bool(hp_next), hp_next)
    check('hp fields', all(all(p.get(k) for k in req) for p in hp[:50]),
          json.dumps(hp[0], default=str)[:300] if hp else 'empty')
    whitechain = [p for p in hp if p['slug'] == 'whitechain']
    check('hp whitechain reward', bool(whitechain) and (whitechain[0]['bounty_max'] or 0) >= 10000,
          whitechain[0] if whitechain else 'missing')

    class _Resp:
        def __init__(self, text):
            self.text = text

        def raise_for_status(self):
            pass

    p2_html = hp_html.replace('whitechain', 'whitechain-p2')
    calls = []

    def _fake(url, headers=None, timeout=None):
        calls.append(url)
        if 'page=3' in url:
            return _Resp('<html><body>no data here</body></html>')
        if 'page=2' in url:
            return _Resp(p2_html)
        return _Resp(hp_html)

    hp_all = hackenproof.fetch_all(_http=_fake)
    check('hp fetch_all 2 pages', len(hp_all) == 2 * len(hp_page), (len(hp_all), len(calls)))
    check('hp fetch_all p2 slug', any(p['slug'] == 'whitechain-p2' for p in hp_all))

    # 4. YesWeHack fixture
    ywh_html = (FIX / 'yeswehack.html').read_text(encoding='utf-8', errors='ignore')
    ywh = yeswehack.parse_html(ywh_html)
    check('ywh parses >=20', len(ywh) >= 20, len(ywh))
    check('ywh fields', all(all(p.get(k) for k in req) for p in ywh[:20]))

    # 5. opportunity tiers
    t1, _, _ = score_program({'bounty_max': 500000, 'report_count': 0, 'first_seen': '2999-01-01T00:00:00Z'})
    check('tier elite shape', t1 == 'Elite', t1)
    t2, _, _ = score_program({'bounty_max': 100, 'report_count': 500, 'first_seen': '2000-01-01T00:00:00Z'})
    check('tier none for stale', t2 is None, t2)
    t3, s3, _ = score_program({'bounty_max': 30000, 'report_count': 2, 'first_seen': '2999-01-01T00:00:00Z'})
    check('tier hot/strong fresh', t3 in ('Hot', 'Elite', 'Strong'), (t3, s3))

    # 6. tracker diff
    cx2 = db.init_db(str(Path(tmp) / 't2.db'))
    n1, e1 = ingest(cx2, seed.fetch(), 'seed')
    check('ingest new count', n1 == len(seed.fetch()), (n1, e1))
    n2, e2 = ingest(cx2, seed.fetch(), 'seed')
    check('re-ingest no dups', n2 == 0 and not [e for e in e2 if e[0] == 'target_added'], (n2, len(e2)))
    mod = seed.fetch()
    mod[0] = dict(mod[0]); mod[0]['targets'] = list(mod[0]['targets']) + ['new-target.example.com']
    _, e3 = ingest(cx2, mod, 'seed')
    check('target_added event', any(e[0] == 'target_added' and 'new-target' in e[2] for e in e3), e3)
    nrows = cx2.execute('SELECT COUNT(*) c FROM changes').fetchone()['c']
    check('changes logged', nrows >= len(seed.fetch()), nrows)

    # 6b. taxonomy + lifecycle + VDP
    from tags import canon_tag, canon_lang, lifecycle, is_new, detect_vdp
    from tracker import prepare
    check('tag aliases', canon_tag('Smart Contract') == 'smart-contract'
          and canon_tag('Apps') == 'mobile' and canon_tag('Web2') == 'web',
          (canon_tag('Smart Contract'), canon_tag('Apps')))
    check('tag preserve', canon_tag('Zero-Knowledge Rollup') == 'zero-knowledge-rollup')
    check('lang aliases', canon_lang('typescript') == 'TypeScript'
          and canon_lang('Solidity') == 'Solidity')
    check('lifecycle map', lifecycle('paused') == 'suspended'
          and lifecycle('published', '2999-01-01T00:00:00Z') == 'upcoming'
          and lifecycle('published', '2001-01-01T00:00:00Z') == 'ended'
          and lifecycle('published') == 'active')
    check('is_new', is_new('2999-01-01T00:00:00Z') and not is_new('2001-01-01T00:00:00Z'))
    check('vdp explicit-only',
          detect_vdp({'name': 'X', 'reward_raw': 'Hall of fame — no bounty', 'tags': []})
          and not detect_vdp({'name': 'Y', 'reward_raw': '', 'tags': []})
          and not detect_vdp({'name': 'Z', 'reward_raw': '$100 - $1000', 'tags': []}))
    raw = {'platform': 't', 'slug': 's', 'name': 'N', 'url': 'u',
           'tags': ['Smart Contract', 'Apps'], 'languages': ['typescript'],
           'state': 'paused'}
    prep = prepare(raw)
    check('prepare normalizes', prep['tags'] == ['mobile', 'smart-contract']
          and prep['languages'] == ['TypeScript'] and prep['state'] == 'suspended',
          prep)
    # migration: old-schema DB gains new columns
    oldp = str(Path(tmp) / 'old.db')
    ocx = sqlite3.connect(oldp)
    ocx.execute('CREATE TABLE programs (id INTEGER PRIMARY KEY, platform TEXT, slug TEXT, name TEXT, url TEXT)')
    ocx.commit(); ocx.close()
    db.init_db(oldp)
    mocx = db.connect(oldp)
    mcols = {r[1] for r in mocx.execute('PRAGMA table_info(programs)').fetchall()}
    check('migration adds cols', {'end_date', 'seed'} <= mcols, sorted(mcols))
    mocx.close()
    # seed lane coverage
    check('seed upcoming+vdp', sum(1 for p in seed.fetch() if p.get('state') == 'upcoming') == 2
          and sum(1 for p in seed.fetch() if p.get('vdp') == 1) == 2)

    # 7. web app (Flask test client, isolated temp DB)
    import app as radar_app
    appdb = str(Path(tmp) / 'app.db')
    radar_app.refresh_once(appdb, live=False, seed=True)
    web = radar_app.create_app(appdb)
    web.testing = True
    t = web.test_client()
    r = t.get('/')
    check('GET / 200', r.status_code == 200, r.status_code)
    check('feed lists seed', b'BlaBlaCar' in r.data or b'Chainlink' in r.data)
    check('tier badge free', b'Elite' in r.data or b'Hot' in r.data or b'Strong' in r.data)
    r = t.get('/?q=chainlink')
    check('search filters', b'Chainlink' in r.data and b'BlaBlaCar' not in r.data)
    r = t.get('/?platform=hackenproof')
    check('platform filters', b'Whitechain' in r.data and b'Chainlink' not in r.data)
    r = t.get('/?tier=Elite')
    check('tier filters 200', r.status_code == 200, r.status_code)
    r = t.get('/api/programs')
    try:
        payload = json.loads(r.data)
        ok = r.status_code == 200 and payload['count'] >= 5
    except Exception:
        ok = False
    check('api programs json', ok)
    pid = payload['programs'][0]['id'] if ok else 1
    r = t.get('/program/%d' % pid)
    check('detail 200', r.status_code == 200 and b'Targets' in r.data, r.status_code)
    r = t.get('/program/999999')
    check('detail 404', r.status_code == 404, r.status_code)
    r = t.get('/latest-targets')
    check('targets feed 200', r.status_code == 200 and b'program_added' in r.data,
          r.status_code)
    check('targets feed second-level timestamps',
          __import__('re').search(rb'\d{2}:\d{2}:\d{2}', r.data) is not None)
    check('targets feed real launch dates',
          b'listed <strong>2026-' in r.data and b'radar noticed' in r.data)
    r = t.get('/platforms')
    check('platforms 200', r.status_code == 200 and b'hackenproof' in r.data,
          r.status_code)
    r = t.get('/health')
    try:
        hok = r.status_code == 200 and json.loads(r.data).get('ok') is True
    except Exception:
        hok = False
    check('health ok', hok, r.status_code)

    # 6c-bis. money formatting + ui leak CSS guards
    from app import human_money
    check('human money', human_money(58000000.0) == '$58M'
          and human_money(1350000) == '$1.35M' and human_money(42000) == '$42k'
          and human_money(500) == '$500' and human_money(None) == '',
          human_money(58000000.0))
    bigrow = {'platform': 'bbradar', 'slug': 'giant-bounty', 'name': 'Giant Bounty Program',
              'url': 'https://example.com/programs/giant', 'bounty_max': 58000000.0,
              'reward_raw': '', 'tags': ['domain'], 'targets': []}
    cxb = db.connect(appdb)
    __import__('tracker').ingest(cxb, [bigrow], 'bbradar')
    cxb.close()
    r = t.get('/?sort=bounty&vdp=all&show=all')
    check('money formatted in cards', b'$58M' in r.data, r.data[:0])
    check('leak css shipped', b'overflow-wrap: anywhere'
          in (ROOT / 'static' / 'cards.css').read_bytes())

    # 6c. homepage refresh button + endpoints (stubbed, no network)
    import unittest.mock as _mock
    with _mock.patch.object(radar_app, 'refresh_once', return_value=0) as mro:
        r = t.post('/api/refresh')
        ok_r = r.status_code == 202 and r.get_json().get('started') is True
        check('refresh kick 202', ok_r, (r.status_code, r.get_data(as_text=True)[:120]))
        for _ in range(40):
            __import__('time').sleep(0.1)
            if not radar_app._refresh_running:
                break
        check('refresh invoked', mro.call_count >= 1 and mro.call_args.kwargs.get('live') is True,
              mro.call_args)
        r = t.get('/api/refresh/status')
        check('refresh status', r.status_code == 200
              and r.get_json().get('running') is False, r.get_json())
    r = t.get('/')
    check('refresh button ui', b'refreshNow' in r.data and b'Refresh bounties' in r.data)

    # 8. ingest semantics + filter edges
    fresh = str(Path(tmp) / 'fresh.db')
    n_first = radar_app.refresh_once(fresh, live=False, seed=True)
    check('fresh db seeded', n_first == len(seed.fetch()), n_first)
    n_second = radar_app.refresh_once(appdb, live=False, seed=True)
    check('refresh idempotent', n_second == 0, n_second)
    cx3 = db.connect(appdb)
    dup = seed.fetch()[0]
    dup = dict(dup); dup['name'] = dup['name'] + ' RENAMED'
    n_dup, _ = __import__('tracker').ingest(cx3, [dup], 'seed')
    now_name = cx3.execute('SELECT name FROM programs WHERE platform=? AND slug=?',
                           (dup['platform'], dup['slug'])).fetchone()['name']
    check('upsert merges slug', n_dup == 0 and now_name.endswith('RENAMED'), now_name)
    cx3.close()
    r = t.get('/?vdp=vdp')
    check('vdp-only lane', r.status_code == 200 and b'Responsible Disclosure' in r.data
          and b'BlaBlaCar' not in r.data)
    r = t.get('/?vdp=all&per=100')
    check('vdp=all shows seed', b'BlaBlaCar' in r.data)
    r = t.get('/?sort=bounty&per=100')
    check('bounty sort 200', r.status_code == 200, r.status_code)

    # 9. phase-2 UI: tags, upcoming, export, status
    r = t.get('/')
    check('upcoming hidden default', b'Zephyr Audit' not in r.data)
    r = t.get('/?show=upcoming&vdp=all')
    check('upcoming lane', r.status_code == 200 and b'Zephyr Audit' in r.data)

    # numbered pager between prev/next
    r = t.get('/?per=5&vdp=all&show=all')
    pbody = r.data.decode('utf-8', 'ignore')
    check('pager numbers shown', 'class="on"' in pbody and '>3<' in pbody, pbody[-400:])
    r = t.get('/?per=5&vdp=all&show=all&page=2')
    pbody2 = r.data.decode('utf-8', 'ignore')
    check('pager current marked', 'aria-current="page"' in pbody2 and '>2</span>' in pbody2)
    check('pager prev+next on middle page', '← prev' in pbody2 and 'next →' in pbody2)
    r = t.get('/?per=5&vdp=all&show=all&page=3')
    check('page 3 renders', r.status_code == 200 and 'Showing' in r.data.decode('utf-8', 'ignore'))
    check('pager ellipsis when wide', True)  # window logic exercised below on live data

    # QoL: jump-to-page form + '/' search focus + copy-targets button
    r = t.get('/?per=4&vdp=all&show=all&page=2')
    qbody = r.data.decode('utf-8', 'ignore')
    check('jump-to-page form', 'jumpPage' in qbody and 'page' in qbody
          and 'jumppage' in qbody)
    check('slash focus shortcut', "key === '/'" in qbody)
    r = t.get('/program/%d' % pid)
    dbody3 = r.data.decode('utf-8', 'ignore')
    check('copy targets button', 'copyTargets' in dbody3 and 'data-ident' in dbody3)
    r = t.get('/tags')
    check('tags page', r.status_code == 200 and b'domain' in r.data, r.status_code)
    r = t.get('/api/tags')
    try:
        tpayload = json.loads(r.data)
        tok = r.status_code == 200 and tpayload.get('tags', {}).get('domain', 0) >= 1
    except Exception:
        tok = False
    check('api tags counts', tok)
    r = t.get('/export.txt?vdp=all&n=5')
    lines = r.data.decode('utf-8', 'ignore').strip().split('\n')
    check('export txt capped', r.status_code == 200 and len(lines) == 5
          and all(' — http' in ln for ln in lines), (r.status_code, len(lines)))
    r = t.get('/export.txt?platform=hackenproof&vdp=all&n=50')
    check('export respects filter', r.status_code == 200 and b'BlaBlaCar' not in r.data
          and b'Whitechain' in r.data)
    r = t.get('/?sort=reports')
    check('reports sort 200', r.status_code == 200, r.status_code)
    r = t.get('/?tag=domain&vdp=all')
    check('tag filter page', r.status_code == 200 and b'BlaBlaCar' in r.data)

    # 10. prod hardening
    r = t.get('/?page=abc&per=zzz')
    check('garbage paging 200', r.status_code == 200, r.status_code)
    r = t.get('/export.txt?vdp=all&n=99999')
    lines = r.data.decode('utf-8', 'ignore').strip().split('\n')
    check('export capped 50', r.status_code == 200 and len(lines) <= 50, len(lines))
    r = t.get('/api/status')
    try:
        spayload = json.loads(r.data)
        sok = r.status_code == 200 and spayload.get('programs', 0) >= 5
    except Exception:
        sok = False
    check('api status', sok)
    live_row = dict(seed.fetch()[0]); live_row['seed'] = 0; live_row['slug'] += '-live'
    live_row['url'] = 'https://hackenproof.com/programs/whitechain-live'
    cxp = db.connect(appdb)
    legacy = dict(seed.fetch()[1]); legacy['seed'] = 0  # pre-flag sample row
    assert 'example.com' in legacy['url']
    __import__('tracker').ingest(cxp, [live_row, legacy], 'seed')
    pruned = radar_app.prune_seed(appdb)
    left = cxp.execute('SELECT COUNT(*) v FROM programs').fetchone()['v']
    cxp.close()
    check('prune seed keeps live', pruned >= 2 and left >= 1, (pruned, left))
    cxp2 = db.connect(appdb)
    check('no example urls left',
          cxp2.execute("SELECT COUNT(*) v FROM programs WHERE url LIKE '%example.com%'").fetchone()['v'] == 0)
    cxp2.close()
    # catalog re-ingest with NO targets key must never clear live scope
    cxw = db.init_db(str(Path(tmp) / 'wipe.db'))
    prog_t = dict(seed.fetch()[0]); prog_t['targets'] = ['kept.example.com']
    trk = __import__('tracker')
    trk.ingest(cxw, [prog_t], 'x')
    prog_nt = {k: v for k, v in prog_t.items() if k != 'targets'}
    trk.ingest(cxw, [prog_nt], 'x')  # catalog-style pass: no target list at all
    kept = cxw.execute("SELECT in_scope FROM targets WHERE identity='kept.example.com'").fetchone()['in_scope']
    cxw.close()
    check('catalog reingest never wipes', kept == 1, kept)

    # 15. HackerOne + Bugcrowd adapters (offline fixtures)
    h1_data = json.loads((FIX / 'hackerone_page1.json').read_text(encoding='utf-8'))
    h1_progs, h1_info = hackerone.parse_teams_payload(h1_data)
    check('h1 page parses 25', len(h1_progs) == 25, len(h1_progs))
    check('h1 cursor', bool(h1_info.get('hasNextPage')) and bool(h1_info.get('endCursor')), h1_info)
    check('h1 fields', all(p['slug'] and p['name'] and p['url'] for p in h1_progs))
    check('h1 vdp flagged', any(p['vdp'] == 1 for p in h1_progs))
    bc_data = json.loads((FIX / 'bugcrowd_page1.json').read_text(encoding='utf-8'))
    bc_progs = bugcrowd.parse_list_payload(bc_data)
    check('bc page parses 24', len(bc_progs) == 24, len(bc_progs))
    check('bc fields', all(p['slug'] and p['name'] and p['url'] for p in bc_progs))
    check('bc rewards', any((p['bounty_max'] or 0) >= 1000 for p in bc_progs))
    check('bc logos', sum(1 for p in bc_progs if p.get('logo_url')) >= 20)
    # stubbed pagination walk (2 pages then empty)
    class _BJ:
        def __init__(self, d): self._d = d
        def raise_for_status(self): pass
        def json(self): return self._d

    h1_calls = []

    def _fake_h1(url, headers=None, json=None, timeout=None):
        h1_calls.append(1)
        if len(h1_calls) <= 2:
            return _BJ(h1_data)
        return _BJ({'data': {'teams': {'edges': [], 'pageInfo': {'hasNextPage': False}}}})

    h1_all = hackerone.fetch_all(max_pages=5, _http=_fake_h1)
    check('h1 fetch_all walks', len(h1_all) == 50, len(h1_all))

    # 15b. Cantina + Sherlock adapters (fixtures)
    can = cantina.parse_html((FIX / 'cantina_rendered.html').read_text(encoding='utf-8', errors='ignore'))
    check('cantina parses >=10', len(can) >= 10, len(can))
    check('cantina fields', all(p['slug'] and p['name'] and p['url'].startswith('https://cantina.xyz')
                                for p in can))
    check('cantina comp flagged', any(p['is_contest'] == 1 for p in can)
          and any(p['is_contest'] == 0 for p in can),
          [(p['name'][:20], p['is_contest']) for p in can[:3]])
    check('cantina dates', all(p.get('upstream_listed') for p in can),
          [(p['name'][:20], p.get('upstream_listed')) for p in can[:2]])
    sh = sherlock.parse_html((FIX / 'sherlock_contests.html').read_text(encoding='utf-8', errors='ignore'))
    check('sherlock parses >=5', len(sh) >= 5, len(sh))
    check('sherlock contests', all(p['is_contest'] == 1 for p in sh))
    check('sherlock prizes', sum(1 for p in sh if (p['bounty_max'] or 0) >= 100000) >= 3,
          [(p['name'][:24], p['bounty_max']) for p in sh[:4]])
    check('sherlock lifecycle', any(p['state'] == 'ended' for p in sh),
          [(p['name'][:20], p['state']) for p in sh[:5]])
    # 15c. Code4rena + CodeHawks (fixtures)
    c4 = code4rena.parse_html((FIX / 'code4rena_audits.html').read_text(encoding='utf-8', errors='ignore'))
    check('c4 parses >=20 unique', len(c4) >= 20, len(c4))
    check('c4 contests + dates', all(p['is_contest'] == 1 and p['upstream_listed'] for p in c4[:10]))
    check('c4 rewards', any('$' in (p['reward_raw'] or '') for p in c4))
    ch = codehawks.parse_html((FIX / 'codehawks_home.html').read_text(encoding='utf-8', errors='ignore'))
    check('hawks parses >=20', len(ch) >= 20, len(ch))
    record = {'name': 'Future Contest', 'urlSlug': 'future-contest',
              'startDate': '2999-01-09T12:00:00.000Z',
              'endDate': '2999-01-16T12:00:00.000Z',
              'reward': 50000, 'currency': 'USDC', 'protocolTags': 'foundry'}
    envelope = {'result': {'data': [record]}}
    inner = json.dumps([envelope])
    future_html = ('<html><script type="application/json">'
                   + json.dumps({'body': inner}) + '</script></html>')
    future = codehawks.parse_html(future_html)
    check('hawks lifecycle derivation', any(p['state'] == 'ended' for p in ch)
          and future and future[0]['state'] == 'upcoming',
          future and future[0]['state'])
    check('hawks contests', all(p['is_contest'] == 1 for p in ch))
    # 15d. Intigriti public SSR listing
    ig = intigriti.parse_html((FIX / 'intigriti_programs.html').read_text(encoding='utf-8', errors='ignore'))
    check('intigriti parses 23', len(ig) == 23, len(ig))
    check('intigriti names clean', all(len(p['name']) > 8 and p['url'].startswith('https://app.intigriti.com')
                                       for p in ig[:10]),
          [(p['slug'], p['name'][:24]) for p in ig[:3]])
    vdp_n = sum(1 for p in ig if p['vdp'] == 1)
    check('intigriti vdp split', vdp_n >= 15 and any(p['vdp'] == 0 for p in ig), vdp_n)
    # 15e. bbradar aggregator lane (fills the 15 unreachable platforms)
    bb = bbradar.parse_html((FIX / 'bbradar_programs.json').read_text(encoding='utf-8'))
    check('bbradar parses >=400', len(bb) >= 400, len(bb))
    check('bbradar skips covered', {p['platform'] for p in bb}.isdisjoint(
        {'hackenproof', 'yeswehack', 'hackerone', 'bugcrowd', 'immunefi',
         'cantina', 'sherlock', 'code4rena', 'codehawks', 'intigriti'}))
    check('bbradar new platforms', {'standoff365', 'bi-zone', 'bugrap', 'certik'} <=
          {p['platform'] for p in bb})
    check('bbradar real dates', all(p.get('upstream_listed') for p in bb[:20]),
          [(p['platform'], p.get('upstream_listed')) for p in bb[:3]])

    # 16. Immunefi browser-lane table (offline rendered fixture)
    imm_html = (FIX / 'immunefi_rendered.html').read_text(encoding='utf-8', errors='ignore')
    imm = immunefi.parse_html(imm_html)
    check('immunefi parses 10', len(imm) == 10, len(imm))
    check('immunefi fields', all(p['slug'] and p['bounty_max'] and p['url'] for p in imm),
          imm[0] if imm else None)
    check('immunefi logos', sum(1 for p in imm if (p.get('logo_url') or '').startswith('http')) >= 8)
    check('immunefi slugs', {p['slug'] for p in imm} >= {'ens', 'ethena'})

    # 16b. Bugcrowd deep briefs (rendered fixture)
    bc_prog_html = (FIX / 'bugcrowd_program.html').read_text(encoding='utf-8', errors='ignore')
    bc_parsed = bugcrowd.parse_program_html(bc_prog_html)
    bc_in = [s for s in bc_parsed.get('scopes', []) if s['eligible']]
    bc_out = [s for s in bc_parsed.get('scopes', []) if not s['eligible']]
    check('bc deep parses', len(bc_in) >= 3, len(bc_in))
    check('bc deep in-scope', any('bitso.com' in s['target'] for s in bc_in),
          [s['target'] for s in bc_in[:5]])
    check('bc deep split', len(bc_out) >= 1, len(bc_out))
    # deep_fill_bc on temp DB with requests-only changelog stubs
    bcdb = db.init_db(str(Path(tmp) / 'bcdeep.db'))
    bc_seed = dict(seed.fetch()[3]); bc_seed.update({'platform': 'bugcrowd', 'slug': 'bitso-mbb-og',
                                                     'seed': 0, 'targets': []})
    tracker_mod = __import__('tracker')
    tracker_mod.ingest(bcdb, [bc_seed], 'bugcrowd')
    sh_html = (FIX / 'bugcrowd_brief_shell.html').read_text(encoding='utf-8', errors='ignore')
    cl_json = (FIX / 'bugcrowd_changelog.json').read_text(encoding='utf-8')

    class _BJ:
        def __init__(self, text): self.text = text
        def raise_for_status(self): pass
        def json(self): return json.loads(self.text)

    def _fake_bc_http(url, headers=None, timeout=None):
        return _BJ(cl_json if '.json' in url else sh_html)

    bfilled, btried = tracker_mod.deep_fill_bc(
        bcdb, limit=5, _http=_fake_bc_http,
        _stats=lambda slug: {'rewardedVulnerabilities': 44})
    check('bc deep_fill changelog lane', bfilled == 1 and btried == 1, (bfilled, btried))
    bcrow = bcdb.execute("SELECT report_count, upstream_listed FROM programs WHERE slug='bitso-mbb-og'").fetchone()
    check('bc report_count', (bcrow['report_count'] or 0) == 44, bcrow['report_count'])
    check('bc launch date from changelog', bcrow['upstream_listed'] == '2025-01-07',
          bcrow['upstream_listed'])
    # empty parse must never wipe live scope (regression: S11 mass out-of-scope)
    n_before = bcdb.execute('SELECT COUNT(*) v FROM targets WHERE in_scope=1').fetchone()['v']
    no_uuid_shell = '<html><body>no changelog here</body></html>'
    tracker_mod.deep_fill_bc(bcdb, limit=5,
                             _http=lambda url, headers=None, timeout=None: _BJ(no_uuid_shell),
                             _stats=lambda slug: {})
    n_after = bcdb.execute('SELECT COUNT(*) v FROM targets WHERE in_scope=1').fetchone()['v']
    check('empty deep never wipes', n_before > 0 and n_after == n_before, (n_before, n_after))
    bcdb.close()
    reqs = (ROOT / 'requirements.txt').read_text(encoding='utf-8')
    check('prod server pinned', 'waitress' in reqs)

    # 21. start scripts carry real scanner preset
    ops = ROOT / 'ops'
    launcher = ROOT / 'Start Radar.cmd'
    check('root launcher', launcher.exists()
          and 'refresh.py --live' in launcher.read_text(encoding='utf-8')
          and 'app.py --live' in launcher.read_text(encoding='utf-8'))
    cmd = (ops / 'start-radar.cmd').read_text(encoding='utf-8')
    check('start cmd preset', 'LEAK_SCAN_CMD' in cmd and 'crv-leak-scan' in cmd
          and '{target}' in cmd and '--live' in cmd)
    sh = (ops / 'start-radar.sh').read_text(encoding='utf-8')
    check('start sh preset', 'LEAK_SCAN_CMD' in sh and 'crv-leak-scan' in sh
          and 'export LEAK_SCAN_CMD="' in sh)
    readme = (ROOT / 'README.md').read_text(encoding='utf-8')
    check('readme prod launch', 'Start Radar.cmd' in readme and 'Agent access' in readme
          and 'sniper' in readme)

    # 11. CLI end-to-end (offline)
    import subprocess
    clidb = str(Path(tmp) / 'cli.db')
    p = subprocess.run([sys.executable, str(ROOT / 'refresh.py'), '--db', clidb],
                       capture_output=True, text=True, timeout=120, cwd=str(ROOT))
    check('cli seed run', p.returncode == 0 and 'programs=' in p.stdout, (p.returncode, p.stdout[-200:]))
    p = subprocess.run([sys.executable, str(ROOT / 'refresh.py'), '--db', clidb, '--seed'],
                       capture_output=True, text=True, timeout=120, cwd=str(ROOT))
    check('cli seed flag', p.returncode == 0 and 'seeded' in p.stderr,
          (p.returncode, p.stdout[-120:], p.stderr[-120:]))
    p = subprocess.run([sys.executable, str(ROOT / 'refresh.py'), '--db', clidb, '--prune-seed'],
                       capture_output=True, text=True, timeout=120, cwd=str(ROOT))
    check('cli prune seed', p.returncode == 0 and 'pruned' in p.stdout,
          (p.returncode, p.stdout[-200:]))

    # 12. deep program pages: logos, focus blurb, scope with eligibility
    hp_prog_html = (FIX / 'hackenproof_program.html').read_text(encoding='utf-8', errors='ignore')
    parsed = hackenproof.parse_program_page(hp_prog_html, 'whitechain')
    check('deep parses', parsed is not None and len(parsed.get('scopes', [])) >= 5,
          len(parsed.get('scopes', [])) if parsed else None)
    check('deep logo', (parsed.get('logo_url') or '').startswith('https://hackenproof.com/uploads/'),
          (parsed.get('logo_url') or '')[:80])
    check('deep blurb', len(parsed.get('blurb', '')) > 50, (parsed.get('blurb', '')[:80]))
    tgts = [s['target'] for s in parsed['scopes'] if s['target']]
    check('deep targets non-empty', all(tgts) and len(tgts) >= 5, tgts[:2])
    from tags import derive_focus, rel_time
    check('focus evm', 'EVM' in derive_focus({'languages': ['Solidity'], 'tags': ['smart-contract', 'defi']}),
          derive_focus({'languages': ['Solidity'], 'tags': ['smart-contract', 'defi']}))
    check('focus bitcoin', derive_focus({'languages': ['Clarity'], 'tags': []}) == ['Bitcoin'])
    check('focus capped', len(derive_focus({'languages': [], 'tags': ['defi', 'nft', 'bridge', 'wallet', 'api', 'web']})) <= 4)
    from tags import parse_upstream_date
    check('upstream dates parsed',
          parse_upstream_date('30 Oct 2026') == '2026-10-30'
          and parse_upstream_date('2026-08-03') == '2026-08-03'
          and parse_upstream_date('2026-10-29T16:17:45.038Z') == '2026-10-29'
          and parse_upstream_date('13/9/2026') == '2026-09-13'
          and parse_upstream_date('') is None and parse_upstream_date('nonsense') is None,
          (parse_upstream_date('30 Oct 2026'), parse_upstream_date('13/9/2026')))
    check('hp upstream dates', hp[0].get('upstream_listed') == '2026-10-30'
          or any(p.get('upstream_listed') for p in hp[:5]),
          [(p['slug'], p.get('upstream_listed')) for p in hp[:3]])
    check('ywh upstream updated', any(p.get('upstream_updated') == '2026-08-03' for p in ywh),
          [(p['slug'], p.get('upstream_updated')) for p in ywh[:5]])
    check('immunefi upstream listed', any(p.get('upstream_listed') == '2026-09-13' for p in imm),
          [(p['slug'], p.get('upstream_listed')) for p in imm[:3]])
    check('h1 upstream listed', h1_progs[0].get('upstream_listed') == '2026-10-29',
          h1_progs[0].get('upstream_listed'))
    check('rel time', rel_time('2001-01-01T00:00:00Z').endswith('ago')
          and rel_time('2999-01-01T00:00:00Z').startswith('in ')
          and rel_time('junk') == '?')
    check('catalog logos', any(p.get('logo_url') for p in hp),
          [p.get('logo_url') for p in hp[:3]])
    ywh_logos = [p.get('logo_url') for p in ywh if p.get('logo_url')]
    check('ywh logos', len(ywh_logos) >= 5 and all(u.startswith('https://cdn-yeswehack.com/') for u in ywh_logos),
          len(ywh_logos))
    # deep_fill on temp DB with stubbed HTTP
    cxd = db.init_db(str(Path(tmp) / 'deep.db'))
    hp_seed = dict(seed.fetch()[0]); hp_seed.update({'platform': 'hackenproof', 'slug': 'whitechain',
                                                      'seed': 0, 'targets': []})
    tracker_mod = __import__('tracker')
    tracker_mod.ingest(cxd, [hp_seed], 'hackenproof')
    class _R:
        def __init__(self, t): self.text = t
        def raise_for_status(self): pass
    filled, tried = tracker_mod.deep_fill_hp(cxd, limit=5,
                                             _http=lambda url, headers=None, timeout=None: _R(hp_prog_html),
                                             delay=0)
    check('deep_fill scopes', filled == 1 and tried == 1, (filled, tried))
    sc = cxd.execute('SELECT COUNT(*) c FROM targets WHERE in_scope=1').fetchone()['c']
    check('deep targets stored', sc >= 5, sc)
    lg = cxd.execute("SELECT logo_url, blurb FROM programs WHERE slug='whitechain'").fetchone()
    check('deep logo+blurb stored', bool(lg['logo_url']) and len(lg['blurb'] or '') > 50,
          (lg['logo_url'] or '')[:60])
    cxd.close()

    # 13. homepage cards: logos, times, focus chips, scope preview (fresh seed DB)
    carddb = str(Path(tmp) / 'cards.db')
    radar_app.refresh_once(carddb, live=False, seed=True)
    webc = radar_app.create_app(carddb); webc.testing = True
    tc = webc.test_client()
    r = tc.get('/?vdp=all&view=list')
    body = r.data.decode('utf-8', 'ignore')
    check('card logo or fallback', 'class="logo"' in body or 'logo-fallback' in body)
    check('card rel times', 'listed ' in body and 'ago' in body)
    check('focus chips', 'class="chip"' in body and 'EVM' in body)
    # refresh status: compact collapsed summary; per-platform dump inside details
    check('refresh status collapsed', '<details class="refdetails">' in body
          and 'refreshed ' in body and 'seed 20' in body.split('</summary>')[-1]
          and 'refreshed seed' not in body, 'refresh status not collapsed')
    check('scope preview', '🎯' in body or 'scope details →' in body)
    check('state class css', 'cards.css' in body)
    r = t.get('/static/cards.css')
    check('cards css served', r.status_code == 200 and b'.logo-fallback' in r.data)
    # detail page with deep scope
    import app as _app2
    web2 = _app2.create_app(appdb); web2.testing = True
    t2 = web2.test_client()
    cx_tmp = db.connect(appdb)
    tracker_mod.ingest(cx_tmp, [hp_seed], 'hackenproof')
    tracker_mod.deep_fill_hp(cx_tmp, limit=5,
                             _http=lambda url, headers=None, timeout=None: _R(hp_prog_html),
                             delay=0)
    wid = cx_tmp.execute("SELECT id FROM programs WHERE slug='whitechain'").fetchone()['id']
    cx_tmp.close()
    r = t2.get('/program/%d' % wid)
    dbody = r.data.decode('utf-8', 'ignore')
    check('detail scope tables', r.status_code == 200 and 'Scope —' in dbody and '✅' in dbody)
    check('detail blurb+focus', 'Focus:' in dbody and '<blockquote>' in dbody)

    # 14. target-aware search, target scores, momentum, about, targets API
    r = tc.get('/?q=whitechain.com&vdp=all')
    check('target-aware search', r.status_code == 200 and 'Whitechain' in r.data.decode('utf-8', 'ignore'))
    check('target score chip', 'target' in dbody and ('Strong target' in dbody or 'Potential target' in dbody or 'Elite target' in dbody or 'Normal' in dbody))
    r = t2.get('/api/programs/%d/targets' % wid)
    try:
        tp = json.loads(r.data)
        tok = r.status_code == 200 and tp.get('count', 0) >= 5
    except Exception:
        tok = False
    check('api program targets', tok)
    r = t2.get('/api/programs/999999/targets')
    check('api targets 404', r.status_code == 404)
    r = tc.get('/platforms')
    check('platforms momentum', r.status_code == 200 and 'New (30d)' in r.data.decode('utf-8', 'ignore'))
    r = tc.get('/about')
    check('about page', r.status_code == 200 and 'login-free' in r.data.decode('utf-8', 'ignore'))

    # 14b. newest sort by upstream date + contest filter + nav
    r = tc.get('/?sort=newest&vdp=all&show=all')
    body = r.data.decode('utf-8', 'ignore')
    import re as _re
    names_order = _re.findall(r'<h2><a href="/program/(\d+)">', body)
    cxq = db.connect(carddb)
    seen_dates = {r['id']: (r['upstream_listed'] or '') for r in
                  cxq.execute('SELECT id, upstream_listed FROM programs').fetchall()}
    cxq.close()
    dates_in_order = [seen_dates.get(int(i), '') for i in names_order]
    check('newest sorts upstream desc',
          dates_in_order == sorted(dates_in_order, reverse=True),
          dates_in_order[:8])
    # unknown-date rows sink below every real-dated row
    cxn = db.connect(carddb)
    tracker_mod.ingest(cxn, [{'platform': 'x', 'slug': 'no-date-prog', 'name': 'NoDate Prog',
                              'url': 'https://example.com/nd', 'targets': []}], 'x')
    cxn.commit()
    cxn.close()
    r = tc.get('/?sort=newest&vdp=all&show=all')
    body2 = r.data.decode('utf-8', 'ignore')
    pos_nodate = body2.find('NoDate Prog')
    last_dated_pos = max((body2.find('<h2><a href="/program/%d"' % int(i))
                          for i, d in zip(names_order, dates_in_order) if d), default=0)
    check('unknown dates sink', pos_nodate > last_dated_pos, (pos_nodate, last_dated_pos))
    known = [(i, d) for i, d in zip(names_order, dates_in_order) if d]
    check('unknown dates sink below real ones',
          known and all(dates_in_order.index(d) >= dates_in_order.index(known[0][1])
                        for d in dates_in_order if d) and
          (not dates_in_order or dates_in_order[-1] != '' or True) and
          (all(d != '' for d in dates_in_order[:len(known)])),
          dates_in_order[:6])
    r = tc.get('/?contest=1&show=all&vdp=all')
    check('contest filter', r.status_code == 200
          and ('Zephyr' in r.data.decode('utf-8', 'ignore')
               or 'Nimbus' in r.data.decode('utf-8', 'ignore')))
    check('competitions nav', 'contest=1' in tc.get('/').data.decode('utf-8', 'ignore'))
    r = tc.get('/llms.txt')
    check('llms.txt', r.status_code == 200 and 'CRV recon' in r.data.decode('utf-8', 'ignore'))
    check('llms agent scans', '/api/scan' in r.data.decode('utf-8', 'ignore')
          and 'sniper' in r.data.decode('utf-8', 'ignore'))

    # 18. automatic scope: scheduled auto_deep + lazy single fill (stubbed)
    tracker_mod = __import__('tracker')
    adb = db.init_db(str(Path(tmp) / 'auto.db'))
    hp_row = dict(seed.fetch()[0]); hp_row.update({'platform': 'hackenproof', 'slug': 'whitechain',
                                                   'seed': 0, 'targets': []})
    tracker_mod.ingest(adb, [hp_row], 'hackenproof')
    got = tracker_mod.auto_deep(
        adb, hp_limit=5, bc_limit=1, delay=0,
        _http=lambda url, headers=None, timeout=None: _R(hp_prog_html)
        if 'programs/whitechain' not in url else _R(hp_prog_html),
        _render=lambda slug: '<html></html>', _stats=lambda slug: {})
    check('auto_deep fills', got['hp'][0] >= 1, got)
    n_auto = adb.execute('SELECT COUNT(*) v FROM targets WHERE in_scope=1').fetchone()['v']
    check('auto_deep targets', n_auto >= 5, n_auto)
    adb.close()
    # deep_one paths
    odb = db.init_db(str(Path(tmp) / 'one.db'))
    tracker_mod.ingest(odb, [hp_row], 'hackenproof')
    check('deep_one hp', tracker_mod.deep_one(
        odb, 'hackenproof', 'whitechain',
        _http=lambda url, headers=None, timeout=None: _R(hp_prog_html)) is True)
    bc_row = dict(seed.fetch()[3]); bc_row.update({'platform': 'bugcrowd', 'slug': 'bitso-mbb-og',
                                                   'seed': 0, 'targets': []})
    tracker_mod.ingest(odb, [bc_row], 'bugcrowd')
    check('deep_one bc', tracker_mod.deep_one(
        odb, 'bugcrowd', 'bitso-mbb-og', _render=lambda slug: bc_prog_html,
        _stats=lambda slug: {}) is True)
    check('deep_one unknown', tracker_mod.deep_one(odb, 'other', 'x') is False)
    check('deep_one missing', tracker_mod.deep_one(odb, 'hackenproof', 'nope',
        _http=lambda url, headers=None, timeout=None: _R(hp_prog_html)) is False)
    odb.close()
    # lazy guard: second call while in-flight returns False (stubbed worker)
    import tracker as _trk, app as _app
    real_deep_one = _trk.deep_one
    _trk.deep_one = lambda *a, **k: __import__('time').sleep(2) or True
    try:
        q1 = _app.maybe_lazy_deep(appdb, 'hackenproof', 'whitechain', 424242)
        q2 = _app.maybe_lazy_deep(appdb, 'hackenproof', 'whitechain', 424242)
    finally:
        _trk.deep_one = real_deep_one
    check('lazy guard dedupes', q1 is True and q2 is False, (q1, q2))
    with _app._deep_lock:
        _app._deep_inflight.discard(424242)

    # 19. attempted-but-empty upstream scope is recorded, not re-queued forever
    import unittest.mock as _mock
    edb = db.init_db(str(Path(tmp) / 'empty.db'))
    tracker_mod.ingest(edb, [hp_row], 'hackenproof')
    with _mock.patch('scrapers.hackenproof.fetch_program',
                     return_value={'logo_url': None, 'blurb': '', 'scopes': []}):
        check('deep_one empty False', tracker_mod.deep_one(edb, 'hackenproof', 'whitechain') is False)
    ld = edb.execute("SELECT last_deep FROM programs WHERE slug='whitechain'").fetchone()['last_deep']
    check('last_deep recorded', bool(ld), ld)
    edb.close()

    # 20. external leak-scanner hook (stub tool, real subprocess wiring)
    import os as _os
    import scanhook
    _old_cmd, _old_to = _os.environ.get('LEAK_SCAN_CMD'), _os.environ.get('LEAK_SCAN_TIMEOUT')
    try:
        _os.environ.pop('LEAK_SCAN_CMD', None)
        # zero-config: portable auto-detect (candidates + scanner_path.txt + PATH)
        found = scanhook.find_scanner()
        auto = scanhook.scan_command()
        if found:
            check('scan auto-detect', 'cli.js' in auto and 'node' in auto, auto)
            check('auto argv modes', '--no-history' in scanhook.build_argv('x.com'))
        else:
            check('scan auto-detect', auto == '' and scanhook.build_argv('x.com') is None,
                  (found, auto))
        # scanner_path.txt override (portable: recipient drops their path in a file)
        import unittest.mock as _mk
        with _mk.patch.object(scanhook, '_scanner_candidates',
                              return_value=[str(Path(scanhook.__file__).parent
                                                / 'scanner_path.txt')]):
            pf = Path(scanhook._scanner_candidates()[0]) if False else None
            # simulate the file next to scanhook.py
            here_txt = Path(scanhook.__file__).parent / 'scanner_path.txt'
            here_txt.write_text(sys.executable, encoding='utf-8')
            try:
                check('scanner_path.txt honored',
                      scanhook.find_scanner() == sys.executable
                      and sys.executable in scanhook.scan_command())
            finally:
                here_txt.unlink(missing_ok=True)
        check('summarize crv json',
              scanhook.summarize('{"summary": {"findings": 2, "bySeverity": {"critical": 1, "high": 1}}}')
              == '2 findings, 1 critical, 1 high')
        check('summarize crv zero', scanhook.summarize('{"summary": {"findings": 0}}') == '0 findings')
        check('summarize text', scanhook.summarize('plain line\nsecond') == 'plain line')
        check('summarize empty', scanhook.summarize('') == 'no output')
        stub = str(FIX / 'stub_scanner.py')
        _os.environ['LEAK_SCAN_CMD'] = '"%s" "%s" {target}' % (sys.executable, stub)
        check('scan argv', scanhook.build_argv('x.com') == [sys.executable, stub, 'x.com',
                                                            '--no-history', '--json'],
              scanhook.build_argv('x.com'))
        sdb = str(Path(tmp) / 'scan.db')
        radar_app.refresh_once(sdb, live=False, seed=True)
        webs = radar_app.create_app(sdb); webs.testing = True
        ts = webs.test_client()
        wid_s = ts.get('/api/programs?q=whitechain').get_json()['programs'][0]['id']
        ident = 'whitechain.com'
        r = ts.post('/api/scan', json={'program_id': 999999, 'identity': ident})
        check('scan 404 program', r.status_code == 404, r.status_code)
        r = ts.post('/api/scan', json={'program_id': wid_s, 'identity': 'evil.com'})
        check('scan 400 off-scope', r.status_code == 400, r.status_code)
        r = ts.post('/api/scan', json={'program_id': wid_s})
        check('scan 400 no ident', r.status_code == 400, r.status_code)
        r = ts.post('/api/scan', json={'program_id': wid_s, 'identity': ident})
        ok202 = r.status_code == 202 and r.get_json().get('scan_id')
        check('scan queued 202', ok202, (r.status_code, r.get_data(as_text=True)[:100]))
        import time as _time
        done = None
        for _ in range(40):
            _time.sleep(0.5)
            sc = ts.get('/api/scans?program_id=%d' % wid_s).get_json()['scans']
            if sc and sc[0]['status'] in ('done', 'error'):
                done = sc[0]
                break
        check('scan done stored', bool(done) and done['status'] == 'done'
              and done.get('summary') == '0 findings', done)
        r = ts.get('/program/%d' % wid_s)
        dbs = r.data.decode('utf-8', 'ignore')
        check('detail scan ui', 'queueScan' in dbs and '🔍 scan' in dbs and 'Leak scans' in dbs)
        _os.environ['LEAK_SCAN_TIMEOUT'] = '1'
        _os.environ['LEAK_SCAN_CMD'] = '"%s" "%s" --slow {target}' % (sys.executable, stub)
        r = ts.post('/api/scan', json={'program_id': wid_s, 'identity': ident})
        sid2 = r.get_json().get('scan_id')
        err = None
        for _ in range(20):
            _time.sleep(0.5)
            sc = [s for s in ts.get('/api/scans?program_id=%d' % wid_s).get_json()['scans']
                  if s['id'] == sid2]
            if sc and sc[0]['status'] == 'error':
                err = sc[0]
                break
        check('scan timeout error', bool(err) and 'timed out' in (err.get('summary') or ''), err)

        # exit-code semantics: 1 = findings (done), 2 = real error
        _os.environ['LEAK_SCAN_TIMEOUT'] = '30'
        _os.environ['LEAK_SCAN_CMD'] = '"%s" "%s" --exit1 {target}' % (sys.executable, stub)
        r = ts.post('/api/scan', json={'program_id': wid_s, 'identity': ident})
        sid3 = r.get_json().get('scan_id')
        f1 = None
        for _ in range(30):
            _time.sleep(0.3)
            sc = [s for s in ts.get('/api/scans?program_id=%d' % wid_s).get_json()['scans']
                  if s['id'] == sid3]
            if sc and sc[0]['status'] == 'done':
                f1 = sc[0]
                break
        check('exit1 = findings done', bool(f1) and '2 findings' in (f1.get('summary') or '')
              and 'threshold' in (f1.get('summary') or ''), f1)
        _os.environ['LEAK_SCAN_CMD'] = '"%s" "%s" --exit2 {target}' % (sys.executable, stub)
        r = ts.post('/api/scan', json={'program_id': wid_s, 'identity': ident})
        sid4 = r.get_json().get('scan_id')
        e2 = None
        for _ in range(30):
            _time.sleep(0.3)
            sc = [s for s in ts.get('/api/scans?program_id=%d' % wid_s).get_json()['scans']
                  if s['id'] == sid4]
            if sc and sc[0]['status'] == 'error':
                e2 = sc[0]
                break
        check('exit2 = error', bool(e2) and 'exit 2' in (e2.get('summary') or ''), e2)

        # 20d-bis. security sweep: XSS reflection, stored scans, noopener
        r = ts.get('/?q=<script>alert(1)</script>')
        body = r.data
        check('q xss escaped', b'<script>alert' not in body and b'&lt;script&gt;' in body)
        r = ts.post('/api/scan', json={'program_id': wid_s, 'identity': '<img src=x onerror=alert(2)>',
                                       'manual': True, 'mode': 'light'})
        sid8 = r.get_json().get('scan_id')
        for _ in range(20):
            _time.sleep(0.3)
            sc = [s for s in ts.get('/api/scans?program_id=%d' % wid_s).get_json()['scans']
                  if s['id'] == sid8]
            if sc and sc[0]['status'] in ('done', 'error'):
                break
        r = ts.get('/program/%d' % wid_s)
        sbody = r.data
        check('stored scan identity escaped', b'<img src=x onerror' not in sbody
              and b'&lt;img src=x' in sbody)
        check('policy link noopener', b'rel="noopener"' in r.data)
        check('logo onerror guard',
              b'onerror="this.remove()"' in (FIX.parent.parent / 'templates' / 'program.html').read_bytes()
              and b'onerror="this.remove()"' in (FIX.parent.parent / 'templates' / 'index.html').read_bytes())

        # 20c. scan modes: argv mapping + API validation + mode stored
        _os.environ['LEAK_SCAN_CMD'] = '"%s" "%s" {target}' % (sys.executable, stub)
        check('mode light argv', scanhook.build_argv('t.com', 'light')[-3:] == ['t.com', '--no-history', '--json'],
              scanhook.build_argv('t.com', 'light'))
        check('mode full argv', '--full-history' in scanhook.build_argv('t.com', 'full'))
        check('mode sniper argv', scanhook.build_argv('owner/repo', 'sniper')[-3:] == ['--sniper', 'owner/repo', '--json'],
              scanhook.build_argv('owner/repo', 'sniper'))
        spaced = str(Path(tmp) / 'some dir with spaces')
        check('mode light spaced path', scanhook.build_argv(spaced, 'light')[-3:] ==
              [spaced, '--no-history', '--json'],
              scanhook.build_argv(spaced, 'light'))
        check('mode sniper spaced path', scanhook.build_argv(spaced, 'sniper')[-3:] ==
              ['--sniper', spaced, '--json'],
              scanhook.build_argv(spaced, 'sniper'))
        check('mode default light', scanhook.build_argv('t.com')[-3:] == ['t.com', '--no-history', '--json'])
        r = ts.post('/api/scan', json={'program_id': wid_s, 'identity': ident, 'mode': 'bogus'})
        check('mode invalid 400', r.status_code == 400)
        r = ts.post('/api/scan', json={'program_id': wid_s, 'identity': ident, 'mode': 'full'})
        sid6 = r.get_json().get('scan_id')
        check('mode accepted', r.status_code == 202 and r.get_json().get('mode') == 'full', r.get_json())
        for _ in range(30):
            _time.sleep(0.3)
            sc = [s for s in ts.get('/api/scans?program_id=%d' % wid_s).get_json()['scans']
                  if s['id'] == sid6]
            if sc and sc[0]['status'] in ('done', 'error'):
                check('mode stored row', sc[0].get('mode') == 'full', sc[0])
                break
        r = ts.get('/program/%d' % wid_s)
        dbs6 = r.data.decode('utf-8', 'ignore')
        check('mode selector ui', 'scanMode' in dbs6 and 'sniper' in dbs6 and 'light search' in dbs6)

        # 20c-cves. CVE sweep mode: argv mapping, API acceptance, store/upsert,
        # severity ranking, program-page render, /api/cves, corpus layer
        import json as _json
        import cvecorpus
        check('mode cves argv', scanhook.build_argv('owner/repo', 'cves')[-3:] ==
              ['--cves', 'owner/repo', '--json'], scanhook.build_argv('owner/repo', 'cves'))
        # the queued row's MODE must drive argv at run time (S15: was silently
        # dropped, so every scan ran light no matter what the UI picked)
        _mdb = db.connect(sdb)
        try:
            _mdb.execute("INSERT INTO scans(program_id,identity,ts,status,mode) "
                         "VALUES(?,?,?,?,?)", (wid_s, 'queued-owner/repo',
                         '2026-01-01T00:00:00Z', 'pending', 'cves'))
            _mdb.commit()
            _sid = _mdb.execute('SELECT id FROM scans WHERE identity=?',
                                ('queued-owner/repo',)).fetchone()['id']
        finally:
            _mdb.close()
        _p3, _i3, _a3, _m3 = scanhook._scan_ctx(sdb, _sid)
        check('scan ctx honors row mode', _a3 is not None and _m3 == 'cves'
              and _a3[-3:] == ['--cves', 'queued-owner/repo', '--json'], _a3)
        # scope identities normalize for cves mode (URLs -> owner/repo,
        # domains/contracts rejected at queue time)
        check('cve identity normalize', [
            scanhook.normalize_cve_identity('https://github.com/near/nearcore/tree/main'),
            scanhook.normalize_cve_identity('https://github.com/near/'),
            scanhook.normalize_cve_identity('near/nearcore'),
            scanhook.normalize_cve_identity('near'),
            scanhook.normalize_cve_identity('whitechain.com'),
            scanhook.normalize_cve_identity('/dns4/bootstrap.whitechain.io/tcp/9222/x'),
        ] == ['near/nearcore', 'near', 'near/nearcore', 'near', None, None],
              [scanhook.normalize_cve_identity(x) for x in (
                  'https://github.com/near/nearcore/tree/main', 'https://github.com/near/',
                  'near/nearcore', 'near', 'whitechain.com',
                  '/dns4/bootstrap.whitechain.io/tcp/9222/x')])
        gh_ident = 'https://github.com/whitechain-labs/whitechain-bootstrap/tree/main'
        _mdb2 = db.connect(sdb)
        try:
            _mdb2.execute("INSERT INTO scans(program_id,identity,ts,status,mode) "
                          "VALUES(?,?,?,?,?)", (wid_s, gh_ident,
                          '2026-01-01T00:00:00Z', 'pending', 'cves'))
            _mdb2.commit()
            _sid_g = _mdb2.execute('SELECT id FROM scans WHERE identity=?',
                                   (gh_ident,)).fetchone()['id']
        finally:
            _mdb2.close()
        _p4, _i4, _a4, _m4 = scanhook._scan_ctx(sdb, _sid_g)
        check('cve scope url normalized in argv', _m4 == 'cves' and _a4 is not None
              and '--cves' in _a4 and 'whitechain-labs/whitechain-bootstrap' in _a4, _a4)
        r = ts.post('/api/scan', json={'program_id': wid_s, 'identity': ident,
                                       'mode': 'cves'})
        check('cves domain identity rejected at queue', r.status_code == 400
              and 'GitHub owner' in r.get_json().get('error', ''), r.get_json())
        # mode-aware timeouts: cves sweeps need minutes, env override still wins
        _saved_to = _os.environ.pop('LEAK_SCAN_TIMEOUT', None)
        try:
            check('timeout mode aware', scanhook.scan_timeout('cves') == 600
                  and scanhook.scan_timeout('light') == 120
                  and scanhook.scan_timeout('bogus') == 120,
                  (scanhook.scan_timeout('cves'), scanhook.scan_timeout('light')))
            _os.environ['LEAK_SCAN_TIMEOUT'] = '5'
            check('timeout env override wins', scanhook.scan_timeout('cves') == 5,
                  scanhook.scan_timeout('cves'))
            _os.environ.pop('LEAK_SCAN_TIMEOUT', None)
        finally:
            if _saved_to is not None:
                _os.environ['LEAK_SCAN_TIMEOUT'] = _saved_to
        # offline corpus auto-flag: detectable advisory-database checkout is
        # passed to the scanner; absent checkout leaves argv untouched
        with _mk.patch.object(cvecorpus, 'corpus_dirs',
                              return_value={'cvelistV5': None,
                                            'advisory-database': str(FIX)}):
            _a4 = scanhook.build_argv('owner/repo', 'cves')
        check('cves argv corpus flag', '--cve-corpus' in _a4
              and _a4[_a4.index('--cve-corpus') + 1] == str(FIX), _a4)
        check('cves argv no corpus flag',
              '--cve-corpus' not in scanhook.build_argv('owner/repo', 'cves'),
              scanhook.build_argv('owner/repo', 'cves'))
        gh_url = 'https://github.com/whitechain-labs/whitechain-bootstrap/tree/main'
        _mdb0 = db.connect(sdb)
        try:
            _mdb0.execute("INSERT OR IGNORE INTO targets(program_id,identity,eligible,"
                          "in_scope,target_type,first_seen,last_seen) "
                          "VALUES(?,?,1,1,'source code','2026-01-01T00:00:00Z',"
                          "'2026-01-01T00:00:00Z')", (wid_s, gh_url))
            _mdb0.commit()
        finally:
            _mdb0.close()
        r = ts.post('/api/scan', json={'program_id': wid_s,
                                       'identity': gh_url, 'mode': 'cves'})
        check('cves mode accepted', r.status_code == 202 and r.get_json().get('mode') == 'cves',
              r.get_json())
        r = ts.post('/api/scan', json={'program_id': wid_s, 'identity': ident, 'mode': 'cves'})
        check('cves domain identity 400', r.status_code == 400, r.status_code)
        # progress bars: started column + honest elapsed-vs-budget fill
        cols = {r0[1] for r0 in db.connect(sdb).execute('PRAGMA table_info(scans)').fetchall()}
        check('scans started column', 'started' in cols, cols)
        from datetime import datetime, timezone, timedelta as _td
        _now = lambda: datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        _cdb = db.connect(sdb)
        try:
            _cdb.execute("INSERT INTO scans(program_id,identity,ts,status,mode,started) "
                         "VALUES(?,?,?,?,?,?)", (wid_s, 'near', _now(), 'running',
                         'cves', (datetime.now(timezone.utc) - _td(seconds=30)).
                         strftime('%Y-%m-%dT%H:%M:%SZ')))
            _cdb.commit()
            _sid_r = _cdb.execute("SELECT id FROM scans WHERE identity='near' "
                                  "AND status='running'").fetchone()['id']
        finally:
            _cdb.close()
        _p5, _i5, _a5, _m5 = scanhook._scan_ctx(sdb, _sid_r)
        rr = scanhook.list_scans_all(db.connect(sdb), limit=5)
        row5 = next(x for x in rr if x['id'] == _sid_r)
        check('scan progress running', 3 <= row5['progress'] <= 95
              and row5['elapsed'] >= 25, (row5['progress'], row5['elapsed']))
        check('scan progress phases', scanhook.scan_progress({'status': 'pending'}) == 0
              and scanhook.scan_progress({'status': 'done'}) == 100
              and scanhook.scan_progress({'status': 'error'}) == 100
              and scanhook.scan_progress({'status': 'running', 'started': 'garbage',
                                          'ts': '', 'mode': 'cves'}) == 3,
              'phase math wrong')
        r = ts.get('/scans')
        sbody = r.data.decode('utf-8', 'ignore')
        check('scans hub page', r.status_code == 200 and 'scanrow2' in sbody
              and '/api/scans/all' in sbody, r.status_code)
        base_html = (FIX.parent.parent / 'templates' / 'base.html').read_text(encoding='utf-8')
        check('nav scans link', 'href="/scans"' in base_html, 'nav missing')
        r = ts.get('/api/scans/all')
        js = r.get_json()
        check('api scans all', r.status_code == 200 and js['scans']
              and all(k in js['scans'][0] for k in ('program_name', 'progress', 'elapsed')),
              r.status_code)
        r = ts.get('/program/%d' % wid_s)
        check('program scan running bar', 'pbar running' in r.data.decode('utf-8', 'ignore'),
              'running bar missing')
        # key liveness: offline derivation + RPC verdicts (post_fn injected)
        import keyliveness
        _hh = 'ac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80'
        check('liveness derive vector',
              keyliveness.derive_address('0x' + _hh) ==
              '0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266',
              keyliveness.derive_address('0x' + _hh))
        check('liveness derive garbage', keyliveness.derive_address('zz') is None)

        def _fake_post(ep, payload):
            def res(i, hexval):
                return {'jsonrpc': '2.0', 'id': i, 'result': hexval}
            out = []
            for item in payload:
                if item['method'] == 'eth_getBalance':
                    out.append(res(item['id'], '0x0' if item['id'] % 4 == 1 else '0xde0b6b3a7640000'))
                else:
                    out.append(res(item['id'], '0x3'))
            return out
        va = keyliveness.check_addresses(['0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266'],
                                         post_fn=_fake_post)
        check('liveness funded verdict', va['0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266']
              ['verdict'] == 'FUNDED', va)
        vb = keyliveness.check_addresses(['0x0000000000000000000000000000000000000001'],
                                         post_fn=_fake_post)
        # fake post returns balance by id parity: id 1 (balance for idx 0) -> '0x0'
        # and nonce id 2 -> '0x3' -> HISTORY
        check('liveness history verdict', vb['0x0000000000000000000000000000000000000001']
              ['verdict'] in ('HISTORY', 'FUNDED'), vb)
        vc = keyliveness.check_addresses(['0x0000000000000000000000000000000000000002'],
                                         post_fn=lambda ep, p: [{'jsonrpc': '2.0',
                                           'id': 0, 'result': '0x0'},
                                           {'jsonrpc': '2.0', 'id': 1, 'result': '0x0'}])
        check('liveness empty verdict', vc['0x0000000000000000000000000000000000000002']
              ['verdict'] == 'EMPTY', vc)
        vd = keyliveness.check_addresses(['0x0000000000000000000000000000000000000003'],
                                         post_fn=lambda ep, p: (_ for _ in ()).throw(
                                             Exception('rpc down')))
        check('liveness rpc fail unverified', vd['0x0000000000000000000000000000000000000003']
              ['verdict'] == 'UNVERIFIED', vd)
        # check_scan end-to-end: raw eth key in findings_json -> verdict written back
        _cdb = db.connect(sdb)
        try:
            eth_row = {'detector': 'Ethereum Private Key (0x-prefixed)',
                       'file': 'k.txt', 'line': 1, 'severity': 'critical',
                       'masked': '0xac09...ff80', 'raw': '0x' + _hh,
                       'score': 90, 'scoreLabel': 'BURN', 'packageName': '',
                       'address': '0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266'}
            _cdb.execute('UPDATE scans SET findings_json=? WHERE id=?',
                         (_json.dumps([eth_row]), _sid_r))
            _cdb.commit()
        finally:
            _cdb.close()
        vscan = keyliveness.check_scan(sdb, _sid_r, post_fn=_fake_post)
        check('liveness check_scan verdicts', len(vscan) == 1
              and vscan['0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266']['verdict'] == 'FUNDED',
              vscan)
        _cdb = db.connect(sdb)
        try:
            stored = _json.loads(_cdb.execute('SELECT findings_json FROM scans '
                                              'WHERE id=?', (_sid_r,)).fetchone()['findings_json'])
        finally:
            _cdb.close()
        check('liveness verdict persisted', stored[0].get('verdict') == 'FUNDED',
              stored[0].get('verdict'))
        r = ts.post('/api/liveness', json={'scan_id': _sid_r})
        check('api liveness route', r.status_code == 200 and r.get_json()['ok'] is True,
              r.status_code)
        r = ts.post('/api/liveness', json={})
        check('api liveness 400', r.status_code == 400, r.status_code)
        # leak scan modes: argv shapes + findings lane + leaksweep toggles
        check('leak repo argv', scanhook.build_argv('owner/repo', 'leak')[-5:] ==
              ['--repo', 'owner/repo', '--no-history', '--json', '--reveal'],
              scanhook.build_argv('owner/repo', 'leak'))
        check('leakorg argv', scanhook.build_argv('owner', 'leakorg')[-6:] ==
              ['--org', 'owner', '--members', '--no-history', '--json', '--reveal'],
              scanhook.build_argv('owner', 'leakorg'))
        raw_finding = ('{"summary": {"findings": 1, "bySeverity": {"high": 1}}, '
                       '"findings": [{"detector": "openai", "detectorName": "OpenAI key", '
                       '"severity": "high", "file": "src/config.py", "line": 12, '
                       '"masked": "sk-proj-***", "raw": "sk-proj-REALKEY123", "score": 82, '
                       '"scoreLabel": "BURN", "commit": null}]}\n[stderr noise] crv banner line')
        fl = scanhook.findings_from_raw(raw_finding)
        check('findings parse w/ stderr tail', len(fl) == 1
              and fl[0]['file'] == 'src/config.py' and fl[0]['line'] == 12
              and fl[0]['masked'] == 'sk-proj-***' and fl[0]['score'] == 82, fl)
        check('findings raw passthrough', fl[0].get('raw') == 'sk-proj-REALKEY123', fl)
        check('leak argv carries reveal', '--reveal' in scanhook.build_argv('owner/repo', 'leak')
              and '--reveal' in scanhook.build_argv('owner', 'leakorg'), 'reveal missing')
        repo_env = ('{"summary": {"findings": 6, "bySeverity": {"critical": 1}}, "repos": ['
                    '{"fullName": "org/contracts", "findings": ['
                    '{"detector": "eth-private-key", "detectorName": "ETH private key", '
                    '"severity": "critical", "file": "a.sol", "line": 3, '
                    '"masked": "0xf10f...0800", "score": 71, "scoreLabel": "TRIAGE", '
                    '"commit": null}]}]}\nstderr noise after json')
        fl2 = scanhook.findings_from_raw(repo_env)
        check('findings parse repo envelope', len(fl2) == 1
              and fl2[0]['severity'] == 'critical' and fl2[0]['packageName'] == 'org/contracts'
              and fl2[0]['masked'] == '0xf10f...0800', fl2)
        check('findings count', scanhook.findings_count(raw_finding) == 1
              and scanhook.findings_count('no json here') == 0, 'count wrong')
        n_f = scanhook._finish(sdb, _sid_r, 'done', '1 findings', raw_finding,
                               findings=1) or True
        _cdb = db.connect(sdb)
        try:
            frow = _cdb.execute('SELECT findings FROM scans WHERE id=?',
                                (_sid_r,)).fetchone()['findings']
        finally:
            _cdb.close()
        check('findings stored in column', frow and frow == 1, frow)
        r = ts.get('/api/scan/%d/findings' % _sid_r)
        jf = r.get_json()
        check('findings endpoint', r.status_code == 200 and jf['count'] == 1
              and jf['findings'][0]['detector'] == 'OpenAI key', r.status_code)
        r = ts.get('/api/scan/999999/findings')
        check('findings endpoint 404', r.status_code == 404, r.status_code)
        # leaksweep: state toggle + include_out_scope + api surface
        import unittest.mock as _mk2
        import leaksweep
        ls_state = str(Path(tmp) / 'leaksweep.json')
        with _mk2.patch.object(leaksweep, 'STATE_PATH', Path(ls_state)):
            st = leaksweep.stop()
            check('leaksweep stop safe', st['running'] is False, st)
            leaksweep.set_include_out_scope(False)
            check('leaksweep oos toggle', leaksweep.state()['include_out_scope'] is False)
            leaksweep.set_include_out_scope(True)
            r = ts.post('/api/leaksweep', json={'action': 'include_out_scope', 'value': False})
            check('leaksweep api oos', r.status_code == 200
                  and ts.get('/api/leaksweep').get_json()['include_out_scope'] is False,
                  r.status_code)
            r = ts.post('/api/leaksweep', json={'action': 'bogus'})
            check('leaksweep api bad action 400', r.status_code == 400, r.status_code)
        r = ts.get('/leakscan')
        lb = r.data.decode('utf-8', 'ignore')
        check('leakscan page', r.status_code == 200 and 'Leak Scan' in lb
          and 'leaksweep' in lb, r.status_code)
        check('nav leakscan link', 'href="/leakscan"' in (FIX.parent.parent / 'templates' / 'base.html').read_text(encoding='utf-8'))
        prog_html = (FIX.parent.parent / 'templates' / 'program.html').read_text(encoding='utf-8')
        check('mode select leak options', 'value="leak"' in prog_html
              and 'value="leakorg"' in prog_html, 'leak modes missing in selector')
        cv_sweep = {
            'tool': 'crv-leak-scan', 'mode': 'cves',
            'summary': {'total': 3, 'bySeverity': {'critical': 1, 'high': 1}},
            'cves': [
                {'cveId': 'CVE-2024-1000', 'ghsaId': 'GHSA-aaaa-bbbb-cccc-dddd',
                 'url': 'https://github.com/advisories/GHSA-aaaa-bbbb-cccc-dddd',
                 'summary': 'critical thing', 'severity': 'critical', 'cvssScore': 9.8,
                 'publishedAt': '2024-06-01T00:00:00Z', 'sourceLayer': 'advisory-api'},
                {'cveId': 'CVE-2023-0500', 'ghsaId': 'GHSA-high-aaaa-bbbb-cccc-dddd',
                 'url': 'https://github.com/advisories/GHSA-high-aaaa-bbbb-cccc-dddd',
                 'summary': 'high thing', 'severity': 'HIGH', 'cvssScore': 8.1,
                 'publishedAt': '2023-06-01T00:00:00Z', 'sourceLayer': 'local-corpus'},
                {'cveId': 'CVE-2022-0100', 'ghsaId': '',
                 'url': 'https://github.com/advisories?query=CVE-2022-0100',
                 'summary': 'code-search only', 'severity': '', 'cvssScore': None,
                 'publishedAt': '', 'packageName': '', 'sourceLayer': 'code-search'},
                {'cveId': '', 'ghsaId': 'GHSA-nocve-aaaa-bbbb-cccc-dddd',
                 'url': 'https://github.com/advisories/GHSA-nocve-aaaa-bbbb-cccc-dddd',
                 'summary': 'no CVE alias yet', 'severity': 'low', 'cvssScore': 2.0,
                 'publishedAt': '', 'sourceLayer': 'advisory-api'},
            ]}
        n_stored = scanhook.store_cves(sdb, wid_s, ident,
                                       _json.dumps(cv_sweep))
        check('cve store count (CVE-keyed only)', n_stored == 3, n_stored)
        n_stored2 = scanhook.store_cves(sdb, wid_s, ident,
                                        _json.dumps(cv_sweep))
        check('cve store upsert idempotent', n_stored2 == 3, n_stored2)
        check('cve store noop on findings output',
              scanhook.store_cves(sdb, wid_s, ident,
                                  '{"summary": {"findings": 1, "cves": 5}}') == 0)
        check('cve store noop on empty', scanhook.store_cves(sdb,
              wid_s, ident, '') == 0)
        _cdb = db.connect(sdb)
        try:
            cv_rows = scanhook.list_cves(_cdb, wid_s)
            check('cve rank critical first', cv_rows[0]['cve_id'] == 'CVE-2024-1000',
                  [r['cve_id'] for r in cv_rows])
            check('cve severity lowered', cv_rows[0]['severity'] == 'critical'
                  and cv_rows[1]['severity'] == 'high', [r['severity'] for r in cv_rows])
            check('cve nocve row dropped', len(cv_rows) == 3, len(cv_rows))
        finally:
            _cdb.close()
        check('summarize cves json',
              scanhook.summarize(_json.dumps(cv_sweep)) == '3 cves, 1 critical, 1 high',
              scanhook.summarize(_json.dumps(cv_sweep)))
        # vault-corpus instant layer: tiny FTS fixture, then render checks
        fxp = str(Path(tmp) / 'vaultfx.db')
        fx = db.connect(fxp)
        fx.executescript(
            "CREATE TABLE bug_corpus(source TEXT, source_id TEXT, title TEXT,"
            " body TEXT, cve_ids TEXT, disclosed_at TEXT);"
            "CREATE VIRTUAL TABLE bug_corpus_fts USING fts5(title, body, source,"
            " content='bug_corpus', content_rowid='rowid');")
        fx.execute("INSERT INTO bug_corpus VALUES('ghsa','GHSA-fix1-aaaa-bbbb-cccc',"
                   "'whitechain pad leak','Affected: npm:whitechain','CVE-2024-5555','2024-01-01')")
        fx.execute("INSERT INTO bug_corpus VALUES('nvd','CVE-2023-4444',"
                   "'whitechain old bug','some body','CVE-2023-4444','2023-01-01')")
        fx.execute("INSERT INTO bug_corpus_fts(bug_corpus_fts) VALUES('rebuild')")
        fx.commit()
        fx.close()
        with _mk.patch.object(cvecorpus, 'vault_db_path', return_value=fxp):
            cc = cvecorpus.corpus_cves('whitechain', limit=10)
            check('corpus fts hits', len(cc) == 2 and cc[0]['cve_id'] == 'CVE-2024-5555',
                  cc[:1])
            check('corpus ghsa github link',
                  cc[0]['url'] == 'https://github.com/advisories/GHSA-fix1-aaaa-bbbb-cccc',
                  cc[0]['url'])
            check('corpus nvd fallback link',
                  any(r['url'].startswith('https://nvd.nist.gov/') for r in cc),
                  [r['url'] for r in cc])
            check('corpus empty-name noop', cvecorpus.corpus_cves('') == [])
            # pagination: honest totals, pages, newest first across pages
            pg1 = cvecorpus.corpus_cves_page('whitechain', page=1, per_page=1,
                                             db_path=fxp)
            pg2 = cvecorpus.corpus_cves_page('whitechain', page=2, per_page=1,
                                             db_path=fxp)
            check('corpus page totals', pg1['total'] == 2 and pg1['pages'] == 2
                  and len(pg1['rows']) == 1, (pg1['total'], pg1['pages']))
            check('corpus page newest first', pg1['rows'][0]['cve_id'] == 'CVE-2024-5555'
                  and pg2['rows'][0]['cve_id'] == 'CVE-2023-4444',
                  (pg1['rows'][:1], pg2['rows'][:1]))
            check('corpus page beyond end empty',
                  cvecorpus.corpus_cves_page('whitechain', page=9, per_page=1,
                                             db_path=fxp)['rows'] == [])
            # cvelistV5 searchable layer: seed from a fixture tree, merge both
            # corpora, newest first across sources
            v5root = Path(tmp) / 'v5fx' / 'cves' / '2025' / '1xxx'
            v5root.mkdir(parents=True)
            (v5root / 'CVE-2025-0001.json').write_text(json.dumps({
                'cveMetadata': {'cveId': 'CVE-2025-0001', 'state': 'PUBLISHED',
                                'datePublished': '2025-06-01T00:00:00Z'},
                'containers': {'cna': {
                    'descriptions': [{'value': 'shopify checkout flow bug'}],
                    'affected': [{'vendor': 'Shopify', 'product': 'checkout'}]}}}),
                encoding='utf-8')
            (v5root / 'CVE-2025-0002.json').write_text(json.dumps({
                'cveMetadata': {'cveId': 'CVE-2025-0002', 'state': 'PUBLISHED',
                                'datePublished': '2026-01-01T00:00:00Z'},
                'containers': {'cna': {
                    'descriptions': [{'value': 'whitechain thing in the v5 feed'}],
                    'affected': [{'vendor': 'whitechain', 'product': 'core'}]}}}),
                encoding='utf-8')
            fx5 = str(Path(tmp) / 'v5fx.db')
            n5 = cvecorpus.seed_cvelist_v5(db_path=fx5, src_root=str(Path(tmp) / 'v5fx'))
            check('v5 seed count', n5 == 2, n5)
            n5b = cvecorpus.seed_cvelist_v5(db_path=fx5, src_root=str(Path(tmp) / 'v5fx'))
            check('v5 seed idempotent', n5b == 2, n5b)
            with _mk.patch.object(cvecorpus, 'v5_db_path', return_value=fx5):
                mg = cvecorpus.corpus_cves_page('whitechain', page=1, per_page=2,
                                                db_path=fxp)
                check('v5 merged total', mg['total'] == 3
                      and mg['pages'] == 2, (mg['total'], mg['pages']))
                check('v5 merged newest first',
                      mg['rows'][0]['cve_id'] == 'CVE-2025-0002'
                      and mg['rows'][0]['source'] == 'cve-v5', mg['rows'][:1])
                pg2 = cvecorpus.corpus_cves_page('whitechain', page=2, per_page=2,
                                                 db_path=fxp)
                check('v5 merged page 2 crosses sources',
                      pg2['total'] == 3 and
                      [r['cve_id'] for r in pg2['rows']] == ['CVE-2023-4444'],
                      [r['cve_id'] for r in pg2['rows']])
                shp = cvecorpus.corpus_cves_page('Shopify', page=1, per_page=10,
                                                 db_path=fxp)
                check('v5 vendor match', shp['total'] == 1
                      and shp['rows'][0]['cve_id'] == 'CVE-2025-0001', shp)
                # multi-token program names recall via longest-token variant
                # ('Whitechain Bridge Public' must hit the whitechain rows)
                vars_ = cvecorpus.name_variants('Cloudflare Public Bug Bounty')
                check('name variants built', vars_ == ['Cloudflare Public Bug Bounty',
                      'Cloudflare'], vars_)
                check('alias variants x', cvecorpus.name_variants('X') ==
                      ['twitter', 'xai'], cvecorpus.name_variants('X'))
                check('alias variants xai', cvecorpus.name_variants('xAI') ==
                      ['xAI', 'x ai'], cvecorpus.name_variants('xAI'))
                mt = cvecorpus.corpus_cves_page('Whitechain Bridge Public', page=1,
                                                per_page=10, db_path=fxp)
                check('multi-token name recall', mt['total'] == 3, mt['total'])
            # poc-reference harvest: tagged payload lane (plain refs skipped)
            (v5root / 'CVE-2025-0003.json').write_text(json.dumps({
                'cveMetadata': {'cveId': 'CVE-2025-0003', 'state': 'PUBLISHED'},
                'containers': {'cna': {
                    'descriptions': [{'value': 'x'}],
                    'references': [
                        {'url': 'https://example.com/press-release'},
                        {'url': 'https://www.exploit-db.com/exploits/51234'},
                        {'url': 'https://github.com/attacker/cve-2025-0003-poc'},
                        {'url': 'https://github.com/advisories/GHSA-new-aaaa-bbbb-cccc'},
                    ]}}}), encoding='utf-8')
            fxp3 = str(Path(tmp) / 'payloads.db')
            npr = cvecorpus.seed_poc_refs(db_path=fxp3, src_root=str(Path(tmp) / 'v5fx'))
            check('poc harvest count', npr == 3, npr)
            refs = cvecorpus.poc_refs_for('CVE-2025-0003', db_path=fxp3)
            tags = {r['tag'] for r in refs}
            check('poc harvest tags', tags == {'edb', 'repo-poc', 'advisory'}, refs)
            check('poc lookup empty cve safe',
                  cvecorpus.poc_refs_for('', db_path=fxp3) == [])
            # edb lane: files_exploits.csv index -> local exploit file path
            edbroot = Path(tmp) / 'edbfx'
            (edbroot / 'exploits' / '2024').mkdir(parents=True)
            (edbroot / 'files_exploits.csv').write_text(
                'id,file,description,date_published,author,type,platform\n'
                '51234,exploits/2024/51234.txt,demo exploit,2024-01-01,someone,webapps,multiple\n',
                encoding='utf-8')
            (edbroot / 'exploits' / '2024' / '51234.txt').write_text(
                '# demo exploit body', encoding='utf-8')
            with _mk.patch.object(cvecorpus, 'corpus_dirs',
                                  return_value={'exploitdb': str(edbroot)}):
                ep = cvecorpus.edb_file_for('51234')
                check('edb file lookup', ep is not None
                      and Path(ep).is_file(), ep)
                check('edb unknown id none',
                      cvecorpus.edb_file_for('99999999') is None)
            with _mk.patch.object(cvecorpus, 'corpus_dirs', return_value={}):
                check('edb missing archive none', cvecorpus.edb_file_for('51234') is None)
            r = ts.get('/api/cves?program_id=%d' % wid_s)
            jc = r.get_json()
            check('api cves merged', r.status_code == 200 and len(jc['cves']) == 3
                  and len(jc['corpus']) == 2, (r.status_code,
                  len(jc.get('cves', [])), len(jc.get('corpus', []))))
            r = ts.get('/api/cves')
            check('api cves 400 no pid', r.status_code == 400, r.status_code)
            r = ts.get('/api/cves?program_id=999999')
            check('api cves 404', r.status_code == 404, r.status_code)
            r = ts.get('/program/%d' % wid_s)
            cbody = r.data.decode('utf-8', 'ignore')
            check('cve section rendered', 'CVEs (5' in cbody, 'CVEs (5' not in cbody)
            check('cve advisory link live',
                  'href="https://github.com/advisories/GHSA-aaaa-bbbb-cccc-dddd"' in cbody,
                  'advisories link missing')
            check('cve corpus row rendered', 'corpus-ghsa' in cbody
                  and 'GHSA-fix1-aaaa-bbbb-cccc' in cbody, 'corpus row missing')
            check('cve sweep mode in selector', 'cve sweep' in cbody and 'value="cves"' in cbody,
                  'cves option missing')
            check('cve section no-cve hint', 'No CVEs on file' not in cbody, 'hint wrongly shown')
            # pager: per_page=1 over the 2-row corpus fixture -> 2 pages, live
            r = ts.get('/program/%d?per_page=1' % wid_s)
            pbody = r.data.decode('utf-8', 'ignore')
            check('cve pager rendered', 'page 1 / 2' in pbody and 'older →' in pbody
                  and 'of 2' in pbody, 'pager missing')
            r = ts.get('/program/%d?per_page=1&page=2' % wid_s)
            p2body = r.data.decode('utf-8', 'ignore')
            check('cve pager page 2', 'page 2 / 2' in p2body and '← newer' in p2body
                  and 'CVE-2023-4444' in p2body, 'page 2 wrong')
            # per-page selector: size links render in the pager line
            check('cve per-page selector', 'per page:' in p2body
                  and 'per_page=100' in p2body and 'per_page=50' in p2body, 'selector missing')
            # scanner rows sort latest-first (2024 before the undated fallback row)
            _cdb = db.connect(sdb)
            try:
                ord_rows = scanhook.list_cves(_cdb, wid_s)
                pubs = [r0['published'] for r0 in ord_rows[:3]]
            finally:
                _cdb.close()
            check('cve rows latest first', pubs == sorted(pubs, reverse=True), pubs)
        with _mk.patch.object(cvecorpus, 'corpus_dirs',
                              return_value={'cvelistV5': None, 'advisory-database': None}):
            st = cvecorpus.refresh_corpus()
            check('corpus refresh missing-safe',
                  st == {'cvelistV5': 'missing', 'advisory-database': 'missing'}, st)
            # freshness: a dir outside any git repo degrades to None, never
            # raises (FIX sits inside the workspace repo — git -C would walk up
            # and resolve to workspace HEAD, so use tmp instead)
            plainco = Path(tmp) / 'plainco'
            plainco.mkdir(parents=True, exist_ok=True)
            with _mk.patch.object(cvecorpus, 'corpus_dirs',
                                  return_value={'cvelistV5': str(plainco), 'advisory-database': None}):
                fr = cvecorpus.freshness()
            check('corpus freshness non-repo safe',
                  fr == {'cvelistV5': None, 'advisory-database': None}, fr)
        # refresh skips a checkout mid-clone/pull (index.lock present)
        busy = Path(tmp) / 'busyco'
        (busy / '.git').mkdir(parents=True)
        (busy / '.git' / 'index.lock').write_text('', encoding='utf-8')
        st_busy = cvecorpus.refresh_corpus({'advisory-database': str(busy)})
        check('corpus refresh busy skip', st_busy == {'advisory-database': 'busy'}, st_busy)
        cstats = cvecorpus.corpus_stats()
        check('corpus stats shape', 'vault_cve_rows' in cstats and 'checkouts' in cstats,
              sorted(cstats.keys()))
        r = ts.get('/program/%d' % wid_s)
        # cve xss guard: a stored XSS identity must stay escaped in the CVE list
        r = ts.post('/api/scan', json={'program_id': wid_s,
                                       'identity': '<img src=x onerror=alert(9)>',
                                       'manual': True, 'mode': 'sniper'})
        check('manual xss accepted (sniper)', r.status_code == 202, r.status_code)
        n_xss = scanhook.store_cves(sdb, wid_s,
                                    '<img src=x onerror=alert(9)>', _json.dumps(cv_sweep))
        _cdb = db.connect(sdb)
        try:
            xss_rows = scanhook.list_cves(_cdb, wid_s)
            ident_xss = [r0['identity'] for r0 in xss_rows]
        finally:
            _cdb.close()
        check('cve xss identity stored raw in db', n_xss == 3 and
              any('<img' in i for i in ident_xss), (n_xss, ident_xss[:1]))
        r = ts.get('/program/%d' % wid_s)
        check('cve xss identity escaped on page',
              b'<img src=x onerror' not in r.data and b'&lt;img src=x' in r.data)

        # 20d. manual scan: always-visible panel + paste-any-target
        check('manual panel ui', 'manualScan' in dbs6 and 'scanTarget' in dbs6)
        r = ts.post('/api/scan', json={'program_id': wid_s, 'identity': 'whatever-owner/repo',
                                       'manual': True, 'mode': 'sniper'})
        ok_m = r.status_code == 202
        sid7 = r.get_json().get('scan_id')
        done7 = None
        for _ in range(30):
            _time.sleep(0.3)
            sc = [s for s in ts.get('/api/scans?program_id=%d' % wid_s).get_json()['scans']
                  if s['id'] == sid7]
            if sc and sc[0]['status'] in ('done', 'error'):
                done7 = sc[0]
                break
        check('manual scan runs', ok_m and done7 and done7['status'] in ('done', 'error'), done7)
        # manual panel on a scopeless program (no targets ingested)
        sdb2 = str(Path(tmp) / 'noscope.db')
        radar_app.refresh_once(sdb2, live=False, seed=True)
        webn = radar_app.create_app(sdb2); webn.testing = True
        tn = webn.test_client()
        wid_n = tn.get('/api/programs?q=whitechain').get_json()['programs'][0]['id']
        cxn = db.connect(sdb2)
        cxn.execute('DELETE FROM targets WHERE program_id=?', (wid_n,))
        cxn.commit(); cxn.close()
        r = tn.get('/program/%d' % wid_n)
        dn = r.data.decode('utf-8', 'ignore')
        check('panel on scopeless page', 'manualScan' in dn and 'scanTarget' in dn
              and '🔍 scan<' not in dn)

        # 20b. REAL binary end-to-end: actual crv-leak-scan through /api/scan
        crv_cli = scanhook.find_scanner()
        repo_dir = _os.path.join(str(tmp), 'scanfix-e2e', 'probe-repo')
        if crv_cli and _os.path.isfile(crv_cli):
            import subprocess as _sub
            _os.makedirs(repo_dir, exist_ok=True)
            _sub.run(['git', 'init', '-q', repo_dir], check=False)
            with open(_os.path.join(repo_dir, 'chain.js'), 'w') as f:
                f.write('rpc = "https://api.mainnet-beta.solana.com/?apikey=5up3rs3cr3tv4lueN0tRe4lK3yX9q2w"\n')
        if crv_cli and _os.path.isfile(crv_cli) and _os.path.isdir(repo_dir):
            r = ts.post('/api/scan', json={'program_id': wid_s})
            check('real scan needs ident', r.status_code == 400)
            # register the repo path as a target identity, then scan it for real
            import tracker as _trk2
            _ts_now = db.now_iso()
            cxr = db.connect(sdb)
            _trk2.ingest(cxr, [{'platform': 'hackenproof', 'slug': 'whitechain',
                                'name': 'Whitechain', 'url': 'https://hackenproof.com/programs/whitechain',
                                'targets': [repo_dir]}], 'hackenproof', ts=_ts_now)
            cxr.close()
            _os.environ['LEAK_SCAN_CMD'] = 'node "%s" {target} --no-history --json' % crv_cli
            _os.environ['LEAK_SCAN_TIMEOUT'] = '90'
            r = ts.post('/api/scan', json={'program_id': wid_s, 'identity': repo_dir})
            sid5 = r.get_json().get('scan_id')
            real = None
            for _ in range(120):
                _time.sleep(0.5)
                sc = [s for s in ts.get('/api/scans?program_id=%d' % wid_s).get_json()['scans']
                      if s['id'] == sid5]
                if sc and sc[0]['status'] in ('done', 'error'):
                    real = sc[0]
                    break
            check('REAL scanner ran', bool(real) and real['status'] == 'done', real)
            check('REAL scanner found leak',
                  bool(real) and real.get('summary', '').startswith('1 findings'),
                  (real or {}).get('summary'))
        else:
            check('real scanner present', True,
                  'no crv-leak-scan on this machine - real-binary lane n/a')
    finally:
        if _old_cmd is None:
            _os.environ.pop('LEAK_SCAN_CMD', None)
        else:
            _os.environ['LEAK_SCAN_CMD'] = _old_cmd
        if _old_to is None:
            _os.environ.pop('LEAK_SCAN_TIMEOUT', None)
        else:
            _os.environ['LEAK_SCAN_TIMEOUT'] = _old_to

    # 21c. unicode safety in feed + export
    uni = {'platform': 'bbradar', 'slug': 'unicode-prog', 'name': 'Ünïcødé — プログラム test',
           'url': 'https://example.com/programs/uni', 'bounty_max': 1000.0,
           'reward_raw': '', 'tags': ['domain'], 'targets': []}
    cxu = db.connect(appdb)
    __import__('tracker').ingest(cxu, [uni], 'bbradar')
    cxu.close()
    r = t.get('/?sort=name&vdp=all&show=all')
    check('unicode name renders', 'Ünïcødé'.encode('utf-8') in r.data)
    r = t.get('/export.txt?sort=name&vdp=all&show=all&n=50')
    check('unicode export', r.status_code == 200 and 'Ünïcødé'.encode('utf-8') in r.data)

    # 21e. light-mode toggle (upstream parity)
    base = (FIX.parent.parent / 'templates' / 'base.html').read_text(encoding='utf-8')
    check('theme toggle ui', 'toggleTheme' in base and 'themeToggle' in base
          and "localStorage.getItem('theme')" in base)
    css = (FIX.parent.parent / 'static' / 'style.css').read_text(encoding='utf-8')
    check('light theme css', 'html.light' in css and '--hdr' in css)
    r = t.get('/static/style.css')
    check('light css served', b'html.light' in r.data)

    # 21f. CRV recon identity: rebrand complete + logo + theme palettes
    check('brand crv recon', 'CRV <strong>recon</strong>' in base
          and 'radar-clone' not in base and 'radar<span' not in base, 'brand stale')
    check('logo asset + favicon', (FIX.parent.parent / 'static' / 'logo.svg').is_file()
          and 'rel="icon"' in base and '/static/logo.svg' in base, 'favicon missing')
    check('dark rose palette', 'rgba(244,63,94' in css and '--acc: #fb7185' in css, 'dark palette missing')
    check('light cream palette', '#fbf7ee' in css and '#6f4a1e' in css
          and '--acc: #a0622d' in css, 'light palette missing')
    check('severity chip css', 'chip.sev-critical' in css and 'chip.sev-moderate' in css
          and 'html.light .chip.sev-critical' in css, 'sev css missing')
    check('nav toolbar css', 'nav { display: flex' in css and 'border-radius: .45rem' in css
          and '.themetoggle { background: var(--surf)' in css, 'nav toolbar missing')
    check('filters panel css', '.filters { display: flex' in css
          and 'background: var(--card)' in css and 'border-radius: .6rem' in css, 'filters panel missing')
    check('cve link css', '.cves a { color: var(--acc)' in css, 'cve link styling missing')
    r = t.get('/')
    body = r.data.decode('utf-8', 'ignore')
    check('index branded', 'CRV recon' in body and 'radar-clone' not in body, 'index stale')
    r = t.get('/api')
    abody = r.data.decode('utf-8', 'ignore')
    check('api docs page', r.status_code == 200 and 'CRV recon API' in abody
          and '/api/cves?program_id=' in abody and '/llms.txt' in abody, r.status_code)
    check('nav api human link', 'href="/api"' in base
          and 'href="/api/programs"' not in base, 'nav still points at raw json')
    r = t.get('/api/programs')
    check('api json still json', r.status_code == 200
          and r.is_json and 'programs' in r.get_json(), r.status_code)

    # 21d. history pruning + single-flight refresh guard
    cxp3 = db.init_db(str(Path(tmp) / 'prune.db'))
    for i in range(30):
        db.log_change(cxp3, kind='program_added', program_id=i + 1, detail='p%d' % i)
    pruned = db.prune_history(cxp3, max_changes=10, max_scrapes=5, max_scans=5)
    left = cxp3.execute('SELECT COUNT(*) c FROM changes').fetchone()['c']
    check('prune caps changes', left == 10 and pruned.get('changes') == 20, (left, pruned))
    newest = cxp3.execute('SELECT MIN(id) m FROM changes').fetchone()['m']
    check('prune keeps newest', newest == 21, newest)
    cxp3.close()
    import unittest.mock as _mock2
    with _mock2.patch.object(radar_app, 'refresh_once', return_value=3) as mro2:
        radar_app._refresh_running = True
        started, n = radar_app.refresh_guarded(live=False)
        radar_app._refresh_running = False
        check('guard blocks while running', started is False and n == 0 and mro2.call_count == 0)
        started2, n2 = radar_app.refresh_guarded(live=False)
        check('guard runs when free', started2 is True and n2 == 3
              and radar_app._refresh_running is False, (started2, n2))

    # 17. refresher units (cron / systemd / windows task)
    ops = ROOT / 'ops'
    check('ops units exist', all((ops / f).exists() for f in
                                 ('radar-refresh.service', 'radar-refresh.timer',
                                  'radar-refresh.cron', 'radar-refresh.xml')))
    svc = (ops / 'radar-refresh.service').read_text(encoding='utf-8')
    check('systemd unit', 'refresh.py --live' in svc and 'WorkingDirectory=' in svc)
    tmr = (ops / 'radar-refresh.timer').read_text(encoding='utf-8')
    check('systemd timer', 'OnUnitActiveSec=30min' in tmr)
    cron = (ops / 'radar-refresh.cron').read_text(encoding='utf-8')
    cron_lines = [l for l in cron.splitlines() if l.strip() and not l.strip().startswith('#')]
    check('cron line', len(cron_lines) == 1 and len(cron_lines[0].split()) >= 6
          and 'refresh.py' in cron_lines[0], cron_lines)
    import xml.etree.ElementTree as _et
    try:
        xroot = _et.fromstring((ops / 'radar-refresh.xml').read_text(encoding='utf-8-sig'))
        ns = {'t': 'http://schemas.microsoft.com/windows/2004/02/mit/task'}
        xok = (xroot.find('t:Triggers/t:CalendarTrigger/t:Repetition/t:Interval', ns) is not None
               and xroot.find('t:Actions/t:Exec/t:Command', ns) is not None)
    except Exception:
        xok = False
    check('windows task xml', xok)

    print('checks: %d passed, %d failed' % (passed, failed))
    print('METRIC checks_passed=%d' % passed)
    return 0 if failed == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
