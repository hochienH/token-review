#!/usr/bin/env python3
"""token-review / build.py

Merge the per-host JSON from analyze.py, apply API prices, flag suspicious
sessions, and write the dashboard datasets:

  build.py OUT_DIR host1.json host2.json ...

Writes OUT_DIR/{sessions,waste,waste_kinds,meta}.json (dashboard),
OUT_DIR/daily.json (for crosscheck.py against ccusage), OUT_DIR/judge_input.json
(digests of flagged sessions, local only) and prints a cost check against
Claude Code's own cost-state records. Cost totals and fixed overhead are left
to ccusage and Claude Code's /context.
"""
import collections, json, os, re, statistics, sys, time

# $/MTok. Source: claude-api skill model table (cached 2026-10-06).
# in/out/cr are published; cache writes use the standard 1.25x (5m) / 2x (1h) of input,
# and cr for opus-5 / haiku-5-5 uses the standard 0.1x. build.py checks these against cost-state.
PRICES = {
    'claude-opus-5-5':   {'in': 4.0,  'out': 20.0, 'cr': 0.20},
    'claude-opus-5':     {'in': 5.0,  'out': 25.0, 'cr': 0.50},
    'claude-fable-5-1':  {'in': 10.0, 'out': 50.0, 'cr': 0.25},
    'claude-sonnet-5-5': {'in': 2.0,  'out': 10.0, 'cr': 0.20},
    'claude-haiku-5-5':  {'in': 0.10, 'out': 0.50, 'cr': 0.01},
    'claude-sonnet-5':   {'in': 2.0,  'out': 10.0, 'cr': 0.20},
    'claude-haiku-4-5':  {'in': 1.0,  'out': 5.0,  'cr': 0.10},
}
for p in PRICES.values():
    p['cw5'], p['cw1h'] = p['in'] * 1.25, p['in'] * 2.0

SOURCE = {'claude-vscode': 'IDE', 'cli': 'Terminal', 'claude-desktop': 'Desktop', 'sdk-py': 'Agent SDK',
          'sdk-ts': 'Agent SDK', 'sdk-cli': 'claude -p'}
KIND = {'big': '大型 tool 輸出', 'reread': '重複讀同一檔', 'retry': '失敗後原樣重試', 'rebuild': 'cache 重建'}
unknown_models = collections.Counter()


def price(model):
    p = PRICES.get(model) or PRICES.get(re.sub(r'-\d{8}$', '', model or ''))
    if p is None:
        unknown_models[model] += 1
    return p


def cost(model, v):
    """v = [in, cw5, cw1h, cr, out, think] -> USD"""
    p = price(model)
    if not p:
        return 0.0
    return (v[0] * p['in'] + v[1] * p['cw5'] + v[2] * p['cw1h'] + v[3] * p['cr'] + v[4] * p['out']) / 1e6


def item_cost(it):
    p = price(it['model'])
    if not p:
        return 0.0
    pw = p['cw1h'] if it['w1h'] else p['cw5']
    if it['k'] == 'rebuild':      # rewritten instead of read
        return it['tok'] * (pw - p['cr']) / 1e6
    return it['tok'] * (pw + p['cr'] * it['rem']) / 1e6


def project(cwd):
    parts = [x for x in cwd.replace('~', '').split('/') if x]
    if parts[:1] == ['nfs']:          # /nfs/<server>/dataN/<me>/<project>/...
        parts = parts[4:]
    return '/'.join(parts[:2]) or '~'


