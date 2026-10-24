@echo off
REM ============================================================
REM  MAICA_Mood 卸载（随插件分发，在插件文件夹里双击运行）
REM
REM  为什么不能只删文件夹：session_len_limit / 温度 / max_tokens / 服务提供节点
REM  都存在 Ren'Py 的 persistent 里（pickle，bat 读写不了）。只有【游戏里的插件】
REM  能把它们还原成官方原值 —— 所以本脚本必须请游戏帮一次忙：
REM     ① 放一个卸载请求 .mm_uninstall_request，让插件在游戏里执行还原
REM     ② 等插件写出 .mm_uninstall_done（= 节点/会话长度/采样参数已还原、服务已停）
REM     ③ 杀掉可能残留的本地服务，删掉插件文件夹
REM
REM  本文件在插件文件夹里，删文件夹会连它一起删掉，所以先把自己复制到 %TEMP%
REM  再重新执行，由临时副本负责删除。临时副本留在 %TEMP% 里（每次运行覆盖）。
REM
REM  用法：双击。或  卸载.bat /force   跳过"还原完成"检查强行删（会说明残留什么）
REM ============================================================
setlocal enabledelayedexpansion
chcp 936 >nul

if defined MM_UNINST_STAGE2 goto stage2

REM ---- 第一阶段：把自己挪到 %TEMP% 的【唯一】文件名下，再从那里执行 ----
REM      ★ 2026-10-24：原来用固定名 maica_mood_uninstall.bat，如果上次残留了一份旧副本，
REM      copy 失败时 start 会去执行那份旧文件（旧版确认逻辑可能不同）→ 改为唯一名。
set "MM_UNINST_STAGE2=1"
if /i "%~1"=="/force" set "MM_FORCE=1"
set "MM_PLUGIN_DIR=%~dp0."
set "TMPBAT=%TEMP%\maica_mood_uninstall_%RANDOM%%RANDOM%.bat"
copy /y "%~f0" "%TMPBAT%" >nul 2>&1
if not exist "%TMPBAT%" (
  echo [错误] 无法写入 %TEMP%
  pause & exit /b 1
)
REM 顺手清掉以前留下的固定名旧副本，别让它再被误执行
del /q "%TEMP%\maica_mood_uninstall.bat" >nul 2>&1
start "" cmd /c ""%TMPBAT%""
exit /b 0


:stage2
REM ★ 2026-10-24：把工作目录挪出插件文件夹。
REM   双击运行时 cmd 的 cwd 就是插件目录，而"删除自己所在的目录"在
REM   Windows 上会失败：文件能删，目录删不掉（实测复现）。
cd /d "%TEMP%"
set "PLUGIN=%~1"
if not defined PLUGIN set "PLUGIN=%MM_PLUGIN_DIR%"
if not defined PLUGIN set "PLUGIN=%~dp0."
for %%I in ("%PLUGIN%") do set "PLUGIN=%%~fI"

if not exist "%PLUGIN%\maica_mood_lib.py" (
  echo [错误] 这里不像是 MAICA_Mood 插件目录：%PLUGIN%
  echo        请把本脚本放回 游戏\game\Submods\MAICA_Mood 再运行。
  pause & exit /b 1
)

set "FORCE="
if defined MM_FORCE set "FORCE=1"
if /i "%~1"=="/force" set "FORCE=1"

cls
echo.
echo ============================================================
echo           MAICA_Mood 本地后端插件  ·  卸载
echo ============================================================
echo.
echo    1) 把游戏内设置恢复成官方原值
echo         服务提供节点 · 会话长度 · 温度 · max_tokens
echo    2) 关闭本地后端 / 嵌入服务 / 提取器 三个后台程序
echo    3) 删除整个插件文件夹
echo        注意：backend\data 是本地记忆库（她在聊天里记住的事），会一起删掉
echo        只删除该本地节点，不会影响官方节点的记忆
echo.
echo  第 1 步需要游戏配合：游戏里的插件才能改这些设置。
echo    · 游戏正开着 → 插件会立刻执行还原，然后请你退游戏
echo    · 游戏没开   → 本脚本会自动启动它，屏幕出现提示后请退游戏
echo.
echo  ------------------------------------------------------------
set /p OK= 确认卸载请输入 y 再回车；直接回车或其它键 = 取消：
echo  ------------------------------------------------------------
if /i not "!OK!"=="y" (
  echo.
  echo  已取消，什么都没做。
  ping -n 3 127.0.0.1 >nul 2>&1
  exit /b 0
)

