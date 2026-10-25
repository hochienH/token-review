#!/usr/bin/env python3
"""token-review / analyze.py

Parse one host's Claude Code transcripts (~/.claude/projects/**/*.jsonl) into
token-level aggregates. Stdlib only, Python 3.6+, so it can be streamed to a
remote host:  ssh HOST 'python3 -I - --host lab' < analyze.py > lab.json

Output (stdout, JSON): {"host", "generated", "chains": [...]}. One chain is
one transcript file (a main session or a subagent). Costs are NOT computed
here; build.py applies prices so every host is priced the same way.
"""
import argparse, calendar, collections, glob, json, os, re, sys, time

# Attachments injected into context; counted when splitting context growth between items.
INJECTED_ATT = {'skill_listing', 'instructions', 'mcp_instructions_delta', 'deferred_tools_delta',
                'deferred_tools_record', 'environment', 'session_context', 'agent_listing_delta'}
EDIT_TOOLS = {'Edit', 'Write', 'NotebookEdit', 'MultiEdit'}
CORRECTION = re.compile(r'不對|不是這樣|錯了|你沒有?|我說|我是說|重來|重做|別再|不要再|又錯|搞錯|看清楚|'
                        r'wrong|that\'s not|not what i', re.I)
SECRET = re.compile(r'sk-ant-[\w-]+|gh[pousr]_\w{20,}|(?i:(?:api[_-]?key|token|password|secret)\s*[=:]\s*)\S+'
                    r'|[A-Za-z0-9+_-]{40,}')
HOME = re.compile(r'(?:/nfs/nas)?/(?:Users|home/nas|home)/[^/\s]+')
BIG_RESULT = 5000       # tokens; a tool result at least this big is reported individually
REBUILD_MIN = 20000     # tokens; a cache write at least this big that rewrites most of the context
# message.diagnostics.cache_miss_reason.type, as recorded by Claude Code
MISS_LABEL = {'previous_message_not_found': 'cache 已失效（找不到上一則）', 'model_changed': '換 model',
              'system_changed': 'system prompt 變動', 'messages_changed': '對話內容被改動',
              'tools_changed': 'tool 定義變動'}
RESET_RATIO = 0.6       # context shrinking below this ratio = compaction / clear
JST = 9 * 3600


def clean(s, n):
    s = HOME.sub('~', SECRET.sub('[REDACTED]', str(s)))
    return ' '.join(s.split())[:n]


def epoch(ts):
    try:
        return calendar.timegm(time.strptime(ts[:19], '%Y-%m-%dT%H:%M:%S'))
    except Exception:
        return None


def day(e):
    return time.strftime('%Y-%m-%d', time.gmtime(e + JST)) if e else None


def size(content):
    """Characters a tool_result / message body puts into context (images ~3000)."""
    if content is None:
        return 0
    if isinstance(content, str):
        return len(content)
    n = 0
    for b in content if isinstance(content, list) else [content]:
        if isinstance(b, dict):
            if b.get('type') == 'text':
                n += len(b.get('text') or '')
            elif b.get('type') == 'image':
                n += 3000
            else:
                n += len(json.dumps(b, ensure_ascii=False))
        else:
            n += len(str(b))
    return n


def text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return '\n'.join(b.get('text') or '' for b in content if isinstance(b, dict) and b.get('type') == 'text')
    return ''


def describe(name, inp):
    """Short, redacted description of a tool call."""
    if not isinstance(inp, dict):
        return clean(inp, 100)
    if name == 'Read':
        s = inp.get('file_path', '')
        if inp.get('offset') or inp.get('limit'):
            s += ' [%s:%s]' % (inp.get('offset', ''), inp.get('limit', ''))
        return clean(s, 120)
    for k in ('command', 'file_path', 'url', 'query', 'pattern', 'description', 'prompt', 'skill'):
        if inp.get(k):
            return clean(inp[k], 120)
    return clean(json.dumps(inp, ensure_ascii=False), 100)


