@echo off
chcp 65001 >nul
title gudum · QQ 协议端
cd /d "%~dp0"

set "ACCOUNT="
for /f "tokens=1,* delims==" %%A in (.env) do (
    if /i "%%A"=="NAPCAT_ACCOUNT" set "ACCOUNT=%%B"
)

REM 按当前 .env 刷新 NapCat 反向 WS 配置（令牌/端口改了也自动同步）
".venv\Scripts\python.exe" -m app.napcat_config

echo.
echo 如需扫码登录：查看 napcat 目录下新生成的二维码（控制台/napcat cache 目录）
echo WebUI 位于 http://127.0.0.1:6099 （忘记密码时可删 napcat/config 里的 webui 配置重置）
echo.
".venv\Scripts\python.exe" -m app.napcat_runner --account "%ACCOUNT%"
pause
