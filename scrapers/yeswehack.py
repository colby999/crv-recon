"""YesWeHack directory scraper (server-rendered program cards)."""
import re

from . import UA, TIMEOUT, norm

LIST_URL = 'https://yeswehack.com/programs'
BASE = 'https://yeswehack.com'

REWARD_RE = re.compile(r'([\d\s,]+)\s*€|\$\s*([\d\s,]+)', re.I)


def _parse_reward(text):
    nums = []
    for a, b in REWARD_RE.findall(text or ''):
        raw = (a or b).replace(' ', '').replace(',', '')
        try:
            nums.append(float(raw))
        except ValueError:
            pass
    if not nums:
        return None, None, ''
    lo, hi = min(nums), max(nums)
    return (lo, hi, ('%g - %g' % (lo, hi)) if hi > lo else ('%g' % hi))


def parse_html(html):
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, 'html.parser')
    out, seen = [], set()
    for card in soup.select('ywh-program-card, [class*="program-card"], article, [data-program]'):
        a = card.find('a', href=re.compile(r'^/programs/'))
        if not a:
            continue
        slug = a['href'].split('/programs/')[-1].strip('/').split('?')[0]
        if not slug or slug in seen:
            continue
        text = card.get_text(' ', strip=True)
        name = a.get_text(' ', strip=True) or slug.replace('-', ' ').title()
        logo_url = None
        for img in card.find_all('img'):
            src = img.get('src', '')
            if src.startswith('http') and 'default_business_unit_logo' not in src and 'divider' not in src:
                logo_url = src
                break
        lo, hi, raw = _parse_reward(text)
        low = text.lower()
        m_upd = re.search(r'Last update on\s+(\d{4}-\d{2}-\d{2})', text)
        tags = [t for t in ('domain', 'mobile', 'api', 'web', 'iot', 'hardware',
                            'source code', 'smart contract', 'blockchain')
                if t in low]
        langs = [l for l in ('Solidity', 'Rust', 'Go', 'Python', 'JavaScript',
                             'TypeScript', 'C++', 'Java', 'Ruby')
                 if l.lower() in low]
        seen.add(slug)
        out.append(norm({
            'platform': 'yeswehack',
            'slug': slug,
            'name': name[:160],
            'url': BASE + '/programs/' + slug,
            'bounty_min': lo, 'bounty_max': hi, 'reward_raw': raw,
            'tags': tags, 'languages': langs,
            'logo_url': logo_url,
            'upstream_updated': m_upd.group(1) if m_upd else None,
        }))
    # fallback: bare links when card markup changes
    if not out:
        for m in set(re.findall(r'href="(/programs/[a-z0-9][a-z0-9\-]{2,80})"', html)):
            slug = m.split('/programs/')[-1]
            out.append(norm({
                'platform': 'yeswehack', 'slug': slug,
                'name': slug.replace('-', ' ').title(),
                'url': BASE + m,
            }))
    return out


def fetch():
    import requests
    r = requests.get(LIST_URL, headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    return parse_html(r.text)
