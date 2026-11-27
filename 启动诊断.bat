@echo off
chcp 65001 >nul
title Muse Dash 谱面急救箱 - 启动诊断
cd /d "%~dp0"
echo ================================================
echo  Muse Dash 谱面急救箱 - 启动诊断
echo ================================================
echo 当前目录：%CD%
echo.
echo [1/5] 解除文件锁定（Unblock）...
powershell -NoProfile -Command "Get-ChildItem -Recurse -Include *.exe,*.dll -ErrorAction SilentlyContinue | Unblock-File -ErrorAction SilentlyContinue" >nul 2>&1
echo 完成
echo.
echo [2/5] 检查文件是否完整：
if exist "portable\MuseDashChartFixer.exe" (echo   portable 文件夹版：存在) else (echo   portable 文件夹版：缺失！)
if exist "MuseDashChartFixer_单文件版.exe" (echo   单文件版：存在) else (echo   单文件版：缺失！)
echo.
echo [3/5] 当前是否已有进程在运行：
tasklist | findstr /i MuseDashChartFixer || echo   无
echo.
echo [4/5] 尝试启动文件夹版（若存在）...
if exist "portable\MuseDashChartFixer.exe" (
  start "" "portable\MuseDashChartFixer.exe"
) else (
  start "" "MuseDashChartFixer_单文件版.exe"
)
echo 等待 8 秒后检查进程...
timeout /t 8 /nobreak >nul
tasklist | findstr /i MuseDashChartFixer
if errorlevel 1 (
  echo.
  echo   进程未启动：可能被 360/电脑管家/火绒/Defender 拦截，或程序报错。
  echo   请把本文件夹加入安全软件白名单后重试。
) else (
  echo.
  echo   进程已启动。若仍看不到窗口，请检查任务栏，或按 Alt+Tab 切换窗口。
)
echo.
echo [5/5] 崩溃日志：
if exist "MuseDashChartFixer_crash.log" (
  type "MuseDashChartFixer_crash.log"
) else (
  echo   暂无（没有崩溃）
)
echo.
echo 诊断结束。请把本窗口截图发给作者。
pause