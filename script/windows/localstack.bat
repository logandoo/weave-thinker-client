@echo off
REM Local stack (Windows) — PG supervisor + backend, pid-file discipline.
REM LOCALSTACK_HOME 可覆盖数据/日志根（默认 %ROOT%\.localstack）
setlocal
set ROOT=%~dp0..\..
cd /d "%ROOT%"
if not defined LOCALSTACK_HOME set LOCALSTACK_HOME=%ROOT%\.localstack
set LS=%LOCALSTACK_HOME%
set LOGDIR=%LS%\logs
set VENV=%LS%\venv
set PY=%VENV%\Scripts\python.exe
if not exist "%LOGDIR%" mkdir "%LOGDIR%"

if "%1"=="start" (
  if not exist "%PY%" (
    echo venv missing: python -m venv "%VENV%" ^&^& "%VENV%\Scripts\pip" install -r localstack\requirements-pg.txt
    exit /b 1
  )
  powershell -NoProfile -Command "$p = Start-Process -FilePath '%PY%' -ArgumentList 'localstack\pg_server.py','start','--data-dir','%LS%\pgdata','--state-file','%LS%\pg_state.json','--port','55432' -WindowStyle Minimized -PassThru -RedirectStandardOutput '%LOGDIR%\localstack_pg.log' -RedirectStandardError '%LOGDIR%\localstack_pg.err.log'; $p.Id | Out-File -Encoding ascii '%LOGDIR%\localstack_pg.pid'"
  echo pg starting — log %LOGDIR%\localstack_pg.log
  exit /b 0
)
if "%1"=="stop" (
  if exist "%LOGDIR%\localstack_pg.pid" (
    set /p PID=<"%LOGDIR%\localstack_pg.pid"
    taskkill /PID %PID% >nul 2>&1
    del "%LOGDIR%\localstack_pg.pid" >nul 2>&1
    echo pg stopped
  ) else echo no pid file
  exit /b 0
)
if "%1"=="psql" (
  "%PY%" localstack\pg_server.py exec --state-file "%LS%\pg_state.json" --sql "%2"
  exit /b %errorlevel%
)
if "%1"=="backend-start" (
  python localstack\gen_config.py
  cd server
  powershell -NoProfile -Command "$p = Start-Process -FilePath 'python' -ArgumentList '-m','uvicorn','main:app','--host','127.0.0.1','--port','8159' -WindowStyle Minimized -PassThru -RedirectStandardOutput '%LOGDIR%\backend_local.log' -RedirectStandardError '%LOGDIR%\backend_local.err.log'; $p.Id | Out-File -Encoding ascii '%LOGDIR%\backend_local.pid'"
  echo backend starting on 8159
  exit /b 0
)
if "%1"=="backend-stop" (
  if exist "%LOGDIR%\backend_local.pid" (
    set /p BPID=<"%LOGDIR%\backend_local.pid"
    taskkill /PID %BPID% >nul 2>&1
    del "%LOGDIR%\backend_local.pid" >nul 2>&1
    echo backend stopped
  ) else echo no backend pid
  exit /b 0
)
if "%1"=="status" (
  if exist "%LOGDIR%\localstack_pg.pid" ( echo pg pid: & type "%LOGDIR%\localstack_pg.pid" ) else ( echo pg not running )
  if exist "%LOGDIR%\backend_local.pid" ( echo backend pid: & type "%LOGDIR%\backend_local.pid" ) else ( echo backend not running )
  exit /b 0
)
echo usage: localstack.bat start^|stop^|status^|psql "SQL"^|backend-start^|backend-stop
