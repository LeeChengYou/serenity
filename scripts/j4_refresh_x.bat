@echo off
set PYTHONIOENCODING=utf-8
set REPO=C:\Users\Jeff\OneDrive\桌面\git_repo\serenity
set PY=C:\Python314\python.exe

echo [J4] Refreshing X cookies...
"%PY%" "%REPO%\scripts\crawler.py" refresh-cookies
if %ERRORLEVEL% NEQ 0 (
    echo [J4] crawler refresh-cookies failed, skipping fetch-x
    exit /b 1
)

echo [J4] Fetching X posts...
"%PY%" "%REPO%\scripts\ingest.py" fetch-x --max-pages 20
echo [J4] Done.
