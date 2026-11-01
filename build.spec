# -*- mode: python ; coding: utf-8 -*-

"""
PyInstaller打包配置
生成单文件exe，包含所有依赖
"""

import os

block_cipher = None

# 数据文件
# PyInstaller 执行 spec 时 __file__ 不可用，改用其内置全局变量 SPECPATH（spec 所在目录）
project_root = os.path.abspath(SPECPATH)
assets_dir = os.path.join(project_root, 'assets')
datas = [
    (os.path.join(project_root, 'config', '*.yaml'), 'config'),
    (os.path.join(project_root, 'web', 'frontend', 'templates'), 'web/frontend/templates'),
    (os.path.join(project_root, 'web', 'frontend', 'static'), 'web/frontend/static'),
    (assets_dir, 'assets'),
]

# 隐藏导入
hiddenimports = [
    'web.main',
    'PySide6.QtCore',
    'PySide6.QtGui',
    'PySide6.QtWidgets',
    'PySide6.QtWebEngineCore',
    'PySide6.QtWebEngineWidgets',
    'PySide6.QtPrintSupport',
    # app.py 用 QQuickWindow.setGraphicsApi 把 WebEngine 合成改为 OpenGL（见 desktop/webengine_profile.py）。
    'PySide6.QtQuick',
    'sqlalchemy.ext.declarative',
    'sqlalchemy.sql.default_comparator',
    'aiohttp',
    'fastapi',
    'uvicorn',
    'jinja2',
    'markdown2',
    'pdfkit',
    'qrcode',
    'PIL',
    'numpy',
    'pandas',
    'jieba',
    'wordcloud',
    'matplotlib',
]

a = Analysis(
    ['app.py'],  # 唯一启动入口：默认桌面版；app.py web 提供 Web 服务
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # 防止误收集其它 Qt 绑定（PyInstaller 收集到两个绑定会直接中止）。
    # 注意：QtQuick / QtQml / QtQuickWidgets / QtPositioning / QtWebChannel / QtNetwork / QtPrintSupport
    # 都是 Qt6WebEngineCore / Qt6WebEngineWidgets 的链接依赖，绝对不能排除。
    excludes=['PyQt5', 'PyQt6', 'PySide2'],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

# PySide6 自带的 WebEngine 调试资源（*.debug.pak / *.debug.bin，约 77MB）运行时用不到，不打进包。
a.datas = [entry for entry in a.datas if not entry[0].replace('\\', '/').endswith(('.debug.pak', '.debug.bin'))]

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

icon_path = os.path.join(assets_dir, '图标.png')

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='FishTool',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # 不用 UPX：Qt6 的 DLL 与 QtWebEngineProcess.exe 压缩后可能启动失败；本机是否装了 UPX 也不应改变打包结果。
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,  # 无控制台窗口
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=icon_path if os.path.exists(icon_path) else None,
)
