"""Immunefi adapter: featured bounties table on /bug-bounty/ (browser lane).

Only ~10 featured rows are public without auth; runs in live refresh as a
bonus lane (failures never break the loop).
"""
import re
from urllib.parse import unquote, urlparse, parse_qs

from . import TIMEOUT, UA, norm

LIST_URL = 'https://immunefi.com/bug-bounty/'
BASE = 'https://immunefi.com'

_MONEY = re.compile(r'\$?\s*([\d,.]+)\s*(k|m)?\b', re.I)


def _money(raw):
    if not raw:
        return None
    m = _MONEY.search(str(raw).replace(',', ''))
    if not m:
        return None
    try:
        v = float(m.group(1))
    except ValueError:
        return None
    mult = (m.group(2) or '').lower()
    if mult == 'k':
        v *= 1000
    elif mult == 'm':
        v *= 1000000
    return v


def _direct_logo(src):
    if not src:
        return None
    if '/_next/image' in src and 'url=' in src:
        try:
            q = parse_qs(urlparse(src).query)
            inner = unquote(q.get('url', [''])[0])
            return inner if inner.startswith('http') else None
        except Exception:
            return None
    return src if src.startswith('http') else None


def parse_html(html):
    from bs4 import BeautifulSoup
    from tags import parse_upstream_date
    soup = BeautifulSoup(html, 'html.parser')
    out = []
    for row in soup.select('tr[data-program-slug]'):
        slug = (row.get('data-program-slug') or '').strip()
        if not slug:
            continue
        cells = [c.get_text(' ', strip=True) for c in row.find_all('td')]
        name = cells[0] if cells else slug
        bounty = None
        for cell in cells[1:]:
            v = _money(cell)
            if v and (bounty is None or v > bounty):
                bounty = v
        img = row.find('img')
        logo = _direct_logo((img.get('src') or '') if img else '')
        listed = None
        for cell in cells[1:]:
            d = parse_upstream_date(cell)
            if d and '/' in cell:
                listed = d
                break
        out.append(norm({
            'platform': 'immunefi', 'slug': slug, 'name': name[:160],
            'url': BASE + '/bug-bounty/' + slug + '/',
            'bounty_min': None, 'bounty_max': bounty, 'reward_raw': '',
            'tags': ['smart-contract', 'blockchain'], 'languages': [],
            'logo_url': logo,
            'upstream_listed': listed,
        }))
    return out


def fetch():
    from . import _browser
    return parse_html(_browser.render_html(LIST_URL))
