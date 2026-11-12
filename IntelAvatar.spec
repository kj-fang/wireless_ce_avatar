# -*- mode: python ; coding: utf-8 -*-
#
# Plain build (CI, build_with_version.ps1 without -HandsfreeRole): IntelAvatar,
# the role decided at start (--handsfree-role / INTELAVATAR_HANDSFREE_ROLE,
# else client).
#
# Role package (build_with_version.ps1 -HandsfreeRole client|server|both):
#   AVATAR_BUILD_NAME      name of the exe and the dist folder
#   AVATAR_HANDSFREE_ROLE  client | server — written to handsfree_role.txt in
#                          the bundle; app.py reads it when neither the flag
#                          nor the environment names a role.
import os

NAME = os.environ.get("AVATAR_BUILD_NAME", "").strip() or "IntelAvatar"
ROLE = os.environ.get("AVATAR_HANDSFREE_ROLE", "").strip().lower()
if ROLE and ROLE not in ("client", "server"):
    raise SystemExit(f"AVATAR_HANDSFREE_ROLE must be client or server, not {ROLE!r}")

datas = [('templates', 'templates'), ('blueprints', 'blueprints'), ('configs', 'configs'), ('models', 'models'), ('static', 'static'), ('services', 'services'), ('utils', 'utils'), ('tray_manager.py', '.'), ('icon.ico', '.')]
if ROLE:
    role_dir = os.path.join("build", f"role_{ROLE}")
    os.makedirs(role_dir, exist_ok=True)
    role_file = os.path.join(role_dir, "handsfree_role.txt")
    with open(role_file, "w", encoding="utf-8") as fh:
        fh.write(ROLE + "\n")
    datas.append((role_file, '.'))


a = Analysis(
    ['app.py'],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=['engineio.async_drivers.threading', 'socketio.async_drivers.threading', 'snowflake.connector.snow_logging', 'py7zr', 'psutil', 'pystray', 'PIL', 'PIL.Image', 'anthropic'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['icon.ico'],
    version='version_info.txt',
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name=NAME,
)
