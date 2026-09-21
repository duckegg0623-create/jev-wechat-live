@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Jev 微信实时解读

echo ============================================
echo   Jev 微信实时解读
echo ============================================
echo.
echo   前提：
echo     1. 微信 4.x 正在运行
echo     2. 代理开着（端口要和 jev-chat\config.json 里的 proxy 一致）
echo     3. 密钥有效（失效就跑 wechat-decrypt 重新提取）
echo.
echo   浮层会自动贴在微信窗口旁边
echo   按 Ctrl+C 或点浮层右上角 ✕ 退出
echo.
pause

python live.py
pause
