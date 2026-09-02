@echo off
chcp 65001
cd /d "%~dp0"

:start_service
cls
echo [%date% %time%] 正在启动钉钉审批流监听服务...
echo ------------------------------------------------
:: 启动 Python 脚本
python main.py stream

:: ==============================================
:: 下面的代码只有在 python 脚本退出(崩溃/关闭)后才会执行
:: ==============================================

echo.
echo [%date% %time%] 检测到程序已退出！
echo 正在准备重启服务...

:: 等待 5 秒再重启，防止因为错误导致 CPU 疯狂空转
timeout /t 5 >nul

:: 跳转回标签，重新启动
goto start_service