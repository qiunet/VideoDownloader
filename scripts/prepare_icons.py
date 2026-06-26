#!/usr/bin/env python3
"""生成多平台应用图标 (ico / icns)。"""

import platform
from pathlib import Path

from PIL import Image

ASSETS = Path(__file__).resolve().parent.parent / "assets"
PNG = ASSETS / "icon.png"


def main() -> None:
    ASSETS.mkdir(exist_ok=True)
    if not PNG.exists():
        raise SystemExit(f"缺少图标源文件: {PNG}")

    img = Image.open(PNG).convert("RGBA")
    sizes = [(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    img.save(ASSETS / "icon.ico", format="ICO", sizes=sizes)
    print(f"已生成 {ASSETS / 'icon.ico'}")

    if platform.system() != "Darwin":
        return

    # macOS icns 通过 iconset 目录生成（需符合 Apple 命名规范）
    iconset = ASSETS / "icon.iconset"
    if iconset.exists():
        import shutil
        shutil.rmtree(iconset)
    iconset.mkdir(exist_ok=True)

    iconset_map = {
        "icon_16x16.png": 16,
        "icon_16x16@2x.png": 32,
        "icon_32x32.png": 32,
        "icon_32x32@2x.png": 64,
        "icon_128x128.png": 128,
        "icon_128x128@2x.png": 256,
        "icon_256x256.png": 256,
        "icon_256x256@2x.png": 512,
        "icon_512x512.png": 512,
        "icon_512x512@2x.png": 1024,
    }
    for name, size in iconset_map.items():
        resized = img.resize((size, size), Image.Resampling.LANCZOS)
        resized.save(iconset / name)

    import subprocess

    subprocess.run(
        ["iconutil", "-c", "icns", str(iconset), "-o", str(ASSETS / "icon.icns")],
        check=True,
    )
    print(f"已生成 {ASSETS / 'icon.icns'}")

    # 清理 iconset
    for f in iconset.iterdir():
        f.unlink()
    iconset.rmdir()


if __name__ == "__main__":
    main()
