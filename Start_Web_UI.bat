@echo off
REM ProcessLens (web UI): http://127.0.0.1:8008
cd /d "%~dp0"
if exist .venv\Scripts\python.exe (set PY=.venv\Scripts\python.exe) else (set PY=python)
%PY% -c "import fastapi, reportlab" 2>NUL || %PY% -m pip install fastapi==0.163.1 reportlab==5.1.0 python-multipart==0.0.32 uvicorn==0.54.0
start "" http://127.0.0.1:8008
%PY% -m app.api
