@echo off
rem MAA_remote launcher - double-click to run, or register with Task Scheduler (see README.md)
cd /d "%~dp0"
if "%DEEPSEEK_API_KEY%"=="" (
    echo [ERROR] DEEPSEEK_API_KEY is not set. Configure it in System Environment Variables first.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" -m maa_remote
echo.
echo [maa_remote] process exited unexpectedly - check logs\maa_remote.log
pause
