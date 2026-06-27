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
python scripts/download_ffmpeg.py

echo ==^> PyInstaller 打包
pyinstaller --clean --noconfirm Updater.spec
pyinstaller --clean --noconfirm VideoDownload.spec

if not exist "dist\VideoDownload.exe" (
  echo 打包失败: 未找到 dist\VideoDownload.exe
  exit /b 1
)

echo ==^> 组装发布目录
if exist "dist\VideoDownload" rmdir /s /q "dist\VideoDownload"
mkdir "dist\VideoDownload"
move /Y "dist\VideoDownload.exe" "dist\VideoDownload\"
move /Y "dist\VideoDownloadUpdater.exe" "dist\VideoDownload\"

echo ==^> 打包 zip
powershell -NoProfile -Command "Compress-Archive -Path 'dist/VideoDownload' -DestinationPath 'dist/VideoDownload-Windows.zip' -Force"

if exist "dist\VideoDownload-Windows.zip" (
  echo.
  echo 打包完成:
  echo   目录: dist\VideoDownload\
  echo   压缩包: dist\VideoDownload-Windows.zip
) else (
  echo 打包失败: 未找到 dist\VideoDownload-Windows.zip
  exit /b 1
)
