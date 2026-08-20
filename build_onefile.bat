@echo off
echo ==============================================
echo Building Single-file SystemHelper.exe
echo ==============================================

pip install -r requirements.txt

pyinstaller --noconfirm --onefile --windowed --name "SystemHelper" ^
  --collect-all aiortc ^
  --collect-all av ^
  --collect-all websockets ^
  monitor.py

echo.
echo Build hoan tat! File nam tai dist\SystemHelper.exe
pause
