@echo off
powershell.exe -NoProfile -STA -ExecutionPolicy RemoteSigned -File "%~dp0scripts\windows\set-feishu-secret.ps1" %*
if errorlevel 1 pause
