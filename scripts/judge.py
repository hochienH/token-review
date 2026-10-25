#!/usr/bin/env python3
"""token-review / judge.py

LLM verdicts for the sessions in OUT_DIR/judge_input.json, through the
Messages API. Promotional credit covers the API but not `claude -p`, so this
must not go through Claude Code. Needs the anthropic SDK:

  ~/.cache/token-review/venv/bin/python judge.py OUT_DIR

Every session is judged REPEATS times; each round shuffles the sessions and
splits them into batches of BATCH, so a session never sits with the same
neighbours twice. The final verdict is the median vote (the majority with
three votes; 部分值得 when all three differ), since one run flips sessions near
a boundary.

Writes OUT_DIR/judge.json (verdicts and votes; build.py merges it into
sessions.json) and OUT_DIR/judge_usage.json (tokens, cost, request ids).
"""
import concurrent.futures, json, os, random, sys

import anthropic

MODEL = 'claude-opus-5-5'
PRICE = {'claude-opus-5-5': (4.0, 20.0)}     # $/MTok in/out, claude-api skill model table
ORDER = ['不值得', '部分值得', '值得']          # ordinal, for the median
REPEATS, BATCH, WORKERS = 3, 15, 8


def ask(client, prompt, batch):
    resp = client.beta.messages.create(
        model=MODEL,
        max_tokens=16000,
        betas=['server-side-fallback-2026-07-01'],
        fallbacks='default',
        thinking={'type': 'adaptive'},
        output_config={'effort': 'high'},
        system=prompt,
        messages=[{'role': 'user', 'content': json.dumps(batch, ensure_ascii=False)}],
    )
    u = resp.usage
    pin, pout = PRICE.get(resp.model, (0, 0))
    usage = {'model': resp.model, 'request_id': resp._request_id, 'stop_reason': resp.stop_reason,
             'n': len(batch), 'input_tokens': u.input_tokens, 'output_tokens': u.output_tokens,
             'cost_usd': round((u.input_tokens * pin + u.output_tokens * pout) / 1e6, 4)}
    if resp.stop_reason != 'end_turn':
        raise RuntimeError('stopped: %s' % resp.stop_reason)
    text = ''.join(b.text for b in resp.content if b.type == 'text')
    try:
        rows = json.loads(text[text.index('['):text.rindex(']') + 1])
    except ValueError:
        raise RuntimeError('reply is not a JSON array: %s' % text[:200])
    ids = {d['id'] for d in batch}
    return usage, [r for r in rows if isinstance(r, dict) and r.get('id') in ids and r.get('verdict') in ORDER]


def main():
    out = sys.argv[1]
    skill = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    prompt = open(os.path.join(skill, 'judge_prompt.md'), encoding='utf-8').read()
    digests = json.load(open(os.path.join(out, 'judge_input.json'), encoding='utf-8'))
    key = open(os.path.expanduser('~/.secrets/anthropic_key')).read().strip()
    client = anthropic.Anthropic(api_key=key)

    tasks = []
    for rep in range(REPEATS):
        order = digests[:]
        random.Random(rep).shuffle(order)
        tasks += [(rep, order[i:i + BATCH]) for i in range(0, len(order), BATCH)]

    calls, votes, failed = [], {}, []
    with concurrent.futures.ThreadPoolExecutor(WORKERS) as ex:
        futs = {ex.submit(ask, client, prompt, b): rep for rep, b in tasks}
        for f in concurrent.futures.as_completed(futs):
            try:
                usage, rows = f.result()
            except (anthropic.APIStatusError, anthropic.APIConnectionError, RuntimeError) as e:
                failed.append('round %d: %s' % (futs[f], e))
                continue
            calls.append(dict(usage, round=futs[f]))
            for r in rows:
                votes.setdefault(r['id'], []).append(r)

    total = {'calls': len(calls), 'failed': failed,
             'input_tokens': sum(c['input_tokens'] for c in calls),
             'output_tokens': sum(c['output_tokens'] for c in calls),
             'cost_usd': round(sum(c['cost_usd'] for c in calls), 4), 'requests': calls}
    json.dump(total, open(os.path.join(out, 'judge_usage.json'), 'w'), ensure_ascii=False, indent=1)
    print('calls=%d failed=%d cost=$%.3f' % (len(calls), len(failed), total['cost_usd']))
    if failed:
        sys.exit('\n'.join(failed))

    good = []
    for d in digests:
        vs = votes.get(d['id'], [])
        if not vs:
            continue
        mid = sorted((v['verdict'] for v in vs), key=ORDER.index)[(len(vs) - 1) // 2]
        pick = next(v for v in vs if v['verdict'] == mid)
        good.append({'id': d['id'], 'verdict': mid, 'votes': [v['verdict'] for v in vs],
                     'reason': pick.get('reason', ''), 'fix': pick.get('fix', '')})
    json.dump(good, open(os.path.join(out, 'judge.json'), 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    short = sum(len(v['votes']) < REPEATS for v in good)
    print('verdicts: %d of %d sessions, %d with fewer than %d votes' % (len(good), len(digests), short, REPEATS))


if __name__ == '__main__':
    main()
