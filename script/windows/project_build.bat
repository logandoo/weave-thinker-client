@echo off
REM Build: sync config -> npm install (first run) -> vite build
setlocal
set ROOT=%~dp0..\..
cd /d "%ROOT%"
if not exist logs mkdir logs
echo == [1/3] sync config ==
node script\sync_config.mjs || exit /b 1
echo == [2/3] npm install (if needed) ==
if not exist node_modules ( call npm install --no-audit --no-fund || exit /b 1 ) else ( echo node_modules present - skip )
echo == [3/3] vite build ==
call npm --workspace app run build || exit /b 1
echo == BUILD OK ==
