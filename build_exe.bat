@echo off
rem Local build of ZapretGUI.exe (run from the repo root, Windows).
rem "python -m ..." guarantees the same interpreter for pip and PyInstaller.
python -m pip install --upgrade -r requirements.txt pyinstaller || exit /b 1
python -m PyInstaller --noconfirm --onefile --noconsole --uac-admin --name ZapretGUI ^
  --icon assets\icon.ico --add-data "assets\icon.ico;assets" ^
  --hidden-import pystray._win32 --collect-all customtkinter zapret_gui.py || exit /b 1
copy /Y dist\ZapretGUI.exe ZapretGUI.exe >nul
echo.
echo Done: ZapretGUI.exe (copy in dist\ZapretGUI.exe)
