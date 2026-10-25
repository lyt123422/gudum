@echo off
chcp 65001 >nul
title gudum · QQ 协议端
cd /d "%~dp0"

if not exist ".env" copy /Y ".env.example" ".env" >nul

if not exist ".venv\Scripts\python.exe" (
    echo [首次运行] 正在创建虚拟环境并安装依赖，只需几分钟...
    python -m venv .venv
    ".venv\Scripts\python.exe" -m pip install -q -r requirements.txt
    if errorlevel 1 (
        echo 依赖安装失败，试试换国内源后重跑本脚本：
        echo    ".venv\Scripts\python.exe" -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
        pause
        exit /b 1
    )
)

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
