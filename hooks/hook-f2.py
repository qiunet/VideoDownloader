"""PyInstaller hook：打包 f2 的配置、语言包等非 Python 资源。"""

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

datas = collect_data_files("f2", include_py_files=False)
hiddenimports = collect_submodules("f2")
