@echo off
setlocal enabledelayedexpansion

cd /d "%~dp0\.."

echo ==^> 创建虚拟环境
python -m venv .venv
call .venv\Scripts\activate.bat

echo ==^> 安装依赖
pip install -q -r requirements.txt

echo ==^> 生成图标
python scripts\prepare_icons.py

echo ==^> 下载 ffmpeg（打包内置）
python scripts\download_ffmpeg.py

echo ==^> PyInstaller 打包
pyinstaller --clean --noconfirm VideoDownload.spec

if exist "dist\VideoDownload.exe" (
  echo.
  echo 打包完成: dist\VideoDownload.exe
) else (
  echo 打包失败: 未找到 dist\VideoDownload.exe
  exit /b 1
)
