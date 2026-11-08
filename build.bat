@echo off
echo Building tdata_switcher.exe (GUI) and tdata_switcher_cli.exe (console) ...
pip install -r requirements.txt || goto :error
pyinstaller --onefile --windowed --clean --name tdata_switcher --hidden-import tdata_gui tdata_switcher.py || goto :error
pyinstaller --onefile --console --clean --name tdata_switcher_cli tdata_switcher_cli.py || goto :error
echo.
echo Done!
echo   dist\tdata_switcher.exe       - window (GUI)
echo   dist\tdata_switcher_cli.exe   - text menu (console)
pause
exit /b 0

:error
echo.
echo Build failed.
pause
exit /b 1
