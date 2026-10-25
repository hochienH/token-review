# token-review 筆記

主機 alias、dashboard URL、run 位置、對帳數字與待辦在 `NOTES.local.md`（不進 git）。

## 決策
- 在各機器上跑 analyze.py，只傳回彙總 JSON；transcript 不搬（lab 有 1.6G，而且含敏感內容）。
- 成本用 usage × 官方單價（claude-api skill model 表）。cache write 用 1.25×／2× input。對帳 Claude Code 的 cost-state.totalCostUSD：142 個 session 中位數 1.000。
- 同一個 API 回應會拆成多行 assistant（同 requestId），要用 requestId 去重，否則重複計算。
- subagent 在 `<proj>/<sid>/subagents/agent-*.jsonl`，算進父 session。
- 每筆內容的 tokens：兩次呼叫的 context 差（扣上次 output），依字元數比例分給中間插入的東西。
- 可疑浪費是上限：大型輸出 = tokens ×（寫入 + 之後每次讀取）；cache 重建 = 重寫 tokens ×（寫入價 − 讀取價）。
- 2026-10-24 縮減成核心（照現成方案調查）：拿掉固定開銷估算（改用 `/context`）與「花在哪」的總表、每日、token 種類、專案圖（改用 ccusage）。保留累積成本、cache 重建、重複讀檔、失敗重試、session 判讀、兩種對帳。
- cache 重建原因優先讀 `message.diagnostics.cache_miss_reason.type`，沒有才用間隔／model 推測並標「推測」。
- 跟 ccusage 20.0.42 交叉核對（crosscheck.py，只有 Air 有 node）。
- judge 用 anthropic SDK 直接打 Messages API（`scripts/judge.py`，venv 在 ~/.cache/token-review/venv），Opus 5.5、effort high、server-side fallback；一次呼叫判 15 個 session。

## 現成方案（2026-10-24 調查，star 數是當天 GitHub API）
- 成本按日／session／model：ccusage（19.1k，MIT，1h/5m 分開計價）、CodeBurn（11.6k，MIT，有 web UI，自承 subagent 可能漏算）。這塊不值得自己維護，拿 ccusage 交叉核對。
- 多機：agentsview（6.2k，MIT）v0.46.0（2026-10-23）支援 SSH 同步，剛加、上一版移除過，觀望。
- 官方：`/usage`（main 的 cache miss 與可能原因）、`/context`（固定開銷精確值，只限當前 session）、`/insights`（LLM friction 報告）、OTel（tool_result_size_bytes，沒有累積成本）。
- cache 重建原因：transcript 有 `message.diagnostics.cache_miss_reason.type`（本機 45 筆、lab 199 筆，大多 `previous_message_not_found`），比用時間差推測可靠；cachemiss（0 star）就是讀這個。
- token-optimizer（2.7k）是 PolyForm Noncommercial，事務所的工作不能用；會裝 hooks、改 statusline。
- 沒有現成對應、值得保留的：單一 tool result 的累積成本（carried cost）、對 cost-state 的對帳驗證、三台彙總。

## 踩過的坑
- 2026-10-23：auto mode 擋下改 ~/.claude/settings.json（Self-Modification），cleanupPeriodDays 要使用者自己改：`scripts/set_retention.py`。
- 2026-10-23：`claude -p --bare` 帶 key 回 `Credit balance is too low`。原因不是沒 credit：promotional credit 只涵蓋 Agent SDK、API、Batch API、Playground、Managed Agents，不含 Claude Code。同一把 key 用 curl 打 API 成功。
- Opus 5.5 的 cache read 是 input 的 1/20（$0.20 vs $4），Fable 5.1 是 1/40；不是常見的 1/10。
- transcript 預設只留 30 天；第一次 run 最早只到 2026-09-20。
