@echo off
REM -----------------------------------------
REM  run_loop.bat — автоматический перезапуск
REM  %JSON_FILE%   — путь к данным (stdin)
REM  %HEADLESS%    — 0/1, переопределяет headless
REM  %MAX_RETRY%   — попыток подряд (по умолчанию 3)
REM  %COOLDOWN%    — сек паузы после исчерпания попыток
REM -----------------------------------------

set JSON_FILE=params.json
set HEADLESS=
set MAX_RETRY=3
set COOLDOWN=300

REM -------- internal -----------
:loop
set /a _count=0
:try
rem ---- build headless arg ----
set "HEADLESS_ARG="
if "%HEADLESS%"=="1" set "HEADLESS_ARG=--headless=true"
if "%HEADLESS%"=="0" set "HEADLESS_ARG=--headless=false"
type "%JSON_FILE%" | ^
python Samokat-TP.py %HEADLESS_ARG%
if %errorlevel% equ 0 goto loop

set /a _count+=1
echo [run_loop] Samokat-TP.py failed (try %_count%) at %date% %time%
if %_count% geq %MAX_RETRY% (
    echo [run_loop] cooldown %COOLDOWN%s
    timeout /t %COOLDOWN% > nul
    goto loop
)
goto try