def parse_chain(path, root):
    rel = os.path.relpath(path, root)
    parts = rel.split(os.sep)
    # <proj>/<sid>.jsonl is a main session; <proj>/<sid>/<kind>/...jsonl is a child chain
    parent = parts[1] if len(parts) > 2 else None
    kind = parts[2] if len(parts) > 3 else 'main'

    reqs, by_rid, items, pending = [], {}, [], []
    tool_uses = {}
    meta = {'cwd': None, 'entrypoint': None, 'title': None, 'session': None, 'first_prompt': None,
            'cost_state': None, 'last_text': ''}
    user_texts = []
    neg = collections.Counter()

    with open(path, errors='replace') as fh:
        for line in fh:
            try:
                d = json.loads(line)
            except Exception:
                continue
            if not isinstance(d, dict):
                continue
            t = d.get('type')
            for k in ('cwd', 'entrypoint'):
                if meta[k] is None and d.get(k):
                    meta[k] = d[k]
            if meta['session'] is None and d.get('sessionId'):
                meta['session'] = d['sessionId']

            if t == 'assistant':
                m = d.get('message') or {}
                if m.get('model') in (None, '<synthetic>'):
                    continue
                rid = d.get('requestId') or m.get('id') or d.get('uuid')
                if rid not in by_rid:
                    by_rid[rid] = len(reqs)
                    for i in pending:
                        items[i]['at'] = len(reqs)
                    pending = []
                    reqs.append({'ts': epoch(d.get('timestamp', '')), 'model': m.get('model'), 'usage': {}})
                r = reqs[by_rid[rid]]
                miss = ((m.get('diagnostics') or {}).get('cache_miss_reason') or {}).get('type')
                if miss and miss != 'unavailable':
                    r['miss'] = miss
                u = m.get('usage') or {}
                if (u.get('output_tokens') or 0) >= (r['usage'].get('output_tokens') or -1):
                    r['usage'] = u
                for b in m.get('content') or []:
                    if not isinstance(b, dict):
                        continue
                    if b.get('type') == 'tool_use':
                        tool_uses[b.get('id')] = (b.get('name'), b.get('input') or {})
                    elif b.get('type') == 'text' and b.get('text'):
                        meta['last_text'] = b['text']

            elif t == 'user':
                if d.get('toolDenialKind'):
                    neg['denials'] += 1
                c = (d.get('message') or {}).get('content')
                blocks = c if isinstance(c, list) else [{'type': 'text', 'text': c}] if isinstance(c, str) else []
                for b in blocks:
                    if not isinstance(b, dict):
                        continue
                    if b.get('type') == 'tool_result':
                        items.append({'k': 'tool', 'tuid': b.get('tool_use_id'), 'chars': size(b.get('content')),
                                      'err': bool(b.get('is_error')), 'ts': d.get('timestamp')})
                        pending.append(len(items) - 1)
                    elif b.get('type') == 'text':
                        txt = b.get('text') or ''
                        items.append({'k': 'meta' if d.get('isMeta') else 'user', 'chars': len(txt)})
                        pending.append(len(items) - 1)
                        if d.get('isMeta') or not txt.strip():
                            continue
                        if txt.startswith('[Request interrupted'):
                            neg['interrupts'] += 1
                            continue
                        if txt.lstrip().startswith('<') and 'command' not in txt[:40]:
                            continue        # injected system/hook text, not typed by the user
                        neg['corrections'] += len(CORRECTION.findall(txt[:2000]))
                        if meta['first_prompt'] is None:
                            meta['first_prompt'] = clean(re.sub(r'<[^>]+>', ' ', txt), 300)
                        if len(user_texts) < 15:
                            user_texts.append(clean(txt, 160))

            elif t == 'attachment':
                a = d.get('attachment') or {}
                at = a.get('type') if isinstance(a, dict) else None
                if at in INJECTED_ATT:
                    items.append({'k': 'att', 'chars': len(json.dumps(a, ensure_ascii=False))})
                    pending.append(len(items) - 1)

            elif t == 'cost-state':
                meta['cost_state'] = {'usd': d.get('totalCostUSD'), 'lines_added': d.get('totalLinesAdded')}

            elif t == 'ai-title' and d.get('aiTitle'):
                meta['title'] = d['aiTitle']

    if not reqs:
        return None

    # ---- per-request token numbers -------------------------------------------------
    for r in reqs:
        u = r.pop('usage')
        cc = u.get('cache_creation') or {}
        cw = u.get('cache_creation_input_tokens') or 0
        r['in'] = u.get('input_tokens') or 0
        r['cw1h'] = min(cc.get('ephemeral_1h_input_tokens') or 0, cw)
        r['cw5'] = cw - r['cw1h']
        r['cr'] = u.get('cache_read_input_tokens') or 0
        r['out'] = u.get('output_tokens') or 0
        r['think'] = (u.get('output_tokens_details') or {}).get('thinking_tokens') or 0
        r['ctx'] = r['in'] + cw + r['cr']

    # segments: a context drop (compaction / clear) ends what earlier content can be carried into
    n = len(reqs)
    reset = [i > 0 and reqs[i]['ctx'] < RESET_RATIO * reqs[i - 1]['ctx'] for i in range(n)]
    seg_end, end = [0] * n, n - 1
    for i in range(n - 1, -1, -1):
        seg_end[i] = end
        if reset[i]:
            end = i - 1

    def growth(j):
        if j == 0 or reset[j]:
            return None
        return reqs[j]['ctx'] - reqs[j - 1]['ctx'] - reqs[j - 1]['out']

    # token estimate per inserted item: split the measured context growth by character share
    by_at = collections.defaultdict(list)
    for it in items:
        if 'at' in it:
            by_at[it['at']].append(it)
    for j, its in by_at.items():
        g, chars = growth(j), sum(it['chars'] for it in its)
        for it in its:
            it['tok'] = int(g * it['chars'] / chars) if g and g > 0 and chars else int(it['chars'] / 3)
            it['rem'] = seg_end[j] - j

    # ---- waste items ----------------------------------------------------------------
    out_items = []
    main_model = collections.Counter(r['model'] for r in reqs).most_common(1)[0][0]

    def emit(k, it, tool, detail, extra=None):
        j = it['at']
        row = {'k': k, 'tok': it['tok'], 'rem': it['rem'], 'w1h': reqs[j]['cw1h'] > 0,
               'model': reqs[j]['model'], 'tool': tool, 'detail': detail, 'ts': it.get('ts')}
        row.update(extra or {})
        out_items.append(row)

    seen_reads, last_err = {}, None
    tool_counts = collections.Counter()
    outcome = collections.Counter()
    for it in items:
        if it['k'] != 'tool' or 'at' not in it:
            continue
        name, inp = tool_uses.get(it['tuid'], (None, {}))
        if name is None:
            continue
        tool_counts[name] += 1
        if not isinstance(inp, dict):
            inp = {}
        if name in EDIT_TOOLS and not it['err']:
            outcome['edits'] += 1
            p = inp.get('file_path') or inp.get('notebook_path')
            for key in [k for k in seen_reads if k[0] == p]:
                del seen_reads[key]
        if name == 'Bash' and 'git commit' in str(inp.get('command', '')) and not it['err']:
            outcome['commits'] += 1
        if name == 'Artifact' and inp.get('action', 'publish') == 'publish' and not it['err']:
            outcome['artifacts'] += 1
        if it['err']:
            neg['errors'] += 1
        sig = (name, json.dumps(inp, sort_keys=True, ensure_ascii=False))
        if last_err is not None and sig == last_err:
            neg['retries'] += 1
            emit('retry', it, name, describe(name, inp))
        last_err = sig if it['err'] else None
        if it['tok'] >= BIG_RESULT:
            emit('big', it, name, describe(name, inp))
        if name == 'Read' and not it['err']:
            key = (inp.get('file_path'), inp.get('offset'), inp.get('limit'))
            if key in seen_reads:
                emit('reread', it, name, describe(name, inp))
            seen_reads[key] = True

    for i in range(1, n):
        r, p = reqs[i], reqs[i - 1]
        cw = r['cw5'] + r['cw1h']
        if cw < REBUILD_MIN or cw < 0.5 * r['ctx']:
            continue
        g = growth(i)
        extra = cw - min(max(g or 0, 0), cw)
        if extra < REBUILD_MIN / 2:
            continue
        gap = (r['ts'] - p['ts']) if r['ts'] and p['ts'] else 0
        if r.get('miss'):                    # Claude Code recorded why the cache missed
            cause = MISS_LABEL.get(r['miss'], r['miss'])
        elif gap > 3600:                     # older transcripts: infer from the gap
            cause = '閒置超過 1 小時（推測）'
        elif r['model'] != p['model']:
            cause = '換 model（推測）'
        elif reset[i]:
            cause = 'compaction 後重建（推測）'
        elif gap > 300 and r['cw1h'] == 0:
            cause = '閒置超過 5 分鐘（推測）'
        else:
            cause = 'prefix 變動（推測）'
        out_items.append({'k': 'rebuild', 'tok': extra, 'rem': 0, 'w1h': r['cw1h'] > 0, 'model': r['model'],
                          'tool': None, 'detail': '%s（間隔 %d 分鐘）' % (cause, gap // 60), 'cause': cause,
                          'ts': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(r['ts'])) if r['ts'] else None})

    # ---- aggregates -----------------------------------------------------------------
    tok, days = {}, {}
    for r in reqs:
        v = [r['in'], r['cw5'], r['cw1h'], r['cr'], r['out'], r['think']]
        a = tok.setdefault(r['model'], [0] * 6)
        b = days.setdefault('%s|%s' % (day(r['ts']), r['model']), [0] * 6)
        for k in range(6):
            a[k] += v[k]
            b[k] += v[k]

    ts = [r['ts'] for r in reqs if r['ts']]
    return {
        'file': rel, 'session': meta['session'] or os.path.basename(path)[:-6], 'parent': parent, 'kind': kind,
        'cwd': clean(meta['cwd'] or '', 200), 'entrypoint': meta['entrypoint'], 'title': clean(meta['title'] or '', 80),
        'first_prompt': meta['first_prompt'], 'model': main_model, 'n_req': n, 'resets': sum(reset),
        'start': min(ts) if ts else None, 'end': max(ts) if ts else None,
        'tok': tok, 'days': days, 'outcome': dict(outcome), 'neg': dict(neg), 'tool_counts': dict(tool_counts),
        'cost_state': meta['cost_state'], 'user_texts': user_texts,
        'last_text': clean(meta['last_text'], 300), 'last_text_chars': len(meta['last_text']),
        'items': out_items,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--root', default=os.path.expanduser('~/.claude/projects'))
    ap.add_argument('--host', required=True)
    a = ap.parse_args()
    chains, bad = [], 0
    for f in sorted(glob.glob(os.path.join(a.root, '**', '*.jsonl'), recursive=True)):
        try:
            c = parse_chain(f, a.root)
        except Exception as e:      # one corrupt file must not kill the whole host
            bad += 1
            sys.stderr.write('skip %s: %r\n' % (f, e))
            continue
        if c:
            c['host'] = a.host
            chains.append(c)
    json.dump({'host': a.host, 'generated': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
               'skipped': bad, 'chains': chains}, sys.stdout, ensure_ascii=False)


if __name__ == '__main__':
    main()
