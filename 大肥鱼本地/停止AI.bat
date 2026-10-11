@echo off
chcp 936 >nul
title Stop DeepSeek Server
cd /d "%~dp0"

echo 正在停止 DeepSeek 服务...
taskkill /f /im llama-server.exe >nul 2>&1
taskkill /f /im llama-cli.exe >nul 2>&1
ping -n 2 127.0.0.1 >nul

tasklist /fi "imagename eq llama-server.exe" 2>nul | find /i "llama-server.exe" >nul
if not errorlevel 1 goto still

echo 服务已停止，显存已释放。
goto done

:still
echo [警告] 服务仍在运行，请在任务管理器里手动结束 llama-server.exe

:done
echo.
ping -n 3 127.0.0.1 >nul
exit /b 0
