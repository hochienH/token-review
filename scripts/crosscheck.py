#!/usr/bin/env python3
"""token-review / crosscheck.py

Compare build.py's daily cost for one host with ccusage's daily report:

  npx -y ccusage@20.0.42 daily --json --since YYYYMMDD --timezone Asia/Tokyo > ccusage_air.json
  crosscheck.py OUT_DIR air ccusage_air.json

The snapshot day itself is skipped (analyze.py ran partway through it).
Writes OUT_DIR/crosscheck.json; build.py merges it into the meta dataset.
"""
import collections, json, os, sys


def main():
    out, host, path = sys.argv[1], sys.argv[2], sys.argv[3]
    ours = collections.defaultdict(float)
    for r in json.load(open(os.path.join(out, 'daily.json'))):
        if r['host'] == host:
            ours[r['day']] += r['cost']
    theirs = {r['period']: r['totalCost'] for r in json.load(open(path))['daily']}
    last = max(ours) if ours else None
    days = sorted(d for d in set(ours) | set(theirs) if d != last)
    a, b = sum(ours.get(d, 0) for d in days), sum(theirs.get(d, 0) for d in days)
    worst = max(days, key=lambda d: abs(ours.get(d, 0) - theirs.get(d, 0))) if days else None
    row = {'host': host, 'days': len(days), 'until': days[-1] if days else None,
           'ours': round(a, 2), 'ccusage': round(b, 2), 'ratio': round(a / b, 4) if b else None,
           'worst_day': worst, 'worst_diff': round(ours.get(worst, 0) - theirs.get(worst, 0), 2) if worst else None}
    rows = [r for r in (json.load(open(os.path.join(out, 'crosscheck.json')))
                        if os.path.exists(os.path.join(out, 'crosscheck.json')) else []) if r['host'] != host]
    json.dump(rows + [row], open(os.path.join(out, 'crosscheck.json'), 'w'), ensure_ascii=False, indent=1)
    print(json.dumps(row, ensure_ascii=False))


if __name__ == '__main__':
    main()
