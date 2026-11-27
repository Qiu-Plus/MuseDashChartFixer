@echo off
chcp 65001 >nul 2>nul
setlocal enabledelayedexpansion
title Muse Dash Chart Fixer - Build

rem ============================================================
rem  MuseDashChartFixer 打包脚本（PyInstaller --onefile --windowed）
rem  用法：双击运行本脚本，或在命令行执行 build_exe.bat
rem  产物：dist\MuseDashChartFixer.exe
rem ============================================================

cd /d "%~dp0"
echo.
echo ============================================================
echo   Muse Dash 谱面急救箱 - 打包脚本
echo   工作目录：%CD%
echo ============================================================
echo.

echo [1/4] 检查 Python 环境 ...
where python >nul 2>nul
if errorlevel 1 goto :no_python
python --version
if errorlevel 1 goto :python_broken
if not exist "main.py" goto :no_main

echo.
echo [2/4] 检查 PyInstaller ...
python -m PyInstaller --version >nul 2>nul
if not errorlevel 1 goto :have_pyinstaller
echo 未检测到 PyInstaller，正在通过 pip 安装 ...
python -m pip install --upgrade pip >nul 2>nul
python -m pip install pyinstaller
if errorlevel 1 goto :install_failed
:have_pyinstaller
set "PYI_VERSION="
for /f "delims=" %%v in ('python -m PyInstaller --version 2^>nul') do set "PYI_VERSION=%%v"
echo PyInstaller 版本：!PYI_VERSION!

echo.
echo [3/4] 清理旧的打包产物 ...
if exist "build" rmdir /s /q "build"
if exist "dist\MuseDashChartFixer.exe" del /q "dist\MuseDashChartFixer.exe"
if exist "dist\MuseDashChartFixerCLI.exe" del /q "dist\MuseDashChartFixerCLI.exe"
if exist "dist_portable" rmdir /s /q "dist_portable"
if exist "MuseDashChartFixer.spec" del /q "MuseDashChartFixer.spec"
if exist "MuseDashChartFixerCLI.spec" del /q "MuseDashChartFixerCLI.spec"

echo.
echo [4/4] 开始打包（--onefile --windowed）...
python -m PyInstaller --onefile --windowed --name MuseDashChartFixer --noconfirm main.py
if errorlevel 1 goto :build_failed
python -m PyInstaller --onefile --console --name MuseDashChartFixerCLI --noconfirm main.py
if errorlevel 1 goto :build_failed
python -m PyInstaller --onedir --windowed --name MuseDashChartFixer --noconfirm --distpath dist_portable --workpath build\portable main.py
if errorlevel 1 goto :build_failed
if not exist "dist\MuseDashChartFixer.exe" goto :exe_missing
if not exist "dist\MuseDashChartFixerCLI.exe" goto :exe_missing
if not exist "dist_portable\MuseDashChartFixer\MuseDashChartFixer.exe" goto :exe_missing

echo.
echo ============================================================
echo   打包成功！
echo   产物：%CD%\dist\MuseDashChartFixer.exe（图形界面单文件版）
echo         %CD%\dist\MuseDashChartFixerCLI.exe（命令行）
echo         %CD%\dist_portable\MuseDashChartFixer\（图形界面文件夹版，启动更快）
echo ============================================================
echo.
echo 提示：dist\MuseDashChartFixer.exe 可直接双击运行（GUI 模式），
echo       也可在命令行执行：MuseDashChartFixer.exe --cli --help
echo.
pause
endlocal
exit /b 0

:no_python
echo [错误] 未找到 python 命令，请先安装 Python 3.10+ 并加入 PATH。
goto :failed

:python_broken
echo [错误] python 无法正常运行，请检查安装。
goto :failed

:no_main
echo [错误] 当前目录没有 main.py，请在 MuseDashChartFixer 项目目录中运行本脚本。
goto :failed

:install_failed
echo [错误] PyInstaller 安装失败，请检查网络，或手动执行：python -m pip install pyinstaller
goto :failed

:build_failed
echo [错误] PyInstaller 打包失败，请查看上面的日志。
goto :failed

:exe_missing
echo [错误] 打包命令结束，但没有找到 dist\MuseDashChartFixer.exe。
goto :failed

:failed
echo.
echo 打包未完成，请根据上面的错误信息处理后重试。
echo.
pause
endlocal
exit /b 1