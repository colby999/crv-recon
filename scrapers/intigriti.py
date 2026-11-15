"""Intigriti adapter: public program listing (SSR cards on www.intigriti.com)."""
import re

from . import TIMEOUT, UA, norm

LIST_URL = 'https://www.intigriti.com/programs'
_APP_RE = re.compile(r'^https://app\.intigriti\.com/programs/([a-z0-9-]+)/[a-z0-9-]+')


def parse_html(html):
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, 'html.parser')
    out, seen = [], set()
    for a in soup.find_all('a', href=True):
        m = _APP_RE.match(a['href'])
        if not m:
            continue
        slug = m.group(1)
        if slug in seen:
            continue
        card = a
        for _ in range(5):
            if card.parent is None:
                break
            card = card.parent
            if len(card.get_text(' ', strip=True)) > 80:
                break
        text = card.get_text(' ', strip=True)
        # name: card heading (h4 headings hold the clean program title)
        h = card.find(['h1', 'h2', 'h3', 'h4'])
        name = (h.get_text(' ', strip=True) if h else '') or slug
        name = name.strip()
        low = text.lower()
        vdp = 'responsible disclosure' in low
        m_amt = re.search(r'up to\s*\$?\s*([\d,]+)', low)
        bmax = None
        if m_amt:
            try:
                bmax = float(m_amt.group(1).replace(',', ''))
            except ValueError:
                bmax = None
        seen.add(slug)
        out.append(norm({
            'platform': 'intigriti',
            'slug': slug,
            'name': name[:160],
            'url': a['href'],
            'bounty_min': None, 'bounty_max': bmax,
            'reward_raw': ('$%s' % m_amt.group(1)) if bmax else '',
            'tags': (['vdp'] if vdp else []) + ['web'],
            'languages': [],
            'state': 'active',
            'vdp': 1 if vdp else 0,
        }))
    return out


def fetch():
    import requests
    r = requests.get(LIST_URL, headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    return parse_html(r.text)
