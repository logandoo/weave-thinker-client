@echo off
REM Start desktop client (minimized) — records PID via PowerShell -PassThru (pid file discipline)
setlocal
set ROOT=%~dp0..\..
cd /d "%ROOT%"
if not exist logs mkdir logs
if not exist app\dist ( echo run script\windows\project_build.bat first & exit /b 1 )
if not exist node_modules\.bin\electron.cmd ( echo electron missing - run project_build.bat & exit /b 1 )
if exist logs\desktop.pid (
  set /p OLD=<logs\desktop.pid
  tasklist /FI "PID eq %OLD%" 2>nul | find "%OLD%" >nul && ( echo already running pid %OLD% & exit /b 0 )
)
powershell -NoProfile -Command "$p = Start-Process -FilePath 'node_modules\.bin\electron.cmd' -ArgumentList 'desktop' -WindowStyle Minimized -PassThru -RedirectStandardOutput 'logs\desktop.log' -RedirectStandardError 'logs\desktop.err.log'; $p.Id | Out-File -Encoding ascii 'logs\desktop.pid'"
echo started — pid in logs\desktop.pid, log in logs\desktop.log
