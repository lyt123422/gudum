@echo off
chcp 65001 >nul
title gudum · 机器人内核
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

:loop
echo.
echo [%date% %time%] 启动机器人内核（监听 127.0.0.1:8081，异常退出 5 秒自动重启）
".venv\Scripts\python.exe" -m app.main
echo.
echo [%date% %time%] 内核已退出，5 秒后自动重启（要彻底停止请直接关闭本窗口）
timeout /t 5 /nobreak >nul
goto loop
