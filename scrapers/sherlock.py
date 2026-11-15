"""Sherlock adapter: audit contests from audits.sherlock.xyz flight payload."""
import json
import re
from datetime import datetime, timezone

from . import TIMEOUT, UA, next_flight_text, norm

LIST_URL = 'https://audits.sherlock.xyz/contests'
BASE = 'https://audits.sherlock.xyz'


def _iso(epoch):
    try:
        return datetime.fromtimestamp(int(epoch), tz=timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _records(F):
    dec = json.JSONDecoder()
    out = []
    idx = F.find('{"isJudging')
    while idx != -1:
        try:
            rec, _ = dec.raw_decode(F, idx)
            if isinstance(rec, dict) and rec.get('title'):
                out.append(rec)
                idx += len(json.dumps(rec)) + 2
        except (json.JSONDecodeError, ValueError):
            pass
        idx = F.find('{"isJudging', idx + 1)
    seen, uniq = set(), []
    for r in out:
        if r['id'] not in seen:
            seen.add(r['id'])
            uniq.append(r)
    return uniq


def parse_html(html):
    F = next_flight_text(html)
    out = []
    for r in _records(F):
        title = (r.get('title') or '').strip()
        if not title:
            continue
        status = (r.get('status') or '').strip().upper()
        starts = _iso(r.get('startsAt'))
        ends = _iso(r.get('endsAt'))
        state = {'live': 'active', 'upcoming': 'upcoming',
                 'finished': 'ended'}.get(status.lower(), 'active')
        if starts:
            from tags import parse_upstream_date, lifecycle
            state = lifecycle(state, ends)
        pot = r.get('prizePool') or r.get('rewards')
        try:
            bmax = float(pot) if pot not in (None, '') else None
        except (TypeError, ValueError):
            bmax = None
        token = r.get('token') or 'USDC'
        slug = r.get('slug') or re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-')[:60]
        out.append(norm({
            'platform': 'sherlock',
            'slug': str(r.get('id') or slug),
            'name': title[:160],
            'url': BASE + '/contests/' + slug,
            'bounty_min': None, 'bounty_max': bmax,
            'reward_raw': ('%g %s prize pool' % (bmax, token)) if bmax else '',
            'tags': ['contest', 'smart-contract', 'source-code'],
            'languages': [],
            'state': state,
            'is_contest': 1,
            'upstream_listed': starts[:10] if starts else None,
            'end_date': ends,
            'logo_url': r.get('logoUrl') or None,
            'blurb': (r.get('shortDescription') or '')[:400],
        }))
    return out


def fetch():
    import requests
    r = requests.get(LIST_URL, headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    return parse_html(r.text)
