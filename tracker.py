"""Ingest pipeline: normalize, upsert programs, diff targets, log changes (live tracking)."""
from db import upsert_program, sync_targets, log_change, record_scrape, now_iso
from tags import normalize_program, lifecycle, detect_vdp, derive_focus


def prepare(p):
    """Normalize one raw scraper dict in place. Returns it."""
    normalize_program(p)
    p['state'] = lifecycle(p.get('state'), p.get('end_date'))
    if detect_vdp(p):
        p['vdp'] = 1
    if not p.get('focus'):
        p['focus'] = ','.join(derive_focus(p))
    p.setdefault('seed', 0)
    return p


def deep_fill_bc(cx, limit=30, _http=None, _stats=None, _render=None):
    """Requests-only BC deep fill: changelog gives launch date + structured
    scope (no browser). Falls back to rendered briefs only if no changelog."""
    from scrapers import bugcrowd as bc_mod
    rows = cx.execute(
        """SELECT id, slug FROM programs WHERE platform='bugcrowd'
           AND id NOT IN (SELECT program_id FROM targets WHERE in_scope=1)
           ORDER BY bounty_max DESC LIMIT ?""", (limit,)).fetchall()
    filled = 0
    for r in rows:
        try:
            parsed = bc_mod.fetch_changelog(r['slug'], _http=_http)
            if not parsed:
                if _render is not None:
                    parsed = bc_mod.parse_program_html(_render(r['slug']))
                elif _http is None:
                    parsed = bc_mod.parse_program_html(bc_mod.render_program(r['slug']))
            idents = [s['target'] for s in parsed.get('scopes', [])]
            cx.execute("UPDATE programs SET last_deep=? WHERE id=?", (now_iso(), r['id']))
            starts = parsed.get('startsAt')
            if starts:
                from tags import parse_upstream_date
                cx.execute("UPDATE programs SET upstream_listed=? WHERE id=?",
                           (parse_upstream_date(starts), r['id']))
            if not idents:
                cx.commit()
                continue  # empty upstream scope: recorded attempt, nothing to sync
            elig = {s['target']: s['eligible'] for s in parsed.get('scopes', [])}
            for kind, ident in sync_targets(cx, r['id'], idents, eligible_map=elig):
                log_change(cx, kind=kind, program_id=r['id'], detail=ident)
            try:
                stats = (_stats(r['slug']) if _stats
                         else bc_mod.fetch_program_stats(r['slug']))
                rc = stats.get('rewardedVulnerabilities')
                if isinstance(rc, int):
                    cx.execute('UPDATE programs SET report_count=? WHERE id=?', (rc, r['id']))
            except Exception:
                pass
            filled += 1
            cx.commit()
        except Exception:
            try:
                cx.rollback()
            except Exception:
                pass
            continue
    record_scrape(cx, 'bugcrowd-deep', filled)
    cx.commit()
    return filled, len(rows)


def deep_fill_hp(cx, limit=100, _http=None, delay=0.4):
    """Fetch HP program pages for rows that have no live targets yet.
    Updates logo/blurb, syncs scope (eligible vs out-of-scope), logs events."""
    from scrapers import hackenproof as hp_mod
    rows = cx.execute(
        """SELECT id, slug FROM programs WHERE platform='hackenproof'
           AND id NOT IN (SELECT program_id FROM targets WHERE in_scope=1)
           ORDER BY bounty_max DESC LIMIT ?""", (limit,)).fetchall()
    slugs = [r['slug'] for r in rows]
    id_by_slug = {r['slug']: r['id'] for r in rows}
    got = hp_mod.fetch_deep(slugs, _http=_http, delay=delay, limit=limit)
    filled = 0
    for slug, parsed in got.items():
        pid = id_by_slug.get(slug)
        if not pid:
            continue
        cx.execute("UPDATE programs SET last_deep=? WHERE id=?", (now_iso(), pid))
        elig = {s['target']: s['eligible'] for s in parsed.get('scopes', [])}
        idents = [s['target'] for s in parsed.get('scopes', [])]
        if not idents:
            continue  # empty upstream scope: recorded attempt, nothing to sync
        cx.execute('UPDATE programs SET logo_url=COALESCE(?, logo_url), blurb=? WHERE id=?',
                   (parsed.get('logo_url'), parsed.get('blurb', ''), pid))
        for kind, ident in sync_targets(cx, pid, idents, eligible_map=elig):
            log_change(cx, kind=kind, program_id=pid, detail=ident)
        filled += 1
        cx.commit()
    record_scrape(cx, 'hackenproof-deep', filled)
    cx.commit()
    return filled, len(slugs)


