@echo off
title Solverr High-Performance Native Launcher
echo ==========================================
echo   ⚡ Solverr - Native Windows Host Launcher
echo ==========================================
cd /d "%~dp0"
powershell -ExecutionPolicy Bypass -File "%~dp0run_native.ps1"
pause
