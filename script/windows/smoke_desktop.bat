@echo off
REM Desktop smoke: Electron --smoke -> checks mount/bridge/screenshot -> exit code
setlocal
set ROOT=%~dp0..\..
cd /d "%ROOT%"
if not exist logs mkdir logs
if not exist tests\screenshots mkdir tests\screenshots
if not exist tests\fixtures mkdir tests\fixtures
> tests\fixtures\bridge_fixture.txt echo weave-bridge-ok
if not exist app\dist ( echo run project_build.bat first & exit /b 1 )
node_modules\.bin\electron.cmd desktop --smoke --screenshot="%ROOT%\tests\screenshots\desktop_login.png" --fixture="%ROOT%\tests\fixtures\bridge_fixture.txt" > logs\smoke.log 2>&1
set RC=%errorlevel%
type logs\smoke.log
echo smoke exit code: %RC%
exit /b %RC%
