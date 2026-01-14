## IntelAvatar Dev Guide: Install, Run & Release

This guide outlines the steps to prepare your project environment and package it using PyInstaller. We emphasize using a **Virtual Environment (venv)** to ensure PyInstaller correctly bundles all necessary dependencies.

### Verify Python Version
Ensure Python 3.10 is installed on your system.
If not installed, please download the specific version from the [Official Python Website](https://www.python.org/downloads/).

### Create and Activate the Virtual Environment
```bash
# Windows
py -3.10 -m venv intel_ava
intel_ava\Scripts\activate
```

### Install Project Dependencies
```bash
pip install -r requirements_v2.txt 

# add proxy if needed: 
# pip install -r requirements_v2.txt --proxy "http://proxy-dmz.intel.com:912"
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

```bash
pyinstaller app.py --name IntelAvatar  --add-data "templates;templates" --add-data "blueprints;blueprints" --add-data "configs;configs" --add-data "models;models" --add-data "static;static" --add-data "services;services" --add-data "utils;utils" --hidden-import engineio.async_drivers.threading --hidden-import socketio.async_drivers.threading --hidden-import snowflake.connector.snow_logging --hidden-import py7zr --noconfirm --icon=icon.ico

```

The final executable file will be placed in the newly created **dist folder** within your project directory: 
```bash
dist/IntelAvatar 
# The directory 'IntelAvatar' contains the EXE and all bundled files.
# pack this directory and release exe
```