def main():
    out_dir, paths = sys.argv[1], sys.argv[2:]
    chains = []
    for p in paths:
        chains += json.load(open(p))['chains']

    for c in chains:
        c['cost'] = sum(cost(m, v) for m, v in c['tok'].items())
        for it in c['items']:
            it['cost'] = item_cost(it)

    # ---- sessions = main chain + its child chains --------------------------------------
    mains = {(c['host'], c['session']): c for c in chains if c['kind'] == 'main'}
    children = collections.defaultdict(list)
    for c in chains:
        if c['kind'] != 'main':
            children[(c['host'], c['parent'])].append(c)

    # cost check: our price model vs Claude Code's own totalCostUSD
    ratios, med = [], None
    for key, c in mains.items():
        cs = (c.get('cost_state') or {}).get('usd')
        mine = c['cost'] + sum(x['cost'] for x in children.get(key, []))
        if cs and cs > 0.05:
            ratios.append(mine / cs)
    if ratios:
        med = statistics.median(ratios)
        print('cost check vs cost-state: n=%d median=%.3f p10=%.3f p90=%.3f' % (
            len(ratios), med, sorted(ratios)[len(ratios) // 10],
            sorted(ratios)[len(ratios) * 9 // 10]))

    sessions, judge_pool = [], []
    for key, c in mains.items():
        kids = children.get(key, [])
        sub_cost = sum(x['cost'] for x in kids)
        allc = [c] + kids
        waste = sum(it['cost'] for x in allc for it in x['items'])
        oc = collections.Counter()
        neg = collections.Counter()
        for x in allc:
            oc.update(x['outcome'])
            neg.update(x['neg'])
        total = c['cost'] + sub_cost
        src = SOURCE.get(c['entrypoint'], c['entrypoint'] or '?')
        row = {
            'session': c['session'][:8], 'host': c['host'], 'project': project(c['cwd']),
            'title': c['title'] or (c['first_prompt'] or '')[:60], 'source': src,
            'day': c['start'] and time.strftime('%Y-%m-%d', time.gmtime(c['start'] + 9 * 3600)),
            'model': c['model'], 'calls': c['n_req'] + sum(x['n_req'] for x in kids), 'subagents': len(kids),
            'cost': round(total, 4), 'sub_cost': round(sub_cost, 4), 'waste_cost': round(waste, 4),
            'edits': oc['edits'], 'commits': oc['commits'], 'artifacts': oc['artifacts'],
            'interrupts': neg['interrupts'], 'corrections': neg['corrections'], 'errors': neg['errors'],
            'flag': '', 'verdict': '', 'reason': '', 'fix': '',
        }
        flags = []
        produced = oc['edits'] + oc['commits'] + oc['artifacts']
        if total >= 1 and produced == 0 and src != 'Agent SDK':
            flags.append('高花費無產出')
        if neg['interrupts'] + neg['corrections'] >= 3:
            flags.append('多次糾正或中斷')
        if total >= 0.5 and waste / total >= 0.3:
            flags.append('浪費比例高')
        row['flag'] = '、'.join(flags)
        sessions.append(row)
        if flags:
            top = sorted((it for x in allc for it in x['items']), key=lambda it: -it['cost'])[:4]
            tc = collections.Counter()
            for x in allc:
                tc.update(x['tool_counts'])
            judge_pool.append((total, {
                'id': row['session'], 'host': row['host'], 'project': row['project'], 'title': row['title'],
                'source': src, 'cost_usd': round(total, 2), 'calls': row['calls'], 'subagents': len(kids),
                'flags': flags, 'outcome': dict(oc), 'negative': dict(neg),
                'top_waste': [{'kind': KIND[it['k']], 'detail': it['detail'], 'usd': round(it['cost'], 2)} for it in top],
                'tool_counts': dict(tc.most_common(8)),
                'user_messages': c['user_texts'][:10], 'final_reply': c['last_text'],
            }))
    sessions.sort(key=lambda r: -r['cost'])

    # ---- daily (crosscheck only) ---------------------------------------------------
    daily = collections.defaultdict(float)
    for c in chains:
        src = SOURCE.get(c['entrypoint'], c['entrypoint'] or '?')
        for k, v in c['days'].items():
            d, m = k.split('|', 1)
            daily[(d, c['host'], src)] += cost(m, v)
    daily_rows = [{'day': d, 'host': h, 'source': s, 'cost': round(v, 4)}
                  for (d, h, s), v in sorted(daily.items()) if d != 'None']

    # ---- waste items -------------------------------------------------------------------
    items = []
    for c in chains:
        sess = c['session'][:8] if c['kind'] == 'main' else (c['parent'] or '')[:8]
        for it in c['items']:
            items.append({'kind': KIND[it['k']], 'tool': it['tool'] or it.get('cause') or '', 'detail': it['detail'],
                          'session': sess, 'host': c['host'], 'project': project(c['cwd']),
                          'where': '主線' if c['kind'] == 'main' else 'subagent',
                          'tokens': it['tok'], 'carried': it['rem'], 'cost': round(it['cost'], 4)})
    kinds = collections.defaultdict(lambda: [0, 0, 0.0])
    for it in items:
        k = kinds[(it['kind'], it['tool'] if it['kind'] in ('cache 重建',) else '')]
        k[0] += 1
        k[1] += it['tokens']
        k[2] += it['cost']
    kind_rows = [{'kind': k, 'detail': t, 'count': v[0], 'tokens': v[1], 'cost': round(v[2], 2)}
                 for (k, t), v in sorted(kinds.items(), key=lambda kv: -kv[1][2])]
    items.sort(key=lambda r: -r['cost'])
    for i, it in enumerate(items):
        it['rank'] = i + 1

    # LLM verdicts from a previous judge run (judge.json), if any
    jpath = os.path.join(out_dir, 'judge.json')
    if os.path.exists(jpath):
        verdicts = {v['id']: v for v in json.load(open(jpath))}
        for r in sessions:
            v = verdicts.get(r['session'])
            if v:
                r['verdict'], r['reason'], r['fix'] = v.get('verdict', ''), v.get('reason', ''), v.get('fix', '')

    days_seen = sorted({r['day'] for r in daily_rows})
    meta = [{
        'start_day': days_seen[0] if days_seen else None, 'end_day': days_seen[-1] if days_seen else None,
        'days': len(days_seen), 'hosts': ', '.join(sorted({c['host'] for c in chains})),
        'sessions': len(sessions), 'chains': len(chains), 'calls': sum(c['n_req'] for c in chains),
        'check_n': len(ratios), 'check_median': round(med, 3) if med else None,
        'generated': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
    }]
    # ccusage crosscheck from crosscheck.py (crosscheck.json), if any; the dashboard shows the first host
    xpath = os.path.join(out_dir, 'crosscheck.json')
    if os.path.exists(xpath):
        for x in json.load(open(xpath))[:1]:
            meta[0].update({'xc_host': x['host'], 'xc_days': x['days'], 'xc_until': x['until'],
                            'xc_ours': x['ours'], 'xc_ccusage': x['ccusage'], 'xc_ratio': x['ratio']})

    judge_pool.sort(key=lambda x: -x[0])
    os.makedirs(out_dir, exist_ok=True)
    for name, rows in [('sessions', sessions), ('daily', daily_rows), ('waste', items[:400]),
                       ('waste_kinds', kind_rows), ('meta', meta),
                       ('judge_input', [x for _, x in judge_pool[:15]])]:
        json.dump(rows, open(os.path.join(out_dir, name + '.json'), 'w'), ensure_ascii=False, indent=0)
    print('sessions=%d chains=%d waste_items=%d flagged=%d total=$%.2f waste=$%.2f' % (
        len(sessions), len(chains), len(items), len(judge_pool), sum(r['cost'] for r in sessions),
        sum(r['cost'] for r in items)))
    if unknown_models:
        print('unpriced models:', dict(unknown_models))


if __name__ == '__main__':
    main()
