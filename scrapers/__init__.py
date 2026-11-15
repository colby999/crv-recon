"""Live scrapers. Each returns a list of normalized program dicts:
{platform, slug, name, url, bounty_min, bounty_max, reward_raw, tags[], languages[],
 state, vdp, is_contest, report_count, targets[]}
All parsers must also work on saved fixtures (offline) for tests."""
import re

UA = {'User-Agent': 'crv-recon/1.0 (+local research aggregator)'}
TIMEOUT = 25


def norm(p):
    p.setdefault('tags', [])
    p.setdefault('languages', [])
    p.setdefault('state', 'active')
    p.setdefault('targets', [])
    return p


def next_flight_text(html):
    """Join a Next.js App-Router flight payload (self.__next_f pushes) into
    one plain text blob, with embedded JSON readable."""
    import json
    chunks = re.findall(r'self\.__next_f\.push\(\[1,"((?:[^"\\]|\\.)*)"\]\)', html)
    return ''.join(json.loads('"' + c + '"') for c in chunks)
