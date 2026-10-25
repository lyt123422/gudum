@echo off
chcp 65001 >nul
title gudum · 一键启动
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [首次运行] 正在创建虚拟环境并安装依赖，只需几分钟...
    where python >nul 2>nul
    if errorlevel 1 (
        echo [错误] 没有找到 Python。请先安装 Python 3.10+，安装时勾选 Add python.exe to PATH。
        pause
        exit /b 1
    )
    python -m venv .venv
    ".venv\Scripts\python.exe" -m pip install -q -r requirements.txt
    if errorlevel 1 (
        echo 依赖安装失败，试试换国内源后重跑：
        echo    ".venv\Scripts\python.exe" -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
        pause
        exit /b 1
    )
)

if not exist ".env" (
    copy /Y ".env.example" ".env" >nul
    echo [首次运行] 已生成 .env；打开控制台后填模型 Key / QQ 小号即可。
)

".venv\Scripts\python.exe" tools\one_click.py
if errorlevel 1 pause
