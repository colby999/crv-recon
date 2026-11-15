"""Bugcrowd scraper: public engagement_listings.json (+ featured collections)."""
import re

from . import UA, TIMEOUT, norm

LIST_URL = 'https://bugcrowd.com/engagement_listings.json'

_MONEY = re.compile(r'\$?\s*([\d,]+(?:\.\d+)?)\s*(k)?', re.I)


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
    if m.group(2):
        v *= 1000
    return v


def _eng_to_program(e):
    url = e.get('briefUrl') or ''
    slug = url.rstrip('/').split('/')[-1] if url else (e.get('name') or '')
    rs = e.get('rewardSummary') or {}
    lo = _money(rs.get('minReward'))
    hi = _money(rs.get('maxReward'))
    etype = ((e.get('productEngagementType') or {}).get('label') or '').strip()
    vdp = etype.lower().startswith('vdp') or etype.lower().startswith('disclosure')
    tags = []
    if etype:
        tags.append(etype.lower())
    if e.get('industryName'):
        tags.append(str(e['industryName']).strip().lower())
    return norm({
        'platform': 'bugcrowd', 'slug': slug, 'name': e.get('name') or slug,
        'url': url, 'bounty_min': lo, 'bounty_max': hi,
        'reward_raw': rs.get('summary') or '',
        'tags': tags, 'languages': [],
        'state': 'active' if (e.get('accessStatus') or 'open') == 'open' else 'suspended',
        'logo_url': e.get('logoUrl'),
        'vdp': 1 if vdp else 0,
        'report_count': 0,
    })


def parse_list_payload(data):
    engs = (data.get('engagements') if isinstance(data, dict) else data) or []
    return [_eng_to_program(e) for e in engs if isinstance(e, dict) and (e.get('briefUrl') or e.get('name'))]


def get_page(page=1, _http=None):
    import requests
    http = _http or requests.get
    url = LIST_URL if page <= 1 else '%s?page=%d' % (LIST_URL, page)
    r = http(url, headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    return parse_list_payload(r.json())


def fetch_all(max_pages=25, _http=None):
    out, page = [], 1
    while page <= max_pages:
        progs = get_page(page, _http=_http)
        if not progs and page > 1:
            break
        out.extend(progs)
        if len(progs) < 24:
            break
        page += 1
    return out


def fetch():
    return fetch_all()


_URL_RE = re.compile(r'https?://[^\s<>"\']+')
_BARE_RE = re.compile(r'(?<![\w@/-])(?:[a-z0-9][a-z0-9-]{1,60}\.)+(?:com|io|net|org|xyz|app|dev|ai|finance|mx|ar|co|in|us|uk|eu|site|online|tech|software|cloud|network|de|fr|nl|ch|sg|hk|jp|br|au|ca)\b(?![\w/])', re.I)


def parse_program_html(html):
    """Split rendered brief at the out-of-scope marker; harvest target URLs."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, 'html.parser')
    text = soup.get_text('\n')
    marks = [m.start() for m in re.finditer(r'Out[\s-]?of[\s-]?Scope', text, re.I)]
    cut = marks[0] if marks else len(text)
    in_part, out_part = text[:cut], text[cut:]

    def harvest(part):
        found, seen = [], set()
        for m in _URL_RE.finditer(part):
            u = m.group(0).rstrip(').,;')
            if u not in seen and len(u) < 200:
                seen.add(u)
                found.append(u)
        for m in _BARE_RE.finditer(part):
            d = m.group(0).lower()
            if d not in seen:
                seen.add(d)
                found.append(d)
        return found

    scopes = [{'target': t, 'title': '', 'eligible': True} for t in harvest(in_part)]
    scopes += [{'target': t, 'title': '', 'eligible': False} for t in harvest(out_part)]
    return {'scopes': scopes}


def fetch_program_stats(slug, _http=None):
    """Cheap requests-only report counts for one engagement."""
    import requests
    http = _http or requests.get
    r = http('https://bugcrowd.com/engagements/%s/statistics' % slug,
             headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def render_program(slug):
    from . import _browser
    return _browser.render_html('https://bugcrowd.com/engagements/' + slug,
                                scrolls=2, wait=5)


def fetch_changelog(slug, _http=None):
    """Requests-only brief data: launch date + structured scope from the
    engagement changelog JSON (uuid sits in the SSR shell)."""
    import requests
    http = _http or requests.get
    shell = http('https://bugcrowd.com/engagements/%s' % slug,
                 headers=UA, timeout=TIMEOUT)
    shell.raise_for_status()
    m = re.search(r'changelog/([0-9a-f-]{36})', shell.text)
    if not m:
        return None
    r = http('https://bugcrowd.com/engagements/%s/changelog/%s.json' % (slug, m.group(1)),
             headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    cl = r.json()
    data = (cl.get('data') or {}) if isinstance(cl, dict) else {}
    eng = data.get('engagement') or {}
    scopes = []
    for group in data.get('scope') or []:
        gname = (group.get('name') or '').strip().lower()
        in_scope = gname.startswith('in')
        for t in group.get('targets') or []:
            ident = (t.get('uri') or t.get('name') or '').strip()
            if ident:
                scopes.append({'target': ident, 'title': t.get('name') or '',
                               'eligible': in_scope})
    return {'startsAt': eng.get('startsAt'), 'scopes': scopes}