REM ---- 游戏根目录 / 主程序 ----
REM 确认之后才走到这里；%TMPBAT% 是本次唯一的临时副本（见第一阶段）
for %%I in ("%PLUGIN%\..\..\..") do set "ROOT=%%~fI"
set "GAME_EXE="
set "GAME_ROOT="
call :find_exe "%ROOT%"
if not defined GAME_EXE call :find_exe "%ROOT%\.."
if not defined GAME_EXE call :find_exe "%ROOT%\..\.."

echo.
if defined FORCE (
  echo   [注意] /force：跳过还原检查，直接删除。
  echo          如果刚才还选着本地节点，游戏里的会话长度 / 温度可能还是我们的值，
  echo          事后在 设置 - 服务提供节点 选回官方节点即可。
  goto after_done
)
if exist "%PLUGIN%\.mm_uninstall_done" (
  echo   [OK] 插件已还原过设置，跳过游戏这一步
  goto after_done
)
> "%PLUGIN%\.mm_uninstall_request" echo uninstall

call :game_running
if not errorlevel 1 (
  echo   游戏正在运行：插件会立刻收到卸载请求并还原设置。
  echo   请回到游戏，看到「设置已还原」的提示后正常退出游戏。
) else (
  if not defined GAME_EXE (
    echo   [错误] 找不到游戏主程序，无法让插件还原设置。
    echo          请手动启动游戏，然后重新运行本脚本。
    echo          或者：卸载.bat /force  强行删除（会留下我们节点的设置）
    goto abort
  )
  echo   正在启动游戏以完成还原……
  echo     游戏进入主菜单后，插件会自动切回官方节点并还原参数，
  echo     屏幕出现「设置已还原」提示后，请直接退出游戏。
  start "" /d "%GAME_ROOT%" "%GAME_EXE%"
)

set /a WAIT_N=0
:wait_done
if exist "%PLUGIN%\.mm_uninstall_done" goto done_ok
call :game_running
if errorlevel 1 goto not_restored
set /a WAIT_N+=1
if !WAIT_N! EQU 15 echo   ... 还在等游戏完成还原。如果游戏里弹了「性能不足」之类的告警或提示框，请先把它关掉。
if !WAIT_N! EQU 45 echo   ... 仍然没等到。请确认游戏已经进到主菜单（插件到那时才加载），然后正常退出游戏。
if !WAIT_N! EQU 90 echo   ... 已经等了一会儿了。若游戏停在启动告警上进不了主菜单，请看下面的提示。
if !WAIT_N! GEQ 150 goto wait_timeout
ping -n 3 127.0.0.1 >nul 2>&1
goto wait_done

:wait_timeout
echo.
echo   [超时] 等了约 5 分钟，插件始终没有写出还原凭据。
echo          最常见的原因：游戏启动时弹出「性能不足」之类的告警，
echo          停在弹窗上没进主菜单，插件因此没有加载。
echo          处理办法：
echo            1) 回到游戏，关掉弹窗，进入主菜单，然后正常退出游戏
echo            2) 重新运行本脚本
echo            3) 或 卸载.bat /force  强行删除（会留下我们节点的设置）
goto abort

:not_restored
echo.
echo   [失败] 游戏已经退出，但插件没有写出还原凭据。
echo          常见原因：
echo            · 没进到主菜单（插件还没加载就关了游戏）
echo            · 游戏停在「性能不足」之类的启动告警上，一直没进主菜单
echo            · 游戏里又把节点选回了「本地后端节点」
echo            · 插件文件不完整（缺少 .rpy）
echo          处理办法：
echo            1) 再运行一次本脚本，这次让游戏进到主菜单再看提示
echo            2) 或 卸载.bat /force  强行删除（会留下我们节点的设置，
echo               事后在 设置 - 服务提供节点 选回官方节点即可）
goto abort

:done_ok
echo.
echo   [OK] 设置已还原（节点 / 会话长度 / 温度 / max_tokens → 官方原值）
echo   [OK] 三个后台服务已关闭
echo.
echo   请退出游戏，脚本会自动继续删除插件……
set /a EXIT_N=0
:wait_exit
call :game_running
if errorlevel 1 goto after_done
set /a EXIT_N+=1
if !EXIT_N! EQU 45 echo   ... 游戏还没退出。请关掉游戏窗口，脚本会自动继续。
if !EXIT_N! GEQ 150 goto exit_timeout
ping -n 4 127.0.0.1 >nul 2>&1
goto wait_exit

