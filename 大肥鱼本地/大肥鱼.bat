@echo off
chcp 936 >nul
title 大肥鱼本地 · 控制台
cd /d "%~dp0"

python control.py
if errorlevel 1 (
  echo.
  echo [错误] 控制台没能运行。请确认 Python 已安装并加入 PATH：
  echo        在命令行执行  python --version  看看有没有输出。
  echo.
  pause
)
