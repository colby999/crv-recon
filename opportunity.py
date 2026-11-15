"""Free opportunity scoring (every Pro-style tier, unlocked by default)."""
from datetime import datetime, timezone

TIERS = ('Elite', 'Hot', 'Strong', 'Potential')


def age_days(first_seen):
    try:
        dt = datetime.strptime(first_seen[:19], '%Y-%m-%dT%H:%M:%S').replace(tzinfo=timezone.utc)
        return max(0, (datetime.now(timezone.utc) - dt).days)
    except Exception:
        return 9999


def score_program(row):
    """Return (tier_or_None, score_0_100, reason). Fresh + high bounty + few reports wins."""
    d = dict(row) if not isinstance(row, dict) else row
    bounty = d.get('bounty_max') or 0
    reports = d.get('report_count') or 0
    age = age_days(d.get('first_seen') or '')
    score = 0
    if age <= 7:
        score += 40
    elif age <= 30:
        score += 25
    elif age <= 90:
        score += 10
    if bounty >= 100000:
        score += 35
    elif bounty >= 25000:
        score += 25
    elif bounty >= 5000:
        score += 15
    elif bounty > 0:
        score += 8
    if reports == 0:
        score += 25
    elif reports <= 5:
        score += 15
    elif reports <= 20:
        score += 5
    score = min(100, score)
    if score >= 85:
        tier, why = 'Elite', 'very fresh, lightly reported, strong payout potential'
    elif score >= 70:
        tier, why = 'Hot', 'fresh with low visible competition and attractive rewards'
    elif score >= 55:
        tier, why = 'Strong', 'paid program with low public report count'
    elif score >= 40:
        tier, why = 'Potential', 'paid program that may be overlooked'
    else:
        return None, score, 'older or heavily reported program'
    return tier, score, why


def score_target(bounty_max, report_count=0, target_type='domain'):
    base = 50
    if (bounty_max or 0) >= 100000:
        base += 25
    elif (bounty_max or 0) >= 10000:
        base += 15
    if target_type in ('domain', 'api'):
        base += 10
    if (report_count or 0) == 0:
        base += 10
    return min(100, base)
