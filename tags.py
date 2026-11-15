"""Canonical tag taxonomy + lifecycle helpers.

Scope tags follow the classic aggregator set plus the web3-native tags HackenProof's
catalog actually uses. Unknown raw tags are preserved verbatim
(lowercased, dashed) so new platform vocab shows up instead of vanishing.
"""
import re
from datetime import datetime, timezone

SCOPE_TAGS = [
    'ai-ml', 'api', 'blockchain-dlt', 'cidr', 'domain', 'executable',
    'hardware', 'iot', 'ip', 'mobile', 'network', 'other', 'rwa',
    'smart-contract', 'source-code', 'wildcard',
    'web', 'web3', 'defi', 'dex', 'nft', 'wallet', 'bridge', 'staking',
    'stablecoin', 'lending',
]

LANGUAGES = ['Bitcoin Script', 'C', 'C++', 'Cairo', 'Clarity', 'Go', 'Java',
             'JavaScript', 'Kotlin', 'Move', 'Objective-C', 'Python', 'Ruby',
             'Rust', 'Solidity', 'Sway', 'Swift', 'TypeScript', 'Vyper', 'Yul']

_ALIAS = {
    'smart contract': 'smart-contract',
    'smart-contracts': 'smart-contract',
    'source code': 'source-code',
    'blockchain': 'blockchain-dlt',
    'blockchain/dlt': 'blockchain-dlt',
    'artificial intelligence': 'ai-ml',
    'ai': 'ai-ml',
    'machine learning': 'ai-ml',
    'web2': 'web',
    'website': 'web',
    'websites': 'web',
    'app': 'mobile',
    'apps': 'mobile',
    'application': 'mobile',
    'dapp': 'web3',
    'l1/l2': 'blockchain-dlt',
    'l1': 'blockchain-dlt',
    'l2': 'blockchain-dlt',
    'cex': 'other',
    'infrastructure': 'network',
    'defi ': 'defi',
    'nft marketplace': 'nft',
    'nfts': 'nft',
    'telegram app': 'mobile',
    'automotive': 'other',
    'gamefi': 'web3',
    'sui ecosystem': 'blockchain-dlt',
    'sui': 'blockchain-dlt',
    'solana': 'blockchain-dlt',
    'near': 'blockchain-dlt',
    'staking ': 'staking',
    'execution environment/engine': 'other',
    'prediction market': 'defi',
}

_LANG_ALIAS = {
    'typescript': 'TypeScript', 'javascript': 'JavaScript', 'c++': 'C++',
    'solidity ': 'Solidity', 'rust ': 'Rust', 'move ': 'Move', 'go ': 'Go',
    'wasm': 'Other', 'func': 'FunC',
}


def canon_tag(raw):
    t = (raw or '').strip().lower().replace('_', ' ').strip()
    if not t:
        return None
    if t in _ALIAS:
        return _ALIAS[t]
    slug = re.sub(r'\s+', '-', t)
    if slug in SCOPE_TAGS:
        return slug
    return slug  # preserve new platform vocab verbatim


def canon_lang(raw):
    t = (raw or '').strip()
    if not t:
        return None
    low = t.lower()
    if low in _LANG_ALIAS:
        return _LANG_ALIAS[low]
    for known in LANGUAGES:
        if known.lower() == low:
            return known
    return t  # preserve (e.g. FunC, Cairo variants)


def normalize_program(p):
    p['tags'] = sorted({c for c in (canon_tag(t) for t in p.get('tags', [])) if c})
    p['languages'] = sorted({c for c in (canon_lang(l) for l in p.get('languages', [])) if c})
    return p


def lifecycle(raw_state, end_date=None):
    """Map platform state + optional end date to active/upcoming/suspended/ended."""
    s = (raw_state or 'active').strip().lower()
    if s in ('paused', 'suspended', 'disabled'):
        return 'suspended'
    if end_date:
        try:
            dt = datetime.fromisoformat(str(end_date).replace('Z', '+00:00'))
            now = datetime.now(timezone.utc)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if dt > now:
                return 'upcoming'
            return 'ended'
        except Exception:
            pass
    if s in ('upcoming', 'soon', 'draft', 'pending'):
        return 'upcoming'
    if s in ('ended', 'closed', 'finished', 'archived'):
        return 'ended'
    return 'active'


