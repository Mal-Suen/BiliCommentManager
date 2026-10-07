@echo off
rem 停止图形界面（后台删除/拉取任务不受影响）
set PID=
set /p PID=<"%~dp0gui.pid"
if "%PID%"=="" goto none
taskkill /F /PID %PID% >nul 2>&1
if %errorlevel%==0 (
  echo 界面已停止。
) else (
  echo 界面未在运行。
)
goto end
:none
echo 未找到界面进程记录（界面可能未启动过）。
:end
pause
