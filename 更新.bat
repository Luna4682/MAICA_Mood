@echo off
REM ============================================================
REM  MAICA_Mood 更新（随插件分发，在插件文件夹里双击运行）
REM
REM  用法：
REM    ① 把更新包（.zip）放进插件文件夹，名字随意，双击本脚本
REM    ② 或者把 .zip 直接拖到本脚本上
REM    ③ 也可以带路径：更新.bat D:\下载\MAICA_Mood_1.0.2.zip
REM
REM  它会：备份记忆库 → 解压覆盖（不动 backend\data 和其它运行期文件）
REM        → 删掉旧的 .rpyc → 打印结果。更新完请重新启动游戏。
REM
REM  本文件可能被更新包替换，所以先复制到 %TEMP% 再从那里跑。
REM ============================================================
setlocal enabledelayedexpansion
chcp 936 >nul

if defined MM_UPDATE_STAGE2 goto stage2

set "MM_UPDATE_STAGE2=1"
if /i "%~x1"==".zip" (
  set "MM_ZIP=%~f1"
  set "MM_PLUGIN=%~dp0."
) else (
  set "MM_PLUGIN=%~dp0."
  set "MM_ZIP=%~1"
)
set "TMPBAT=%TEMP%\maica_mood_update_%RANDOM%%RANDOM%.bat"
copy /y "%~f0" "%TMPBAT%" >nul 2>&1
if not exist "%TMPBAT%" (
  echo [错误] 无法写入 %TEMP%
  pause & exit /b 1
)
del /q "%TEMP%\maica_mood_update.bat" >nul 2>&1
start "" cmd /c ""%TMPBAT%" "%MM_PLUGIN%" "%MM_ZIP%""
exit /b 0


:stage2
REM ★ 2026-10-24：把工作目录挪出插件文件夹。
REM   双击运行时 cmd 的 cwd 就是插件目录，而"删除自己所在的目录"在
REM   Windows 上会失败：文件能删，目录删不掉（实测复现）。
cd /d "%TEMP%"
set "PLUGIN=%~1"
if not defined PLUGIN set "PLUGIN=%~dp0."
for %%I in ("%PLUGIN%") do set "PLUGIN=%%~fI"
set "ZIP=%~2"

if not exist "%PLUGIN%\maica_mood_lib.py" (
  echo [错误] 这里不像是 MAICA_Mood 插件目录：%PLUGIN%
  echo        请把本脚本放回 游戏\game\Submods\MAICA_Mood 再运行。
  pause & exit /b 1
)
set "PY=%PLUGIN%\runtime\Scripts\python.exe"
REM dev hook: allow an external python (used by the automated test; double-click is unaffected)
if defined MM_UPDATE_PY if exist "%MM_UPDATE_PY%" set "PY=%MM_UPDATE_PY%"
if not exist "%PLUGIN%\updater.py" (
  echo [错误] 缺少 updater.py —— 插件目录不完整，或版本太老（请先手动解压完整包一次）。
  pause & exit /b 1
)
if not exist "%PY%" (
  echo [错误] 找不到插件自带的 python：%PY%
  echo        插件目录不完整，建议重新解压完整包。
  pause & exit /b 1
)

REM ---- 没给 zip 就在插件目录里找一个 ----
if defined ZIP if /i not "%~x2"==".zip" set "ZIP="
if not defined ZIP (
  for %%F in ("%PLUGIN%\*.zip") do (
    set "ZIP=%%~fF"
    goto got_zip
  )
)
:got_zip

cls
echo.
echo ============================================================
echo           MAICA_Mood 本地后端插件  ·  更新
echo ============================================================
echo.
if not defined ZIP (
  echo   [错误] 没找到更新包。
  echo.
  echo   把 .zip 放进这个文件夹再双击本脚本：
  echo       %PLUGIN%
  echo   或者把 .zip 直接拖到本脚本上。
  echo.
  pause & exit /b 1
)
echo   更新包：%ZIP%
echo   插件目录：%PLUGIN%
echo.
echo   更新会：先备份记忆库，再覆盖程序文件。
echo   backend\data（她的记忆）、ports.json、.env、日志都不会动。
echo   更新完请重新启动游戏。
echo.
echo  ------------------------------------------------------------
set /p OK= 确认更新请输入 y 再回车；直接回车或其它键 = 取消：
echo  ------------------------------------------------------------
if /i not "%OK%"=="y" (
  echo.
  echo  已取消，什么都没做。
  ping -n 3 127.0.0.1 >nul 2>&1
  exit /b 0
)

"%PY%" -X utf8 "%PLUGIN%\updater.py" --zip "%ZIP%" --root "%PLUGIN%"
set "RC=%ERRORLEVEL%"
echo.
echo  ------------------------------------------------------------
if "%RC%"=="0" (
  echo   [OK] 更新完成。
  echo        旧的 .rpyc 已清掉，重新启动游戏即生效。
) else if "%RC%"=="2" (
  echo   [停止] 游戏还在运行。请先退出游戏，再运行本脚本。
) else if "%RC%"=="3" (
  echo   [失败] 更新包有问题（损坏 / 不是插件更新包 / 下载失败）。
) else (
  echo   [失败] 覆盖出错（退出码 %RC%），部分文件可能没更新。
  echo          日志：%PLUGIN%\logs\update.log
)
echo  ------------------------------------------------------------
echo.
if exist "%PLUGIN%\logs\update.log" (
  echo   最近日志：
  powershell -NoProfile -Command "Get-Content -Encoding UTF8 -LiteralPath '%PLUGIN%\logs\update.log' -Tail 12" 2>nul
)
echo.
pause
exit /b 0
