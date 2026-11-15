"""One-shot ingest: python refresh.py [--live] [--db PATH]."""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import app as radar_app
import db


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--live', action='store_true')
    ap.add_argument('--seed', action='store_true',
                    help='force (re)ingest of offline sample rows')
    ap.add_argument('--prune-seed', action='store_true',
                    help='delete sample (seed=1) rows after live data takes over')
    ap.add_argument('--deep', action='store_true',
                    help='fetch HP program pages for scope/logos (slow, polite)')
    ap.add_argument('--deep-limit', type=int, default=100)
    ap.add_argument('--db', default=None)
    a = ap.parse_args()
    db.init_db(a.db)
    if a.prune_seed:
        n = radar_app.prune_seed(a.db)
        print('pruned %d seed programs' % n)
        return
    n = radar_app.refresh_once(a.db, live=a.live, seed=a.seed)
    if a.deep:
        import tracker as tracker_mod
        cxd = db.connect(a.db)
        filled, tried = tracker_mod.deep_fill_hp(cxd, limit=a.deep_limit)
        print('deep fill HP: %d/%d program pages with scope' % (filled, tried))
        bfilled, btried = tracker_mod.deep_fill_bc(cxd, limit=min(a.deep_limit, 30))
        print('deep fill BC: %d/%d briefs with scope' % (bfilled, btried))
    print('ingested/updated programs (new=%d) live=%s' % (n, a.live))
    cx = db.connect(a.db)
    print('programs=%d targets=%d changes=%d' % (
        cx.execute('SELECT COUNT(*) c FROM programs').fetchone()['c'],
        cx.execute('SELECT COUNT(*) c FROM targets').fetchone()['c'],
        cx.execute('SELECT COUNT(*) c FROM changes').fetchone()['c']))


if __name__ == '__main__':
    main()
