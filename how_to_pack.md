## IntelAvatar Dev Guide: Install, Run & Release

This guide outlines the steps to prepare your project environment and package it using PyInstaller. We emphasize using a **Virtual Environment (venv)** to ensure PyInstaller correctly bundles all necessary dependencies.

### Verify Python Version
Ensure Python 3.10 or 3.12 is installed on your system.
If not installed, please download the specific version from the [Official Python Website](https://www.python.org/downloads/).

### Create and Activate the Virtual Environment
```bash
# Windows
py -3.10 -m venv intel_ava
# py -3.12 -m venv intel_ava
intel_ava\Scripts\activate
```

### Install Project Dependencies
```bash
pip install -r requirements.txt 

# add proxy if needed: 
# pip install -r requirements.txt --proxy "http://proxy-dmz.intel.com:912"
```

### Run Avatar
```bash
intel_ava\Scripts\activate # or abs path to venv activate
python app.py # or use python in venv
```

### PyInstaller: Pack to EXE

Ensure the PyInstaller package is installed within the active virtual environment:
```bash
pip install pyinstaller
```
**PRE-REQUISITES:**

* **Virtual Environment Must Be Active:** Ensure your terminal prompt shows (intel_ava).

* **Location:** This command must be run from the project root directory where your app.py file is located.
For Only Web app:
```bash
pyinstaller app.py --name IntelAvatar  --add-data "templates;templates" --add-data "blueprints;blueprints" --add-data "configs;configs" --add-data "models;models" --add-data "static;static" --add-data "services;services" --add-data "utils;utils" --hidden-import engineio.async_drivers.threading --hidden-import socketio.async_drivers.threading --hidden-import snowflake.connector.snow_logging --hidden-import py7zr --noconfirm --icon=icon.ico

```

For adding system tray, SendTo:
```bash
pyinstaller app.py --name IntelAvatar --add-data "templates;templates" --add-data "blueprints;blueprints" --add-data "configs;configs" --add-data "models;models" --add-data "static;static" --add-data "services;services" --add-data "utils;utils" --add-data "tray_manager.py;." --add-data "icon.ico;." --hidden-import engineio.async_drivers.threading --hidden-import socketio.async_drivers.threading --hidden-import snowflake.connector.snow_logging --hidden-import py7zr --hidden-import psutil --hidden-import pystray --hidden-import PIL --hidden-import PIL.Image --hidden-import anthropic --noconfirm --icon=icon.ico
```

The final executable file will be placed in the newly created **dist folder** within your project directory: 
```bash
dist/IntelAvatar 
# The directory 'IntelAvatar' contains the EXE and all bundled files.
# pack this directory and release exe
```

### Beacon (Handsfree) packages: one for the client PCs, one for the server

`build_with_version.ps1` can build one package per Beacon role instead of the plain build.
The role is baked into the package (`_internal/handsfree_role.txt`), so the exe starts in that
role without any flag; `--handsfree-role` or `INTELAVATAR_HANDSFREE_ROLE` still win when given.
Each package gets its launcher `.bat` and README from `tools/packaging/<role>/` and is zipped.
```powershell
intel_ava\Scripts\Activate.ps1
.\build_with_version.ps1 -HandsfreeRole both     # or: client | server
# dist/IntelAvatar_Client/  IntelAvatar_Client_v<version>.exe  "Beacon Client.bat"  README-Beacon-client.txt
# dist/IntelAvatar_Server/  IntelAvatar_Server_v<version>.exe  "Beacon Server.bat"  README-Beacon-server.txt
# dist/IntelAvatar_Client_v<version>.zip, dist/IntelAvatar_Server_v<version>.zip  -> hand these out
```
Without `-HandsfreeRole` the script builds `dist/IntelAvatar` exactly as CI does (the spec reads
`AVATAR_BUILD_NAME` / `AVATAR_HANDSFREE_ROLE` only when the script sets them).
