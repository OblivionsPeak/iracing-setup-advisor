@echo off
setlocal EnableDelayedExpansion

:: ── Read version ─────────────────────────────────────────────────────────────
set /p VERSION=<version.txt
set VERSION=%VERSION: =%
if "%VERSION%"=="" ( echo ERROR: version.txt is empty. & pause & exit /b 1 )

echo.
echo ============================================================
echo   Building iRacing Setup Advisor v%VERSION%
echo ============================================================
echo.

:: ── Activate venv ────────────────────────────────────────────────────────────
if not exist venv\Scripts\activate.bat (
    echo ERROR: Run setup.bat first to create the virtual environment.
    pause & exit /b 1
)
call venv\Scripts\activate.bat

:: ── Install / upgrade PyInstaller ────────────────────────────────────────────
echo [1/3] Installing PyInstaller...
pip install pyinstaller --quiet --upgrade
if %errorlevel% neq 0 ( echo PyInstaller install failed. & pause & exit /b 1 )

:: ── Clean previous build ─────────────────────────────────────────────────────
if exist build  rmdir /s /q build
if exist dist   rmdir /s /q dist

:: ── Build ────────────────────────────────────────────────────────────────────
echo [2/3] Building executable...
pyinstaller ^
    --onefile ^
    --noconsole ^
    --icon=icon.ico ^
    --name "SetupAdvisor-v%VERSION%" ^
    --add-data "version.txt;." ^
    --add-data "cars;cars" ^
    --add-data "tracks;tracks" ^
    --hidden-import=flask ^
    --hidden-import=werkzeug ^
    --hidden-import=werkzeug.serving ^
    --hidden-import=werkzeug.routing ^
    --hidden-import=numpy ^
    --collect-submodules=flask ^
    app.py

if %errorlevel% neq 0 ( echo Build failed — see errors above. & pause & exit /b 1 )

:: ── Done ─────────────────────────────────────────────────────────────────────
echo [3/3] Done!
echo.
echo   Output: dist\SetupAdvisor-v%VERSION%.exe
echo.
echo   Share that single file — no Python install needed.
echo.

:: Auto-bump patch version for next build (1.0.0 -> 1.0.1)
for /f "tokens=1,2,3 delims=." %%a in ("%VERSION%") do (
    set MAJOR=%%a
    set MINOR=%%b
    set /a PATCH=%%c+1
)
echo %MAJOR%.%MINOR%.%PATCH%> version.txt
echo   version.txt bumped to %MAJOR%.%MINOR%.%PATCH% for your next build.
echo.

pause
