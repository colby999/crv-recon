"""CodeHawks adapter: contests from the embedded JSON envelope."""
import json
import re
from datetime import datetime, timezone

from . import TIMEOUT, UA, norm

LIST_URL = 'https://codehawks.cyfrin.io/'
BASE = 'https://codehawks.cyfrin.io'


def _now():
    return datetime.now(timezone.utc)


def parse_html(html):
    m = re.search(r'<script[^>]*type="application/json"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        return []
    try:
        outer = json.loads(m.group(1))
        inner = json.loads(outer.get('body') or '[]')
    except (json.JSONDecodeError, KeyError, ValueError):
        return []
    out = []
    for block in inner if isinstance(inner, list) else []:
        data = ((block.get('result') or {}).get('data')) or []
        for rec in data:
            if not isinstance(rec, dict):
                continue
            name = (rec.get('name') or '').strip()
            slug = rec.get('urlSlug')
            if not name or not slug:
                continue
            start = (rec.get('startDate') or '')[:10]
            end = (rec.get('endDate') or '')[:10]
            state = 'active'
            try:
                sd = datetime.fromisoformat(rec.get('startDate').replace('Z', '+00:00'))
                ed = datetime.fromisoformat(rec.get('endDate').replace('Z', '+00:00'))
                if sd.tzinfo is None:
                    sd = sd.replace(tzinfo=timezone.utc)
                if ed.tzinfo is None:
                    ed = ed.replace(tzinfo=timezone.utc)
                now = _now()
                if sd > now:
                    state = 'upcoming'
                elif ed < now:
                    state = 'ended'
            except (AttributeError, ValueError, TypeError):
                pass
            reward = rec.get('reward')
            try:
                bmax = float(reward) if reward not in (None, '') else None
            except (TypeError, ValueError):
                bmax = None
            tags = [t.strip().lower() for t in (rec.get('protocolTags') or '').split(',')
                    if t.strip()]
            out.append(norm({
                'platform': 'codehawks',
                'slug': slug,
                'name': name[:160],
                'url': BASE + '/contest/' + slug,
                'bounty_min': None, 'bounty_max': bmax,
                'reward_raw': ('%g %s' % (bmax, rec.get('currency') or 'USD')) if bmax else '',
                'tags': (tags[:5] or ['contest', 'smart-contract']) + ['contest'],
                'languages': [],
                'state': state,
                'is_contest': 1,
                'upstream_listed': start or None,
                'end_date': rec.get('endDate'),
                'logo_url': None,
            }))
    return out


def fetch():
    import requests
    r = requests.get(LIST_URL, headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    return parse_html(r.text)
