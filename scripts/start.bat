@echo off
rem 启动旁路服务；意外退出后 10 秒自动重启。关闭这个窗口即停止。
chcp 65001 >nul
title 幻梦境骰娘 旁路服务
cd /d "%~dp0.."

rem 开机自启时带 delay 参数：先等 60 秒，让 SnowLuma 和 QQ 先启动
if /i "%~1"=="delay" timeout /t 60 /nobreak >nul

set "UV=uv"
where uv >nul 2>nul || set "UV=%USERPROFILE%\.local\bin\uv.exe"

:loop
"%UV%" run python -m sidecar
echo.
echo 旁路服务已退出（%date% %time%），10 秒后重启。要彻底停止请直接关闭这个窗口。
timeout /t 10 /nobreak >nul
goto loop
