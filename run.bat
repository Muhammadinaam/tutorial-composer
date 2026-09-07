@echo off
cd /d "%~dp0"
if exist "%~dp0venv\Scripts\python.exe" (
  "%~dp0venv\Scripts\python.exe" run.py
) else (
  echo Create the virtual environment first:
  echo   py -3 -m venv venv
  echo   venv\Scripts\activate
  echo   pip install -r requirements.txt
  exit /b 1
)
