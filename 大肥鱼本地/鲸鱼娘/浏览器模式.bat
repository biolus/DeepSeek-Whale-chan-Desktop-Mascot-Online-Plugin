@echo off
chcp 936 >nul
title 鲸鱼娘（浏览器模式）
cd /d "%~dp0"

echo ================================================================
echo    鲸鱼娘 · 浏览器模式
echo ================================================================
echo.
echo  这个模式不装 Electron 也能看她，用来排查问题：
echo    · 能看到她 = 后端没问题，是外壳的问题
echo    · 看不到她 = 后端或模型有问题
echo.
echo  缺点：有标题栏、不透明、不能置顶、会挡你干活。
echo.

powershell -NoProfile -Command "try{ if((Invoke-WebRequest 'http://127.0.0.1:8095/api/config' -UseBasicParsing -TimeoutSec 2).StatusCode -eq 200){exit 0} }catch{}; exit 1" >nul 2>&1
if errorlevel 1 (
  echo 启动后端...
  start "鲸鱼娘-后端" /min cmd /c "cd /d "%~dp0" && python server.py"
  timeout /t 4 /nobreak >nul
)

echo 打开页面...
start "" http://127.0.0.1:8095/
timeout /t 3 /nobreak >nul
exit /b 0
