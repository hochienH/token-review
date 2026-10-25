#!/usr/bin/env bash
# 把這個 repo 裝成 ~/.claude/skills/token-review（覆寫已裝的同名檔案）。只裝在跑 review 的機器；其他機器由 ssh 串流 analyze.py，不用裝。
set -eu
cd "$(dirname "$0")"
rsync -a --exclude .git --exclude .gitignore --exclude __pycache__ --exclude install.sh --exclude README.md ./ ~/.claude/skills/token-review/
echo "installed to ~/.claude/skills/token-review"
