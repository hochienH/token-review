---
name: token-review
description: 找出 Claude Code 的 token 浪費：從三台機器（MacBook Air、MacBook Pro、實驗室 NAS）的 transcript 算出可疑浪費（cache 重建、大型 tool 輸出的累積成本、重複讀檔、失敗重試）與最不值得的 session，跟 ccusage 交叉核對後更新 Token Review dashboard。使用者說 token review、哪裡浪費 token、更新 token dashboard 時使用。成本總表交給 ccusage，固定開銷交給 /context。
---

# token-review

在 MacBook Air 上跑。先讀同目錄的 `NOTES.md`（決策與踩過的坑）和 `NOTES.local.md`（dashboard URL、run 位置、待辦；不進 git）。

## 步驟

1. **開 run 資料夾**（一次一個，不覆寫）：
   `R=~/.cache/token-review/runs/$(date +%y%m%d-%H%M)_review; mkdir -p $R`
2. **三台各自解析，只傳回彙總**（transcript 不離開原機器）。`A=~/.claude/skills/token-review/scripts/analyze.py`；pro 與 lab 的 ssh alias 在同目錄的 `hosts.env`（不進 git，格式見 `hosts.env.example`），ssh 那兩行先 `. ~/.claude/skills/token-review/hosts.env`。三行平行跑：
   - `python3 -I $A --host air > $R/air.json`
   - `ssh -o BatchMode=yes "$PRO_HOST" 'python3 -I - --host pro' < $A > $R/pro.json`
   - `ssh -o BatchMode=yes "$LAB_HOST" 'python3 -I - --host lab' < $A > $R/lab.json`（lab 的 home 在 NAS，8 台共用，進一台就夠）
3. **套價、標記、產生資料集**：`python3 -I ~/.claude/skills/token-review/scripts/build.py $R/out $R/air.json $R/pro.json $R/lab.json`
   - 看輸出的 `cost check vs cost-state` 中位數，應接近 1.000；偏離超過 5% 就先查定價表（`build.py` 的 `PRICES`）再往下。
   - 出現 `unpriced models` 時，從 claude-api skill 的 model 表補價格，不要憑記憶。
3b. **跟 ccusage 交叉核對**（只有 Air 有 node）：
   `npx -y ccusage@20.0.42 daily --json --since 20260920 --timezone Asia/Tokyo > $R/ccusage_air.json && python3 -I ~/.claude/skills/token-review/scripts/crosscheck.py $R/out air $R/ccusage_air.json`
   - 比值應接近 1.000（第一次 1.0001／1.000）；偏離超過 1% 就先查哪一天差最多（`worst_day`），再往下。
4. **LLM 判讀（直接打 Messages API，扣 promotional credit；第一次 run 約 US$0.16）**：
   `~/.cache/token-review/venv/bin/python ~/.claude/skills/token-review/scripts/judge.py $R/out`
   - 寫出 `$R/out/judge.json` 與 `judge_usage.json`，再跑一次步驟 3，判讀與 ccusage 核對結果就會併進 sessions 與 meta。
   - **不要用 `claude -p`**：promotional credit 不涵蓋 Claude Code 用量，帶 key 也會回 `Credit balance is too low`。
   - venv 不見了就重建：`python3 -m venv ~/.cache/token-review/venv && ~/.cache/token-review/venv/bin/pip install anthropic`。
   - 失敗就停下來回報，不要換別的方式重試。
5. **更新 dashboard**（URL 見 `NOTES.local.md`）：`sessions`、`waste`、`waste_kinds`、`meta` 四個 `$R/out/*.json` 各自用 Artifact `asset: true` 上傳，再用 ArtifactData 對 `datasets/<id>` 做 `update`，改 `source.url` 和 `updated`（要帶 `if_version`）。`daily.json` 只給 crosscheck 用，`judge_input.json` 含對話節錄，兩者都只留本機。頁面 `files/index.html` 不用動，原始頁面在 `dashboard/index.html`。
6. 回報：總花費、可疑浪費佔比、前三大浪費來源與對應的具體建議、dashboard 連結。
