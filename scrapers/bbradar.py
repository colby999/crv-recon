"""bbradar.io aggregator adapter: their full multi-platform catalog via the
browser-lane XHR capture (their API is Cloudflare-gated to requests).

Rows for platforms we already cover with live adapters are skipped to avoid
duplicate listings; the 15 unreachable platforms come through this lane.
"""
import json

from . import norm

SKIP_PLATFORMS = {'hackenproof', 'yeswehack', 'hackerone', 'bugcrowd', 'immunefi',
                  'cantina', 'sherlock', 'code4rena', 'codehawks', 'intigriti'}
API_PATH = '/api/programs?scope_source=stored&sort=date_desc'


def platform_slug(label):
    import re
    s = (label or '').strip().lower()
    s = re.sub(r'[^a-z0-9]+', '-', s).strip('-')
    return s or 'bbradar'


def parse_json(rows):
    out = []
    for r in rows:
        plat = platform_slug(r.get('platform_label') or r.get('platform'))
        if plat in SKIP_PLATFORMS:
            continue  # we have live adapters for these — avoid duplicate listings
        handle = r.get('handle') or ''
        name = (r.get('name') or handle or '').strip()
        if not name:
            continue
        status = (r.get('program_status') or 'active').strip().lower()
        state = {'active': 'active', 'paused': 'suspended', 'upcoming': 'upcoming',
                 'ended': 'ended'}.get(status, 'active')
        launched = r.get('date_launched') or (r.get('launch_at') or '')[:10] or None
        end = r.get('ends_on') or (r.get('ends_at') or '')[:10] or None
        kind = (r.get('program_kind') or 'bounty').strip().lower()
        no_reward = (r.get('reward_status') or '') == 'no_reward'
        bmin = r.get('bounty_min')
        bmax = r.get('bounty_max')
        out.append(norm({
            'platform': plat,
            'slug': handle or name.lower().replace(' ', '-')[:60],
            'name': name[:160],
            'url': r.get('link') or '',
            'bounty_min': bmin if isinstance(bmin, (int, float)) else None,
            'bounty_max': bmax if isinstance(bmax, (int, float)) else None,
            'reward_raw': r.get('reward_range') or '',
            'tags': [t.strip().lower() for t in (r.get('scope_tags') or [])
                     if isinstance(t, str)][:6],
            'languages': [],
            'state': state,
            'vdp': 1 if no_reward else 0,
            'is_contest': 1 if kind == 'contest' else 0,
            'upstream_listed': launched,
            'end_date': end,
            'logo_url': r.get('profile_picture') or None,
        }))
    return out


def parse_html(html):
    """Accepts the raw JSON text (from the captured XHR body)."""
    return parse_json(json.loads(html))


def fetch():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch(args=['--no-sandbox'])
        try:
            pg = b.new_page(user_agent='crv-recon/1.0 (research aggregator)')
            body = {}

            def on_resp(r):
                if API_PATH in r.url:
                    try:
                        body['text'] = r.text()
                    except Exception:
                        pass
            pg.on('response', on_resp)
            pg.goto('https://bbradar.io/', timeout=60000, wait_until='domcontentloaded')
            pg.wait_for_timeout(12000)
            b.close()
        except Exception:
            try:
                b.close()
            except Exception:
                pass
            raise
    if not body.get('text'):
        raise RuntimeError('bbradar catalog XHR not captured')
    return parse_json(json.loads(body['text']))
