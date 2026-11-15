"""Offline seed so the app is useful before/without network."""
from . import norm

SEED = [
    ('hackenproof', 'whitechain', 'Whitechain', 200.0, 10000.0, ['domain', 'smart contract', 'source code'], {}),
    ('hackenproof', 'rhea-finance', 'Rhea Finance Smart Contracts', 100.0, 250000.0, ['smart contract', 'source code'], {}),
    ('yeswehack', 'doctolib-public-bug-bounty-program', 'Doctolib', 250.0, 3000.0, ['domain', 'api', 'mobile'], {}),
    ('yeswehack', 'blablacar-bug-bounty-program', 'BlaBlaCar', 100.0, 2000.0, ['domain', 'mobile', 'api'], {}),
    ('immunefi', 'chainlink', 'Chainlink', 1000.0, 500000.0, ['smart contract', 'solidity', 'blockchain'], {}),
    ('hackerone', 'snapchat', 'Snapchat', 500.0, 15000.0, ['domain', 'mobile', 'api'], {}),
    ('bugcrowd', 'tesla', 'Tesla', 500.0, 15000.0, ['domain', 'api', 'automotive'], {}),
    ('intigriti', 'asos', 'ASOS', 100.0, 2500.0, ['domain', 'web'], {}),
    ('cantina', 'v4lend-competition', 'v4lend Competition', 0.0, 50000.0, ['solidity', 'contest', 'source code'], {}),
    ('sherlock', 'kamu-syllabus', 'Kamu', 0.0, 250000.0, ['smart contract', 'contest'], {}),
    # upcoming (future-dated) + free/VDP samples so those lanes render offline
    ('cantina', 'zephyr-audit', 'Zephyr Audit Competition', 0.0, 100000.0,
     ['solidity', 'contest', 'source code'],
     {'state': 'upcoming', 'end_date': '2999-02-01T00:00:00Z', 'is_contest': 1}),
    ('sherlock', 'nimbus-contest', 'Nimbus upcoming contest', 0.0, 75000.0,
     ['smart contract', 'contest'],
     {'state': 'upcoming', 'end_date': '2999-03-01T00:00:00Z', 'is_contest': 1}),
    ('yeswehack', 'example-vdp', 'Example Responsible Disclosure (VDP)', None, None, ['domain'],
     {'vdp': 1, 'reward_raw': 'Hall of fame — no monetary bounty'}),
    ('hackerone', 'example-disclosure', 'Example Disclosure Program', None, None, ['domain', 'api'],
     {'vdp': 1, 'reward_raw': 'VDP — responsible disclosure only'}),
]


def _reward(lo, hi, extra):
    if extra.get('reward_raw'):
        return extra['reward_raw']
    if lo is None or hi is None:
        return ''
    return '%g - %g' % (lo, hi)


def fetch():
    out = []
    for plat, slug, name, lo, hi, tags, extra in SEED:
        d = {
            'platform': plat, 'slug': slug, 'name': name,
            'url': 'https://example.com/programs/' + slug,
            'bounty_min': lo, 'bounty_max': hi,
            'reward_raw': _reward(lo, hi, extra),
            'tags': tags,
            'targets': [name.lower().replace(' ', '') + '.com'],
            'seed': 1,
            'upstream_listed': '2026-10-30',
            'upstream_updated': '2026-11-01',
        }
        d.update(extra)
        out.append(norm(d))
    return out
