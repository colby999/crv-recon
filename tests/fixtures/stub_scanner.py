"""Test stub for the external leak scanner (never touches real tooling)."""
import json
import sys
import time

target = sys.argv[1] if len(sys.argv) > 1 else ''
if '--slow' in sys.argv:
    time.sleep(10)
if '--exit1' in sys.argv:
    # crv-leak-scan semantics: findings at/above --fail-on threshold
    print(json.dumps({'summary': {'findings': 2, 'bySeverity': {'critical': 1, 'high': 1}}}))
    sys.exit(1)
if '--exit2' in sys.argv:
    print('bad usage', file=sys.stderr)
    sys.exit(2)
print(json.dumps({'summary': {'findings': 0, 'bySeverity': {}}, 'target': target}))
