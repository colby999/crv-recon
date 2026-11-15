"""HackenProof directory scraper.

The /programs page embeds the full catalog in the __NUXT_DATA__ devalue
payload: program objects reference shared nodes by integer index, inline
numbers stay literal. We resolve refs with a schema-driven walker (the
program object shape is fixed), which avoids full devalue ambiguity.
"""
import json
import re

from . import UA, TIMEOUT, norm

LIST_URL = 'https://hackenproof.com/programs'
BASE = 'https://hackenproof.com'

REWARD_RE = re.compile(r'\$?\s*([\d,]+(?:\.\d+)?)\s*(k|K|m|M)?')


def parse_reward(raw):
    """Best-effort (min, max) from strings like '$10,000' or '$100 - $250,000'."""
    if not raw:
        return None, None
    nums = []
    for num, mult in REWARD_RE.findall(raw):
        try:
            v = float(num.replace(',', ''))
        except ValueError:
            continue
        if mult and mult.lower() == 'k':
            v *= 1000
        elif mult and mult.lower() == 'm':
            v *= 1000000
        nums.append(v)
    if not nums:
        return None, None
    return (min(nums), max(nums)) if len(nums) > 1 else (nums[0], nums[0])


def _nodes_from_html(html):
    m = re.search(r'<script[^>]*id="__NUXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


def _resolve(nodes, idx):
    """Resolve a devalue node index to a python value (one level).

    Only use for fields known to hold shared nodes (strings/arrays/objects).
    Plain numbers and booleans stay inline in devalue — never resolve those.
    """
    if isinstance(idx, int) and idx >= 0 and idx < len(nodes):
        v = nodes[idx]
        if isinstance(v, (str, list, dict)):
            return v
    return idx


def _ref(nodes, idx):
    """Unconditional index dereference for pagination scalars (Nuxt interns
    shared numbers/nulls: next_page=194 -> node 2, prev_page=21 -> None)."""
    if isinstance(idx, int) and idx >= 0 and idx < len(nodes):
        return nodes[idx]
    return idx


def _resolve_list(nodes, idx):
    v = _resolve(nodes, idx)
    if isinstance(v, list):
        return [_resolve(nodes, x) if isinstance(x, int) else x for x in v]
    return []


def parse_page(html):
    """Returns (programs, next_page or None)."""
    nodes = _nodes_from_html(html)
    if not nodes:
        return [], None
    # find the {"programs": <idx>, ...} holder
    holder = None
    for n in nodes:
        if isinstance(n, dict) and 'programs' in n and 'total_items' in n:
            holder = n
            break
    if holder is None:
        return [], None
    nxt = holder.get('next_page')
    nxt = _ref(nodes, nxt)
    nxt = str(nxt) if isinstance(nxt, int) and nxt > 0 else None
    prog_refs = _resolve(nodes, holder['programs'])
    if not isinstance(prog_refs, list):
        return [], nxt
    out = []
    for ref in prog_refs:
        if not (isinstance(ref, int) and 0 <= ref < len(nodes)):
            continue
        p = nodes[ref]
        if not isinstance(p, dict) or 'slug' not in p or 'name' not in p:
            continue
        slug = _resolve(nodes, p.get('slug'))
        name = _resolve(nodes, p.get('name'))
        if not isinstance(slug, str) or not isinstance(name, str):
            continue
        reward = _resolve(nodes, p.get('reward'))
        reward_raw = reward if isinstance(reward, str) else ''
        bmin, bmax = parse_reward(reward_raw)
        tags_node = _resolve(nodes, p.get('tags'))
        tags, langs = [], []
        if isinstance(tags_node, dict):
            for grp in _resolve_list(nodes, tags_node.get('platforms')):
                if isinstance(grp, str) and grp:
                    tags.append(grp.strip().lower())
            for grp in _resolve_list(nodes, tags_node.get('project_types')):
                if isinstance(grp, str) and grp:
                    tags.append(grp.strip().lower())
            for grp in _resolve_list(nodes, tags_node.get('types')):
                if isinstance(grp, str) and grp:
                    tags.append(grp.strip().lower())
            for grp in _resolve_list(nodes, tags_node.get('languages')):
                if isinstance(grp, str) and grp:
                    langs.append(grp.strip())
        state = _resolve(nodes, p.get('state'))
        end_date = _resolve(nodes, p.get('endDate'))
        start_date = _resolve(nodes, p.get('startDate'))
        last_updated = _resolve(nodes, p.get('lastUpdated'))
        logo = _resolve(nodes, p.get('image'))
        logo_url = (BASE + logo) if isinstance(logo, str) and logo.startswith('/') else None
        reports = p.get('submittedReports')
        reports = reports if isinstance(reports, int) and reports >= 0 else 0
        from tags import parse_upstream_date
        out.append(norm({
            'platform': 'hackenproof',
            'slug': slug,
            'name': name,
            'url': BASE + '/programs/' + slug,
            'bounty_min': bmin, 'bounty_max': bmax, 'reward_raw': reward_raw,
            'tags': sorted(set(tags)), 'languages': sorted(set(langs)),
            'state': state if isinstance(state, str) else 'active',
            'end_date': end_date if isinstance(end_date, str) else None,
            'upstream_listed': parse_upstream_date(start_date),
            'upstream_updated': parse_upstream_date(last_updated),
            'logo_url': logo_url,
            'report_count': reports,
        }))
    return out, nxt


def parse_html(html):
    programs, _ = parse_page(html)
    return programs


def get_page(page=1, _http=None):
    """Fetch one catalog page. _http(url, headers, timeout) injectable for tests."""
    import requests
    http = _http or requests.get
    r = http(LIST_URL + ('?page=%d' % page if page > 1 else ''),
             headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    return parse_page(r.text)


def fetch_all(max_pages=50, _http=None):
    """Walk the whole HackenProof catalog page by page."""
    all_progs, page = [], 1
    while page <= max_pages:
        progs, nxt = get_page(page, _http=_http)
        if not progs and page > 1:
            break
        all_progs.extend(progs)
        if not nxt:
            break
        page += 1
    return all_progs


def fetch():
    return fetch_all()


def parse_program_page(html, slug):
    """Deep parse of one /programs/<slug> page: logo, focus blurb, scope list."""
    import re as _re
    nodes = _nodes_from_html(html)
    if not nodes:
        return None
    key = 'programs-' + slug
    prog = None
    for n in nodes:
        if isinstance(n, dict) and key in n:
            lst = _resolve(nodes, n[key])
            if isinstance(lst, list):
                for el in lst:
                    cand = _ref(nodes, el) if isinstance(el, int) else el
                    if isinstance(cand, dict) and 'scopes' in cand:
                        prog = cand
                        break
            break
    if prog is None:
        return None

    def txt(v):
        r = _resolve(nodes, v) if isinstance(v, int) else v
        return r if isinstance(r, str) else ''

    logo = txt(prog.get('logo'))
    scopes = _resolve(nodes, prog.get('scopes'))
    out_scopes = []
    if isinstance(scopes, list):
        for s in scopes:
            s = _ref(nodes, s) if isinstance(s, int) else s
            if not isinstance(s, dict):
                continue
            target = txt(s.get('target'))[:300]
            if not target:
                continue
            oos = _ref(nodes, s.get('out_of_scope')) if isinstance(s.get('out_of_scope'), int) else s.get('out_of_scope')
            out_scopes.append({
                'target': target,
                'title': txt(s.get('title'))[:160],
                'eligible': not (oos is True),
            })
    focus_raw = txt(prog.get('focusArea'))
    focus_raw = _re.sub(r'\s+', ' ', focus_raw.replace('\r', ' ').replace('#', '')).strip()
    return {
        'logo_url': (BASE + logo) if logo.startswith('/') else (logo or None),
        'blurb': focus_raw[:400],
        'scopes': out_scopes,
    }


def fetch_program(slug, _http=None):
    import requests
    http = _http or requests.get
    r = http(BASE + '/programs/' + slug, headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    return parse_program_page(r.text, slug)


def fetch_deep(slugs, _http=None, delay=0.4, limit=100):
    """Polite deep fetch for a slug list. Returns {slug: parsed}."""
    import time as _time
    out = {}
    for slug in slugs[:limit]:
        try:
            parsed = fetch_program(slug, _http=_http)
            if parsed:
                out[slug] = parsed
        except Exception:
            pass
        if delay and _http is None:
            _time.sleep(delay)
    return out
