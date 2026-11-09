@echo off
rem Operator check: positions, recent audit trail and the track-record stats,
rem written to logs\checks\<timestamp>.txt (and latest.txt). Read-only: no
rem model calls, no orders. Meant for a scheduler (Windows Task Scheduler,
rem twice a day) but fine by hand.
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
