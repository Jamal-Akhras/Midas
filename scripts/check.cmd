@echo off
rem Operator check: positions, recent audit trail and the track-record stats,
rem written to logs\checks\<timestamp>.txt (and latest.txt), then the Telegram
rem brief with a Claude review of that report (midas brief --send --review).
rem Places no orders and calls no model through the router. Meant for a
rem scheduler (Windows Task Scheduler, twice a day) but fine by hand.
rem
rem   scripts\check.cmd [path\to\midas.exe]
rem
rem The argument is for schedulers that run without the Python environment
rem on PATH; by default `midas` is taken from PATH.
setlocal
cd /d "%~dp0.."
set "MIDAS=%~1"
if "%MIDAS%"=="" set "MIDAS=midas"
set "OUT=logs\checks"
if not exist "%OUT%" mkdir "%OUT%"
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd_HHmm"') do set "STAMP=%%i"
set "FILE=%OUT%\%STAMP%.txt"
(
  echo === midas check %STAMP% ^(machine local time^) ===
  echo.
  echo --- positions ---
  "%MIDAS%" positions
  echo.
  echo --- audit, last 30 events ---
  "%MIDAS%" audit --limit 30
  echo.
  echo --- stats ---
  "%MIDAS%" stats
) > "%FILE%" 2>&1
copy /y "%FILE%" "%OUT%\latest.txt" >nul
echo wrote %FILE%
rem The brief exits 1 (and says so in its file) until TELEGRAM_BOT_TOKEN and
rem TELEGRAM_CHAT_ID are set in .env; the report above is written either way.
"%MIDAS%" brief --send --review --report "%FILE%" > "%OUT%\%STAMP%.brief.txt" 2>&1
copy /y "%OUT%\%STAMP%.brief.txt" "%OUT%\latest.brief.txt" >nul
echo wrote %OUT%\%STAMP%.brief.txt
