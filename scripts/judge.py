#!/usr/bin/env python3
"""token-review / judge.py

LLM verdicts for the flagged sessions in OUT_DIR/judge_input.json, through the
Messages API. Promotional credit covers the API but not `claude -p`, so this
must not go through Claude Code. Needs the anthropic SDK:

  ~/.cache/token-review/venv/bin/python judge.py OUT_DIR

Writes OUT_DIR/judge.json (verdicts; build.py merges it into sessions.json)
and OUT_DIR/judge_usage.json (tokens, cost, request id).
"""
import json, os, sys

import anthropic

MODEL = 'claude-opus-5-5'
PRICE = {'claude-opus-5-5': (4.0, 20.0)}     # $/MTok in/out, claude-api skill model table
VERDICTS = {'值得', '部分值得', '不值得'}


def main():
    out = sys.argv[1]
    skill = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    prompt = open(os.path.join(skill, 'judge_prompt.md'), encoding='utf-8').read()
    digests = json.load(open(os.path.join(out, 'judge_input.json'), encoding='utf-8'))
    key = open(os.path.expanduser('~/.secrets/anthropic_key')).read().strip()

    client = anthropic.Anthropic(api_key=key)
    try:
        resp = client.beta.messages.create(
            model=MODEL,
            max_tokens=16000,
            betas=['server-side-fallback-2026-07-01'],
            fallbacks='default',
            thinking={'type': 'adaptive'},
            output_config={'effort': 'high'},
            system=prompt,
            messages=[{'role': 'user', 'content': json.dumps(digests, ensure_ascii=False)}],
        )
    except anthropic.APIStatusError as e:
        sys.exit('API error %s: %s' % (e.status_code, e.message))
    except anthropic.APIConnectionError:
        sys.exit('network error')

    u = resp.usage
    pin, pout = PRICE.get(resp.model, (0, 0))
    usage = {'model': resp.model, 'request_id': resp._request_id, 'stop_reason': resp.stop_reason,
             'input_tokens': u.input_tokens, 'output_tokens': u.output_tokens,
             'cost_usd': round((u.input_tokens * pin + u.output_tokens * pout) / 1e6, 4)}
    json.dump(usage, open(os.path.join(out, 'judge_usage.json'), 'w'), ensure_ascii=False, indent=1)
    print(json.dumps(usage, ensure_ascii=False))
    if resp.stop_reason != 'end_turn':
        sys.exit('stopped: %s' % resp.stop_reason)

    text = ''.join(b.text for b in resp.content if b.type == 'text')
    try:
        rows = json.loads(text[text.index('['):text.rindex(']') + 1])
    except ValueError:
        open(os.path.join(out, 'judge_raw.txt'), 'w', encoding='utf-8').write(text)
        sys.exit('reply is not a JSON array; saved judge_raw.txt')

    ids = {d['id'] for d in digests}
    good = [r for r in rows if isinstance(r, dict) and r.get('id') in ids and r.get('verdict') in VERDICTS]
    json.dump(good, open(os.path.join(out, 'judge.json'), 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    print('verdicts: %d of %d sessions' % (len(good), len(ids)))


if __name__ == '__main__':
    main()