def ingest(cx, programs, platform, ts=None):
    """Returns (new_programs, events). Emits changes rows for the live feed."""
    ts_new, events = 0, []
    for raw in programs:
        p = prepare(dict(raw))
        pid, is_new = upsert_program(cx, p, ts)
        if is_new:
            ts_new += 1
            log_change(cx, kind='program_added', program_id=pid, detail=p['name'])
        for kind, ident in sync_targets(cx, pid, p.get('targets', []), ts):
            events.append((kind, p['name'], ident))
            log_change(cx, kind=kind, program_id=pid, detail=ident)
    record_scrape(cx, platform, len(programs))
    cx.commit()
    return ts_new, events


def auto_deep(cx, hp_limit=40, bc_limit=10, _http=None, _render=None, _stats=None, delay=0.4):
    """Scheduled incremental backfill: newest unscraped programs first.
    Converges to full coverage across scheduler cycles without manual runs."""
    hf, ht = deep_fill_hp(cx, limit=hp_limit, _http=_http, delay=delay)
    bf, bt = deep_fill_bc(cx, limit=bc_limit, _render=_render, _stats=_stats)
    return {'hp': (hf, ht), 'bc': (bf, bt)}


def deep_one(cx, platform, slug, _http=None, _render=None, _stats=None):
    """One-shot scope fill for a single program (lazy detail-page backfill)."""
    row = cx.execute('SELECT id FROM programs WHERE platform=? AND slug=?',
                     (platform, slug)).fetchone()
    if not row:
        return False
    if platform == 'hackenproof':
        from scrapers import hackenproof as hp_mod
        try:
            parsed = hp_mod.fetch_program(slug, _http=_http)
        except Exception:
            return False
        if not parsed:
            return False
        cx.execute("UPDATE programs SET last_deep=? WHERE id=?", (now_iso(), row['id']))
        if not parsed.get('scopes'):
            cx.commit()
            return False
        cx.execute('UPDATE programs SET logo_url=COALESCE(?, logo_url), blurb=? WHERE id=?',
                   (parsed.get('logo_url'), parsed.get('blurb', ''), row['id']))
        elig = {s['target']: s['eligible'] for s in parsed['scopes']}
        idents = [s['target'] for s in parsed['scopes']]
    elif platform == 'bugcrowd':
        from scrapers import bugcrowd as bc_mod
        try:
            html = _render(slug) if _render else bc_mod.render_program(slug)
            parsed = bc_mod.parse_program_html(html)
        except Exception:
            return False
        if not parsed:
            return False
        cx.execute("UPDATE programs SET last_deep=? WHERE id=?", (now_iso(), row['id']))
        if not parsed.get('scopes'):
            cx.commit()
            return False
        elig = {s['target']: s['eligible'] for s in parsed['scopes']}
        idents = [s['target'] for s in parsed['scopes']]
    else:
        return False
    for kind, ident in sync_targets(cx, row['id'], idents, eligible_map=elig):
        log_change(cx, kind=kind, program_id=row['id'], detail=ident)
    record_scrape(cx, 'deep-one', 1)
    cx.commit()
    return True
