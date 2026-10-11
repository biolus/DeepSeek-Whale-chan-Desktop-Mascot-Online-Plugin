@echo off
chcp 936 >nul
title 鲸鱼娘桌宠
cd /d "%~dp0"

echo ================================================================
echo    鲸鱼娘桌宠  启动中
echo ================================================================
echo.

rem ================================================================
rem  这个脚本里有三个踩过的坑，都实测确认过：
rem
rem  ① 中文路径让 electron.exe 崩
rem     实测（各跑 3 次，结果确定）：
rem       <含中文的父目录>\_etest\  -> 崩溃 0xc0000005，0/3 成功
rem       D:\etest_ascii\abc\    -> 正常，3/3 成功
rem     在这样的中文目录树下 electron.exe 会确定性地访问冲突。
rem     所以启动时把外壳同步一份到纯 ASCII 的短路径再跑。
rem     主体文件仍然在 鲸鱼娘\外壳\，那边是主副本。
rem
rem  ② DSH 会给子进程设 ELECTRON_RUN_AS_NODE=1
rem     那会让 Electron 退化成纯 Node，报 Cannot find module electron。
rem     必须清掉，而且要写成 set "VAR=" 的带引号形式 ——
rem     写成 set VAR= 再接 && 时，cmd 会把它设成一个空格，空格是真值。
rem
rem  ③ 健康检查不能用 HTTP
rem     国内常见 Clash / 机场会把系统代理设成 127.0.0.1:7890，
rem     Invoke-WebRequest 会走代理，于是本地服务被判成"没起来"，
rem     后端明明在跑却一直重试。这里一律用裸 TCP 连接探测。
rem ================================================================

set "RUNROOT=%LOCALAPPDATA%\DSWhalePet"
set "SRC=%~dp0外壳"

rem ---------- 1. 后端 ----------
powershell -NoProfile -Command "try{$c=New-Object Net.Sockets.TcpClient;$c.Connect('127.0.0.1',8095);$c.Close();exit 0}catch{exit 1}" >nul 2>&1
if not errorlevel 1 goto backend_ok

echo [1/5] 启动桌宠后端...
start "鲸鱼娘-后端" /min python server.py
echo       等待后端就绪...
powershell -NoProfile -Command "for($i=0;$i -lt 40;$i++){try{$c=New-Object Net.Sockets.TcpClient;$c.Connect('127.0.0.1',8095);$c.Close();exit 0}catch{}; Start-Sleep -Milliseconds 500}; exit 1" >nul 2>&1
if errorlevel 1 goto backend_fail
echo       后端就绪。
goto backend_ok

:backend_ok
echo [1/5] 后端已在运行。

rem ---------- 2. 大脑 ----------
echo.
echo [2/5] 检查本地大脑...
powershell -NoProfile -Command "try{$c=New-Object Net.Sockets.TcpClient;$c.Connect('127.0.0.1',8080);$c.Close();exit 0}catch{exit 1}" >nul 2>&1
if not errorlevel 1 (
  echo       大脑在线。她会在你不跟她说话时把它卸掉、腾出显存。
) else (
  echo       大脑待命 —— 不用手动起了，她要开口时会自己加载模型。
  echo       第一次说话会多等几秒加载，之后不聊天就自动卸载，不占显存。
)

rem ---------- 3. ASCII 运行目录 ----------
echo.
echo [3/5] 准备 ASCII 运行目录 %RUNROOT% ...

if not exist "%SRC%\electron\electron.exe" goto no_shell

if exist "%RUNROOT%\electron\electron.exe" goto sync

echo       首次运行，拷贝 Electron 运行时（约 270MB，只需一次）...
robocopy "%SRC%" "%RUNROOT%" /E /NFL /NDL /NJH /NJS /NP /R:1 /W:1 >nul
if not exist "%RUNROOT%\electron\electron.exe" goto copy_fail
echo       拷贝完成。
goto sync

:sync
copy /Y "%SRC%\main.js" "%RUNROOT%\" >nul 2>&1
copy /Y "%SRC%\preload.js" "%RUNROOT%\" >nul 2>&1
copy /Y "%SRC%\package.json" "%RUNROOT%\" >nul 2>&1
if exist "%SRC%\build\" xcopy /Y /E /I /Q "%SRC%\build" "%RUNROOT%\build" >nul 2>&1
echo       运行目录就绪。

rem ---------- 4. 起外壳 ----------
echo.
echo [4/5] 叫醒鲸鱼娘...
set "ELECTRON_RUN_AS_NODE="
cd /d "%RUNROOT%"
start "" "electron\electron.exe" .

rem ---------- 5. 验证 ----------
echo.
echo [5/5] 等她出现...
timeout /t 6 /nobreak >nul
tasklist /fi "imagename eq electron.exe" 2>nul | find /i "electron.exe" >nul
if errorlevel 1 goto shell_fail

echo.
echo   OK 她起来了，应该在桌面右下角。
echo.
echo     · 鼠标压在她身上才收点击，其它地方直接穿透，不挡你干活
echo     · 拖她 = 拖窗口；右键 = 她的菜单（表情/换装/场景/动作/大小）
echo     · 双击 = 跟她说句话；左上角大脑按钮 = 换本地模型
echo     · 平时不占显卡：不跟她说话时本模型会自动卸载，开口前才加载
echo     · 托盘图标 = 显示/隐藏/重新加载/退出
echo.
timeout /t 8 /nobreak >nul
exit /b 0

:shell_fail
echo.
echo   [失败] Electron 起来了又立刻退出。
echo.
echo   请改用 浏览器模式.bat —— 功能完全一样，只是有窗口边框。
echo   想知道崩在哪：事件查看器 - Windows 日志 - 应用程序
echo   找 electron.exe 的 Application Error，把异常代码发我。
echo.
timeout /t 8 /nobreak >nul
exit /b 1

:no_shell
echo [错误] 找不到 %SRC%\electron\electron.exe
echo        Electron 运行时没装好。
timeout /t 8 /nobreak >nul
exit /b 1

:copy_fail
echo [错误] 拷贝到 %RUNROOT% 失败，可能是 D 盘权限问题。
echo        可以手动把这个文件夹整个拷过去：
echo          %SRC%
echo        目标： %RUNROOT%
echo        然后重新双击本脚本。
timeout /t 10 /nobreak >nul
exit /b 1

:backend_fail
echo [错误] 后端启动失败。
echo        手动排查：在这个目录执行  python server.py  看报错。
timeout /t 10 /nobreak >nul
exit /b 1
