@echo off
chcp 65001
cd /d "%~dp0"
echo 正在启动钉钉审批流监听服务...
echo 按 Ctrl+C 可以停止服务
python main.py stream
if errorlevel 1 (
    echo.
    echo 程序异常退出，请检查错误信息。
    pause
)
