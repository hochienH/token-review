# token-review

Claude Code skill：從多台機器的 transcript 找出 token 浪費（cache 重建、大型 tool 輸出之後被 cache read 重付的累積成本、重複讀檔、失敗重試），用 LLM 判讀每個 session 值不值得，再跟 [ccusage](https://github.com/ryoppippi/ccusage) 與 Claude Code 自己的 cost-state 對帳。成本總表交給 ccusage，固定開銷交給 `/context`，這裡只做它們沒有的部分。

## 安裝

```bash
cp hosts.env.example hosts.env   # 填其他機器的 ssh alias
./install.sh                     # rsync 到 ~/.claude/skills/token-review
```

`analyze.py`、`build.py`、`crosscheck.py` 只用 Python stdlib，所以能經 ssh 串流到遠端執行，原始 transcript 不跨機器。`judge.py` 需要 `anthropic` SDK 與 API key，會把遮蔽過路徑與金鑰的 session 節錄送到 Messages API。`crosscheck.py` 需要 node（`npx ccusage`）。

流程見 `SKILL.md`，設計決策與踩過的坑見 `NOTES.md`。
