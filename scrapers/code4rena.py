"""Code4rena adapter: contests from the RSC flight payload."""
import re

from . import TIMEOUT, UA, next_flight_text, norm

LIST_URL = 'https://code4rena.com/audits'
BASE = 'https://code4rena.com'


def parse_html(html):
    F = next_flight_text(html)
    out, seen = [], set()
    for m in re.finditer(r'"uid":"([A-Za-z0-9]+)",', F):
        uid = m.group(1)
        win = F[max(0, m.start() - 1500):m.end() + 1500]

        def grab(field, win=win):
            fm = re.search(r'"%s":"((?:[^"\\]|\\.)*?)"' % field, win)
            return fm.group(1) if fm else None

        title = grab('title')
        slug = grab('slug')
        if not title or not slug or slug in seen:
            continue
        seen.add(slug)
        status = (grab('status') or '').strip().lower()
        start = grab('startTime')
        amount = (grab('formattedAmount') or '').replace('$$', '$')
        state = {'live': 'active', 'judging': 'active', 'ended': 'ended',
                 'upcoming': 'upcoming', 'active': 'active'}.get(status, 'active')
        out.append(norm({
            'platform': 'code4rena',
            'slug': slug,
            'name': title[:160],
            'url': BASE + '/report/' + slug,
            'bounty_max': None,
            'reward_raw': amount,
            'tags': ['contest', 'smart-contract', 'source-code'],
            'languages': [],
            'state': state,
            'is_contest': 1,
            'upstream_listed': (start or '')[:10] or None,
            'blurb': 'Code4rena audit contest — %s' % status,
        }))
    return out


def fetch():
    import requests
    r = requests.get(LIST_URL, headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    return parse_html(r.text)
