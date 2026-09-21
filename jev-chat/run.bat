@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Jev 即时解读

echo ============================================
echo   Jev 即时解读  -  输入对方的消息，回车出结果
echo   （需要 Clash 代理开着：127.0.0.1:7897）
echo ============================================
echo.

python jev_read.py -i

echo.
pause
