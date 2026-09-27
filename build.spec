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
    'PyQt5.QtCore',
    'PyQt5.QtGui',
    'PyQt5.QtWidgets',
    'PyQt5.QtWebEngineWidgets',
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
    ['start_desktop.py', 'start_web.py'],  # 同时打包桌面和 Web 启动脚本
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

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
    upx=True,
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
