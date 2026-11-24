@echo off
powershell.exe -NoProfile -ExecutionPolicy RemoteSigned -File "%~dp0scripts\windows\open-steward.ps1" %*
if errorlevel 1 pause
