@echo off
rem Builds dist\NewMUD.exe (a single file, no Python needed to run it)
cd /d "%~dp0"
python -m pip install --quiet pyinstaller
python -m PyInstaller --noconfirm --clean --onefile --windowed ^
  --name NewMUD ^
  --icon newmud\newmud.ico ^
  --add-data "newmud\newmud.ico;newmud" ^
  NewMUD.pyw
echo.
echo Done: dist\NewMUD.exe
