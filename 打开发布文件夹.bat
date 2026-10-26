@echo off
setlocal
set "ZIP=%~dp0dist\gudum-v1.0.0-windows-x64.zip"

if not exist "%ZIP%" (
    echo [错误] 未找到发布包：
    echo   %ZIP%
    echo 请先运行 tools\build_release.py 重新打包。
    pause
    exit /b 1
)

echo 正在打开发布文件夹并选中压缩包...
explorer /select,"%ZIP%"
endlocal
