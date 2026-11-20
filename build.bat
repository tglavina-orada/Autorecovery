@echo off
rem Builds a portable single-file dist\Autorecovery.exe (no Python needed on the target PC).
setlocal
cd /d "%~dp0"

if not exist .venv (
    python -m venv .venv || goto :error
)
call .venv\Scripts\python -m pip install -q -r requirements.txt || goto :error

rem generate the exe icon from the same artwork as the tray icon
call .venv\Scripts\python -c "import autorecovery as a; a.make_icon_image(256).save('autorecovery.ico', sizes=[(16,16),(32,32),(48,48),(64,64),(256,256)])" || goto :error

call .venv\Scripts\python -m PyInstaller --noconfirm --clean --onefile --windowed ^
    --name Autorecovery --icon autorecovery.ico ^
    --hidden-import pystray._win32 ^
    autorecovery.py || goto :error

echo.
echo Done: %~dp0dist\Autorecovery.exe
exit /b 0

:error
echo Build failed.
exit /b 1
