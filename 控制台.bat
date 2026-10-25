@echo off
chcp 65001 >nul
title gudum 控制台
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [首次运行] 正在创建虚拟环境并安装依赖，只需几分钟...
    python -m venv .venv
    ".venv\Scripts\python.exe" -m pip install -q -r requirements.txt
    if errorlevel 1 (
        echo 依赖安装失败，试试国内源后重跑本脚本：
        echo    ".venv\Scripts\python.exe" -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
        pause
        exit /b 1
    )
)

".venv\Scripts\python.exe" -m app.panel
echo.
echo 控制台已退出。
pause
