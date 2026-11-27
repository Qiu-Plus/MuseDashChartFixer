@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist "portable\MuseDashChartFixer.exe" (
  echo 正在启动 Muse Dash 谱面急救箱（文件夹版，启动最快）...
  start "" "portable\MuseDashChartFixer.exe"
  exit /b 0
)
if exist "MuseDashChartFixer_单文件版.exe" (
  echo 正在启动 Muse Dash 谱面急救箱（单文件版首次启动需 5~20 秒，请稍候）...
  start "" "MuseDashChartFixer_单文件版.exe"
  exit /b 0
)
if exist "MuseDashChartFixer.exe" (
  echo 正在启动 Muse Dash 谱面急救箱...
  start "" "MuseDashChartFixer.exe"
  exit /b 0
)
echo 未找到程序文件，请确认压缩包已完整解压。
pause
exit /b 1