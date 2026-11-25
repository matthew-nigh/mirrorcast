@echo off
REM Run from source without building an .exe. First run installs the libraries.
setlocal
cd /d "%~dp0"
where py >nul 2>nul && (set PY=py -3) || (set PY=python)
if not exist .venv\Scripts\python.exe (
  echo First run: setting up, this takes a minute...
  %PY% -m venv .venv || goto :fail
  .venv\Scripts\python -m pip install --upgrade pip
  .venv\Scripts\python -m pip install -r requirements.txt || goto :fail
)
.venv\Scripts\python main.py %*
exit /b %errorlevel%
:fail
echo Setup failed. Is Python 3.10+ installed from python.org (with "Add to PATH")?
pause
exit /b 1
