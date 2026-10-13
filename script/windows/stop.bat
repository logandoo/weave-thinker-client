@echo off
REM Stop desktop client by recorded pid only (no pattern kill)
setlocal
set ROOT=%~dp0..\..
cd /d "%ROOT%"
if not exist logs\desktop.pid ( echo no pid file & exit /b 0 )
set /p PID=<logs\desktop.pid
taskkill /PID %PID% >nul 2>&1
if %errorlevel%==0 ( echo stopped pid %PID% ) else ( echo pid %PID% not running )
del logs\desktop.pid >nul 2>&1
