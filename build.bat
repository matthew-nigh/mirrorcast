@echo off
REM One-click build of dist\ChromecastMirror.exe. Needs Python 3.10+ from python.org.
setlocal
cd /d "%~dp0"
where py >nul 2>nul && (set PY=py -3) || (set PY=python)
echo Creating build environment...
%PY% -m venv .venv || goto :fail
call .venv\Scripts\activate.bat || goto :fail
python -m pip install --upgrade pip || goto :fail
python -m pip install -r requirements-dev.txt || goto :fail
echo Building the .exe (takes a minute or two)...
pyinstaller --noconfirm --clean ChromecastMirror.spec || goto :fail
echo.
echo Done: %cd%\dist\ChromecastMirror.exe
pause
exit /b 0
:fail
echo.
echo Build failed. Scroll up for the error.
pause
exit /b 1
