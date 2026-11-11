@echo off
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0Read-OutlookMail.ps1" %*