def is_new(first_seen, days=7):
    try:
        dt = datetime.strptime((first_seen or '')[:19], '%Y-%m-%dT%H:%M:%S')
        return (datetime.utcnow() - dt).days <= days
    except Exception:
        return False


_LANG_FOCUS = {
    'solidity': 'EVM', 'vyper': 'EVM', 'yul': 'EVM',
    'bitcoin script': 'Bitcoin', 'clarity': 'Bitcoin',
    'cairo': 'Starknet', 'move': 'MoveVM',
}
_TAG_FOCUS = {
    'defi': 'DeFi', 'dex': 'DeFi', 'lending': 'Lending', 'staking': 'Staking',
    'nft': 'NFT', 'bridge': 'Bridge', 'wallet': 'Wallet', 'mobile': 'Mobile',
    'api': 'API', 'ai-ml': 'AI', 'web': 'Web', 'stablecoin': 'Stablecoin',
    'rwa': 'RWA', 'iot': 'IoT', 'hardware': 'Hardware', 'executable': 'Binary',
}


def derive_focus(p):
    """Short 'what this bounty is about' chips: EVM, Solana, DeFi… (max 4)."""
    out = []
    langs = [str(x).lower() for x in p.get('languages', [])]
    tags = [str(x).lower() for x in p.get('tags', [])]
    for lang in langs:
        chip = _LANG_FOCUS.get(lang)
        if chip and chip not in out:
            out.append(chip)
    if 'solana' in tags and 'Solana' not in out:
        out.append('Solana')
    for tag in tags:
        chip = _LANG_FOCUS.get(tag) or _TAG_FOCUS.get(tag)
        if chip and chip not in out:
            out.append(chip)
    if 'smart-contract' in tags and not [c for c in out if c in ('EVM', 'Solana', 'Bitcoin', 'Starknet', 'MoveVM')]:
        out.append('Smart contracts')
    return out[:4]


def rel_time(iso):
    """Human relative time: '3d ago', 'in 12d', 'today'."""
    try:
        dt = datetime.strptime((iso or '')[:19], '%Y-%m-%dT%H:%M:%S').replace(tzinfo=timezone.utc)
    except Exception:
        return '?'
    delta = datetime.now(timezone.utc) - dt
    secs = int(delta.total_seconds())
    future = secs < 0
    a = abs(secs)
    if a < 3600:
        s = '%dm' % max(1, a // 60)
    elif a < 86400:
        s = '%dh' % (a // 3600)
    elif a < 86400 * 30:
        s = '%dd' % (a // 86400)
    elif a < 86400 * 365:
        s = '%dmo' % (a // (86400 * 30))
    else:
        s = '%dy' % (a // (86400 * 365))
    return ('in ' + s) if future else (s + ' ago')


_MONTHS = {m.lower(): i for i, m in enumerate(
    ('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'), 1)}


def parse_upstream_date(raw):
    """Normalize upstream platform dates to ISO 'YYYY-MM-DD' (or None).
    Handles: '30 Oct 2026', '2026-08-03', ISO timestamps, '13/9/2026' (d/m/y)."""
    s = (raw or '').strip()
    if not s:
        return None
    m = re.match(r'^(\d{4})-(\d{2})-(\d{2})', s)
    if m:
        return '%s-%s-%s' % m.groups()
    m = re.match(r'^(\d{1,2})\s+([A-Za-z]{3,})\s+(\d{4})$', s)
    if m and m.group(2).lower() in _MONTHS:
        return '%s-%02d-%02d' % (m.group(3), _MONTHS[m.group(2).lower()], int(m.group(1)))
    m = re.match(r'^(\d{1,2})/(\d{1,2})/(\d{4})$', s)
    if m:
        return '%s-%02d-%02d' % (m.group(3), int(m.group(2)), int(m.group(1)))
    return None


def detect_vdp(p):
    """Explicit-only VDP/free detection. Reward-less != free."""
    blob = ' '.join([p.get('name', ''), p.get('reward_raw', ''),
                     ' '.join(p.get('tags', []))]).lower()
    if re.search(r'\bvdp\b|responsible disclosure|hall of fame|no monetary|swag only|bounty:?\s*\$0\b', blob):
        return True
    return False
