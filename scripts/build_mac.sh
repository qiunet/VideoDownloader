#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

pick_python() {
  for candidate in python3.13 /opt/homebrew/bin/python3 python3; do
    if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c "import sys; exit(0 if sys.version_info >= (3, 10) else 1)" 2>/dev/null; then
      echo "$candidate"
      return 0
    fi
  done
  echo "错误: 需要 Python 3.10 及以上版本。" >&2
  exit 1
}

PYTHON="$(pick_python)"
echo "==> 使用 Python: $PYTHON"

echo "==> 创建虚拟环境"
"$PYTHON" -m venv .venv
source .venv/bin/activate

echo "==> 安装依赖"
pip install -q -r requirements.txt

echo "==> 生成图标"
python scripts/prepare_icons.py

echo "==> 下载 ffmpeg（打包内置）"
python scripts/download_ffmpeg.py

echo "==> PyInstaller 打包"
pyinstaller --clean --noconfirm Updater.spec
pyinstaller --clean --noconfirm VideoDownload.spec
cp dist/VideoDownloadUpdater dist/VideoDownload.app/Contents/MacOS/

APP_PATH="dist/VideoDownload.app"
DMG_PATH="dist/VideoDownload.dmg"

if [[ -d "$APP_PATH" ]]; then
  echo "==> 创建 DMG"
  rm -f "$DMG_PATH"
  hdiutil create -volname "VideoDownload" -srcfolder "$APP_PATH" -ov -format UDZO "$DMG_PATH"
  (cd dist && ditto -c -k --sequesterRsrc --keepParent VideoDownload.app VideoDownload.app.zip)
  echo ""
  echo "打包完成:"
  echo "  App: $ROOT/$APP_PATH"
  echo "  DMG: $ROOT/$DMG_PATH"
  echo "  Zip: $ROOT/dist/VideoDownload.app.zip"
else
  echo "打包失败: 未找到 $APP_PATH"
  exit 1
fi
