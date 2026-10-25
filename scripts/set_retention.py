#!/usr/bin/env python3
"""Set Claude Code transcript retention (cleanupPeriodDays) to 365 days, backing up settings.json first.

Run it yourself on each machine (auto mode will not let Claude edit ~/.claude/settings.json):
  python3 -I set_retention.py
  . ../hosts.env; ssh "$PRO_HOST" 'python3 -I -' < set_retention.py
  . ../hosts.env; ssh "$LAB_HOST" 'python3 -I -' < set_retention.py
"""
import json, os, shutil, time

p = os.path.expanduser('~/.claude/settings.json')
shutil.copy2(p, p + '.bak.' + time.strftime('%y%m%d'))
d = json.load(open(p))
d['cleanupPeriodDays'] = 365
json.dump(d, open(p, 'w'), indent=2, ensure_ascii=False)
print(os.uname().nodename, 'cleanupPeriodDays ->', json.load(open(p)).get('cleanupPeriodDays'))
