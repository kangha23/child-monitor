@echo off
echo ==============================================
echo Building Single-file SystemHelper.exe
echo ==============================================

pip install -r requirements.txt

pyinstaller --noconfirm --onefile --windowed --name "SystemHelper" ^
  --collect-all aiortc ^
  --collect-all av ^
  --collect-all websockets ^
  --add-data "config.json;." ^
  --add-data "web_viewer;web_viewer" ^
  --add-data "version.txt;." ^
  monitor.py

echo.
echo Build hoan tat! File nam tai dist\SystemHelper.exe

