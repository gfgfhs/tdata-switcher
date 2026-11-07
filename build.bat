@echo off
echo Building tdata_switcher.exe ...
pip install -r requirements.txt
pyinstaller --onefile --console --name tdata_switcher --clean tdata_switcher.py
echo.
echo Done! File: dist\tdata_switcher.exe
pause
