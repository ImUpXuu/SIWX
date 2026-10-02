@echo off
chcp 65001 >nul 2>&1
title stories-in-wx 自动更新

echo ============================================
echo   stories-in-wx 自动更新
echo ============================================
echo.

set REPO=ImUpXuu/SIWX
set RAW=https://raw.gh.1s.fan/%REPO%/main

REM ── 1. 获取远程版本信息 ──────────────────────
echo [1/5] 检查新版本...
curl -fsSL "%RAW%/version.json" > "%TEMP%\siwx_version.json" 2>nul
if errorlevel 1 (
    echo [错误] 无法获取版本信息，请检查网络连接
    pause
    exit /b 1
)

for /f "tokens=*" %%i in ('python -c "import json; d=json.load(open(r'%TEMP%\siwx_version.json','r',encoding='utf-8')); print(d['version']) "') do set NEW_VER=%%i
echo       新版本: v%NEW_VER%

REM ── 2. 下载新产物 ──────────────────────────────
set ASSET_URL=https://github.com/%REPO%/releases/download/v%NEW_VER%/stories-in-wx-v%NEW_VER%-windows-x64.exe
set DEST=%~dp0..\stories-in-wx-v%NEW_VER%-windows-x64.exe

echo [2/5] 下载新产物...
curl -fsSL -L "%ASSET_URL%" -o "%DEST%" 2>nul
if errorlevel 1 (
    echo [错误] 下载失败，请检查网络或手动下载
    pause
    exit /b 1
)
echo       下载完成: %DEST%

REM ── 3. 校验 SHA-256 ────────────────────────────
echo [3/5] 校验文件完整性...
curl -fsSL "%RAW%/version.json" | python -c "import json,sys; d=json.load(sys.stdin); print(d.get('sha256',''))" > "%TEMP%\siwx_sha_url.txt"
set /p SHA_URL=<"%TEMP%\siwx_sha_url.txt"
curl -fsSL "%SHA_URL%" -o "%TEMP%\siwx_sha256.txt" 2>nul
for /f "tokens=1" %%h in ('certutil -hashfile "%DEST%" SHA256 ^| findstr /v "hash"') do set DL_HASH=%%h
findstr /i "%DL_HASH%" "%TEMP%\siwx_sha256.txt" >nul 2>&1
if errorlevel 1 (
    echo [警告] SHA-256 校验失败，文件可能损坏
    pause
    exit /b 1
)
echo       校验通过

REM ── 4. 替换旧版本 ──────────────────────────────
echo [4/5] 替换旧版本...
taskkill /f /im stories-in-wx*.exe /t 2>nul
timeout /t 2 /nobreak >nul

REM 备份旧版本
if exist "%~dp0..\stories-in-wx.exe" (
    copy /y "%~dp0..\stories-in-wx.exe" "%~dp0..\stories-in-wx.backup.exe" >nul 2>&1
)

REM 替换
copy /y "%DEST%" "%~dp0..\stories-in-wx.exe" >nul 2>&1
if errorlevel 1 (
    echo [错误] 替换失败，可能需要管理员权限
    pause
    exit /b 1
)
echo       替换完成

REM ── 5. 清理并启动 ──────────────────────────────
echo [5/5] 启动新版本...
del "%DEST%" 2>nul
del "%TEMP%\siwx_version.json" 2>nul
del "%TEMP%\siwx_sha256.txt" 2>nul
del "%TEMP%\siwx_sha_url.txt" 2>nul

echo.
echo ============================================
echo   更新完成！正在启动 stories-in-wx...
echo ============================================
start "" "%~dp0..\stories-in-wx.exe" serve
timeout /t 3 /nobreak >nul
exit /b 0
