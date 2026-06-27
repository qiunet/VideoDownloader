# VideoDownload

基于 Python + tkinter + yt-dlp 的跨平台视频下载客户端。

## 功能

- 输入视频链接，一键下载
- 自定义保存目录
- 实时显示下载进度（标题、状态、进度、速度、剩余时间）
- 支持打包为 macOS `.app` / `.dmg` 和 Windows `.exe`

## 开发运行

```bash
cd VideoDownload
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python main.py
```

或直接运行：

```bash
./scripts/run.sh
```

## 使用说明

1. 在输入框粘贴视频链接
2. 点击「下载」，在弹出的对话框中选择保存目录
3. 下方列表实时显示下载进度

## 打包

### GitHub Actions（推荐，可同时打 Mac + Windows）

推送代码到 GitHub 后：

**手动触发：**
1. 打开仓库 → Actions → Build → Run workflow

**打 tag 自动发布：**
```bash
git tag v1.0.0
git push origin v1.0.0
```

会在 Actions 页面生成产物：
- `VideoDownload-macOS`：`VideoDownload.dmg` + `VideoDownload.app.zip`
- `VideoDownload-Windows`：`VideoDownload-Windows.zip`（解压后为 `VideoDownload` 文件夹，内含主程序与更新器）

推送 `v*` 标签时还会自动创建 GitHub Release 并上传安装包。

### 本地打包 - macOS (dmg)

```bash
chmod +x scripts/build_mac.sh
./scripts/build_mac.sh
```

产物位于 `dist/VideoDownload.app` 和 `dist/VideoDownload.dmg`。

打包时会自动下载 **ffmpeg** 并内置到应用中，用户无需单独安装即可合并高清视频。

### 本地打包 - Windows (exe)

需在 Windows 上执行：

```cmd
scripts\build_win.bat
```

产物位于 `dist\VideoDownload\` 和 `dist\VideoDownload-Windows.zip`。

解压 zip 后运行 `VideoDownload\VideoDownload.exe` 即可。

## 依赖

- Python 3.10+
- [yt-dlp](https://github.com/yt-dlp/yt-dlp)
- PyQt6
- **ffmpeg**（开发时可选；**打包时会自动内置**）

开发环境若未安装 ffmpeg，可手动安装：

```bash
brew install ffmpeg
```

或仅下载到项目目录（与打包脚本相同）：

```bash
python scripts/download_ffmpeg.py
```

未安装 ffmpeg 时，客户端仅显示单文件清晰度；打包后的 exe/dmg 已内置 ffmpeg，支持全部清晰度。
# VideoDownloader
