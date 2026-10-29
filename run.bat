@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem Python venv is kept outside OneDrive (%LOCALAPPDATA%\theme_score_venv)
set "PY=%LOCALAPPDATA%\theme_score_venv\Scripts\python.exe"
if not exist "%PY%" (
  python -m venv "%LOCALAPPDATA%\theme_score_venv"
  "%PY%" -m pip install -q yfinance pandas pyyaml pypdf openpyxl xlrd curl_cffi
)
rem 1) Build report.html with the latest data
"%PY%" theme_score.py %*
rem 2) Start the local server and open the dashboard (keep this window open to use the diagnose tab)
"%PY%" server.py
pause
