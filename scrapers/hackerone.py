"""HackerOne directory scraper: replays the public DirectoryQuery GraphQL op.

No browser needed. Polite cursor walk, 25/page, capped.
"""
import json

from . import UA, TIMEOUT, norm

GQL_URL = 'https://hackerone.com/graphql'
BASE = 'https://hackerone.com'

# Captured from the signed-out directory page (operationName=DirectoryQuery).
QUERY = """query DirectoryQuery($cursor: String, $secureOrderBy: FiltersTeamFilterOrder, $where: FiltersTeamFilterInput) {
  teams(first: 25, after: $cursor, secure_order_by: $secureOrderBy, where: $where) {
    pageInfo { endCursor hasNextPage __typename }
    edges { node {
      id name handle state submission_state launched_at currency
      base_bounty minimum_bounty_table_value
      average_bounty_lower_amount average_bounty_upper_amount
      resolved_report_count profile_picture(size: medium) triage_active
      __typename } __typename } __typename } }"""

WHERE = {"_and": [{"_or": [{"submission_state": {"_eq": "open"}}, {"external_program": {}}]},
                  {"_not": {"external_program": {}}},
                  {"_or": [{"_and": [{"state": {"_neq": "sandboxed"}}, {"state": {"_neq": "soft_launched"}}]},
                           {"external_program": {}}]}]}
ORDER = {"launched_at": {"_direction": "DESC"}}


def _money(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _node_to_program(n):
    handle = n.get('handle') or ''
    name = n.get('name') or handle
    bounty_vals = [_money(n.get('minimum_bounty_table_value')), _money(n.get('base_bounty')),
                   _money(n.get('average_bounty_upper_amount'))]
    bounty_vals = [v for v in bounty_vals if v]
    vdp = 'vdp' in handle.lower() or '(vdp)' in name.lower()
    pic = n.get('profile_picture')
    logo = pic if isinstance(pic, str) and pic.startswith('http') else None
    if isinstance(pic, dict):
        logo = pic.get('url') or pic.get('original') or None
    from tags import parse_upstream_date
    return norm({
        'platform': 'hackerone', 'slug': handle, 'name': name,
        'url': BASE + '/' + handle,
        'bounty_min': min(bounty_vals) if bounty_vals else None,
        'bounty_max': max(bounty_vals) if bounty_vals else None,
        'reward_raw': '',
        'tags': ['vdp'] if vdp else [],
        'languages': [],
        'state': 'active',
        'logo_url': logo,
        'vdp': 1 if vdp else 0,
        'report_count': int(n.get('resolved_report_count') or 0),
        'upstream_listed': parse_upstream_date(n.get('launched_at')),
    })


def parse_teams_payload(data):
    teams = (data.get('data') or {}).get('teams') or {}
    edges = teams.get('edges') or []
    info = teams.get('pageInfo') or {}
    return [_node_to_program(e.get('node') or {}) for e in edges if e.get('node')], info


def get_page(cursor=None, _http=None):
    import requests
    http = _http or requests.post
    variables = {"where": WHERE, "first": 25, "secureOrderBy": ORDER,
                 "product_area": "directory", "product_feature": "programs"}
    if cursor:
        variables["cursor"] = cursor
    r = http(GQL_URL, headers=dict(UA, **{'Content-Type': 'application/json', 'Accept': 'application/json'}),
             json={"operationName": "DirectoryQuery", "query": QUERY, "variables": variables},
             timeout=TIMEOUT)
    r.raise_for_status()
    return parse_teams_payload(r.json())


def fetch_all(max_pages=40, _http=None):
    out, cursor, pages = [], None, 0
    while pages < max_pages:
        progs, info = get_page(cursor, _http=_http)
        if not progs and pages > 0:
            break
        out.extend(progs)
        pages += 1
        if not info.get('hasNextPage'):
            break
        cursor = info.get('endCursor')
        if not cursor:
            break
    return out


def fetch():
    return fetch_all()
