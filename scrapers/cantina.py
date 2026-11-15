"""Cantina adapter: competitions + bounties from the RSC flight payload."""
import json

from . import TIMEOUT, UA, next_flight_text, norm

LIST_URL = 'https://cantina.xyz/competitions'
BASE = 'https://cantina.xyz'


def parse_flight(html):
    """Extract the dehydrated react-query items array(s) from flight text."""
    F = next_flight_text(html)
    dec = json.JSONDecoder()
    items, seen = [], set()
    idx = F.find('"items":[')
    while idx != -1:
        start = F.find('[', idx)
        try:
            arr, _ = dec.raw_decode(F, start)
            for it in arr if isinstance(arr, list) else []:
                if isinstance(it, dict) and it.get('id') and it['id'] not in seen:
                    seen.add(it['id'])
                    items.append(it)
        except (json.JSONDecodeError, ValueError):
            pass
        idx = F.find('"items":[', idx + 8)
    return items


def parse_html(html):
    out = []
    for it in parse_flight(html):
        name = (it.get('name') or '').strip()
        if not name:
            continue
        url = it.get('url') or ''
        kind = (it.get('kind') or '').strip().lower()
        is_comp = kind == 'competition' or '/competitions/' in url
        path = url if url.startswith('/') else url.replace(BASE, '')
        tf = it.get('timeframe') or {}
        status = (it.get('status') or '').strip().lower()
        state = {'live': 'active', 'upcoming': 'upcoming',
                 'ended': 'ended', 'finished': 'ended'}.get(status, 'active')
        pot = it.get('totalRewardPot')
        try:
            bmax = float(pot) if pot not in (None, '') else None
        except (TypeError, ValueError):
            bmax = None
        out.append(norm({
            'platform': 'cantina',
            'slug': (it.get('id') or name.lower().replace(' ', '-')[:60]),
            'name': name[:160],
            'url': url if url.startswith('http') else BASE + path,
            'bounty_min': None, 'bounty_max': bmax,
            'reward_raw': ('%g %s' % (bmax, it.get('currencyCode') or '')) if bmax else '',
            'tags': (['contest', 'source-code', 'smart-contract'] if is_comp
                     else ['bounty', 'smart-contract']),
            'languages': [],
            'state': state,
            'is_contest': 1 if is_comp else 0,
            'upstream_listed': (tf.get('start') or '')[:10] or None,
            'end_date': (tf.get('end') or '')[:10] or None,
            'logo_url': (it.get('company') or {}).get('logo') or None,
            'blurb': (it.get('shortDescription') or it.get('description') or '')[:400],
        }))
    return out


def fetch():
    from . import _browser
    return parse_html(_browser.render_html(LIST_URL, scrolls=3, wait=7))