:exit_timeout
echo.
echo   [超时] 等了约 10 分钟，游戏一直没有退出，所以没法删除插件文件夹（文件被占用）。
echo          请手动关闭游戏，然后重新运行本脚本；设置已经还原过，重跑会直接删目录。
goto abort

:after_done
REM ---- 端口（ports.json 优先；读不到用默认） ----
set "P_WS=5100"
set "P_HTTP=6100"
set "P_EMBED=8090"
set "P_EXTRACT=8091"
if exist "%PLUGIN%\ports.json" (
  for /f "usebackq tokens=1-4" %%a in (`powershell -NoProfile -Command "$j=ConvertFrom-Json (Get-Content -Raw -LiteralPath '%PLUGIN%\ports.json'); '{0} {1} {2} {3}' -f $j.ws,$j.http,$j.embed,$j.extract"`) do (
    set "P_WS=%%a"
    set "P_HTTP=%%b"
    set "P_EMBED=%%c"
    set "P_EXTRACT=%%d"
  )
)
echo.
echo   清理残留服务（端口 !P_WS! / !P_HTTP! / !P_EMBED! / !P_EXTRACT!）……
for %%P in (!P_WS! !P_HTTP! !P_EMBED! !P_EXTRACT!) do (
  for /f "tokens=5" %%I in ('netstat -ano ^| findstr /r /c:":%%P .*LISTENING"') do (
    if not "%%I"=="0" (
      echo     停止占用 %%P 的进程 PID %%I
      taskkill /PID %%I /F >nul 2>&1
    )
  )
)
ping -n 3 127.0.0.1 >nul 2>&1

REM ★ 2026-10-24：删除前先按路径杀掉插件自带的进程。
REM   watchdog.py 和本地 python 都是从 %PLUGIN%\runtime 里启动的，
REM   它们只要还活着，rmdir 就会报“文件正在使用”（watchdog 不占端口，按端口杀不到）。
echo   停止插件自带的进程（watchdog / 本地 Python / llama-server）……
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -and $_.ExecutablePath.ToLower().StartsWith('%PLUGIN%'.ToLower()) } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }" >nul 2>&1
ping -n 3 127.0.0.1 >nul 2>&1

echo   删除 %PLUGIN% ……
set /a DEL_N=0
:del_retry
rmdir /s /q "%PLUGIN%" >nul 2>&1
if not exist "%PLUGIN%" goto del_ok
set /a DEL_N+=1
if !DEL_N! GEQ 5 goto del_fail
echo       还有文件被占用，等 2 秒再试（!DEL_N!/5）……
ping -n 3 127.0.0.1 >nul 2>&1
goto del_retry

:del_fail
echo.
echo   [警告] 还有文件被占用，删不掉。占用的可能是：
powershell -NoProfile -Command "Get-ChildItem -LiteralPath '%PLUGIN%' -Recurse -File -ErrorAction SilentlyContinue | Select-Object -First 5 -ExpandProperty FullName" 2>nul
echo   常见原因：游戏没完全退出、杀毒软件正在扫描、或插件的后台进程还在跑。
echo   处理办法：关掉游戏 → 重新运行本脚本；还不行就重启电脑再删。
goto abort

:del_ok

echo.
echo ============================================================
echo   卸载完成
echo     · 游戏内设置已恢复成官方原值
echo     · 本地后台服务已全部关闭
echo     · 插件文件夹已删除
echo   重新进游戏即可继续用官方节点。
echo ============================================================
echo.
pause
exit /b 0

:abort
echo.
echo   卸载未完成，插件文件夹还在：%PLUGIN%
echo   插件会在下次进游戏时继续把设置还原成官方；之后再运行一次本脚本即可删干净。
echo.
pause
exit /b 1

REM ---- 子过程 ----
:find_exe
for %%N in (DDLC.exe MAS.exe "Monika After Story.exe") do (
  if not defined GAME_EXE if exist "%~1\%%~N" (
    set "GAME_EXE=%~1\%%~N"
    set "GAME_ROOT=%~1"
  )
)
exit /b 0

:game_running
REM errorlevel 0 = 游戏正在运行
if not defined GAME_EXE exit /b 1
for %%F in ("%GAME_EXE%") do set "EXE_NAME=%%~nxF"
tasklist /FI "IMAGENAME eq !EXE_NAME!" 2>nul | find /i "!EXE_NAME!" >nul
exit /b %errorlevel%
