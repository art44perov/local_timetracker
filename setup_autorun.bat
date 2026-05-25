@echo off
set APP_PATH=%~dp0
reg add HKCU\Software\Microsoft\Windows\CurrentVersion\Run /v TimeTracker /t REG_SZ /d "%APP_PATH%run_tracker.bat" /f
pause