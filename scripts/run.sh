#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

pick_python() {
  for candidate in python3.13 /opt/homebrew/bin/python3 python3 /usr/bin/python3; do
    if command -v "$candidate" >/dev/null 2>&1; then
      ver=$("$candidate" -c "import sys; print(sys.version_info[:2])" 2>/dev/null) || continue
      if "$candidate" -c "import sys; exit(0 if sys.version_info >= (3, 10) else 1)" 2>/dev/null; then
        echo "$candidate"
        return 0
      fi
    fi
  done
  echo "错误: 需要 Python 3.10 及以上版本。" >&2
  exit 1
}

if [[ ! -d .venv ]]; then
  PYTHON="$(pick_python)"
  echo "创建虚拟环境: $PYTHON"
  "$PYTHON" -m venv .venv
  source .venv/bin/activate
  pip install -q -r requirements.txt
else
  source .venv/bin/activate
fi

python main.py
