@echo off
rem Pure ASCII wrapper: double-click to start musicbot.
rem Real logic lives in run.ps1 (UTF-8) to avoid console codepage issues.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run.ps1" %*
pause
