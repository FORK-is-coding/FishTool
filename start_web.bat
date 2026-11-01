@echo off
setlocal EnableExtensions
rem Keep everything above "chcp 65001" ASCII-only: cmd decodes each line with the
rem current code page, so non-ASCII text is only safe after switching to UTF-8.
rem Remember the original code page (last word of the localized "chcp" output).
set "_FT_CP="
for /f "tokens=*" %%a in ('chcp') do for %%b in (%%a) do set "_FT_CP=%%b"
if defined _FT_CP set "_FT_CP=%_FT_CP:.=%"
chcp 65001 >nul

rem ============================================================
rem  FishTool one-click launcher for the Web UI (no desktop window).
rem
rem  Usage: double-click this file, or run  start_web.bat [port]
rem         e.g.  start_web.bat 9000      (default 8000)
rem
rem  Steps:
rem    1. If FishTool already answers /health on the port (e.g. the
rem       desktop app is running), just open the browser.
rem    2. Look for Python 3.11/3.12 in this order: .venv, venv, python
rem       on PATH, py -3.12, py -3.11. Prefer one with the project deps
rem       installed; otherwise offer to run pip install -r requirements.txt.
rem    3. Poll /health in the background and open http://localhost:PORT
rem       once the server is ready.
rem    4. Run "python app.py web" in the foreground (127.0.0.1 only).
rem       Close this window or press Ctrl+C to stop the server.
rem
rem  Environment variables (for scripts/tests, not needed normally):
rem    FISHTOOL_NO_BROWSER=1      do not open the browser
rem    FISHTOOL_NONINTERACTIVE=1  never prompt or pause; fail fast on missing deps
rem
rem  Keep every "rem" line ASCII-only: under code page 65001 cmd can
rem  misparse non-ASCII comment lines and run fragments as commands.
rem ============================================================

title FishTool Web
cd /d "%~dp0"
set "RC=0"

if not exist "app.py" (
    echo [错误] 找不到 app.py。请把 start_web.bat 放在 FishTool 项目根目录下运行。
    set "RC=1"
    goto :end
)

rem ---------- port argument ----------
set "PORT=8000"
if not "%~1"=="" set "PORT=%~1"
call :validate_port
if errorlevel 1 (
    echo [错误] 端口必须是 1-65535 之间的整数，收到："%PORT%"
    echo        用法：start_web.bat [端口]，例如 start_web.bat 9000
    set "RC=2"
    goto :end
)

set "URL=http://localhost:%PORT%"
set "HEALTH=http://127.0.0.1:%PORT%/health"

rem ---------- already running: just open the browser ----------
call :health_ok
if not errorlevel 1 (
    echo [提示] 端口 %PORT% 上已有 FishTool 在运行，直接打开 %URL%
    call :open_browser_now
    if not "%FISHTOOL_NONINTERACTIVE%"=="1" timeout /t 3 >nul
    goto :end
)

rem ---------- port taken by some other program ----------
call :port_free
if errorlevel 1 (
    echo [错误] 端口 %PORT% 已被其他程序占用（不是 FishTool）。
    echo        换一个端口启动，例如：start_web.bat 9000
    set "RC=1"
    goto :end
)

rem ---------- find Python ----------
set "PY="
set "PY_NODEPS="
if exist ".venv\Scripts\python.exe" call :probe "%CD%\.venv\Scripts\python.exe"
if exist "venv\Scripts\python.exe" call :probe "%CD%\venv\Scripts\python.exe"
call :probe python
call :probe py -3.12
call :probe py -3.11

if not defined PY if defined PY_NODEPS goto :install_deps
if not defined PY (
    echo [错误] 没找到 Python 3.11 或 3.12。
    echo        请先安装 Python 3.12：https://www.python.org/downloads/
    echo        安装时勾选 "Add python.exe to PATH"，装好后重新双击本文件。
    set "RC=1"
    goto :end
)
goto :run

:install_deps
echo [提示] 找到了 Python，但还没装项目依赖：
%PY_NODEPS% -c "import sys; print('       ' + sys.executable)"
if "%FISHTOOL_NONINTERACTIVE%"=="1" (
    echo [错误] 请先运行：pip install -r requirements.txt
    set "RC=1"
    goto :end
)
choice /c YN /m "是否现在安装依赖（pip install -r requirements.txt）"
if errorlevel 2 (
    echo [提示] 已取消。手动安装：pip install -r requirements.txt
    set "RC=1"
    goto :end
)
%PY_NODEPS% -m pip install -r requirements.txt
if errorlevel 1 (
    echo [错误] 依赖安装失败，请查看上面的 pip 输出。
    set "RC=1"
    goto :end
)
set "PY=%PY_NODEPS%"

rem ---------- start ----------
:run
echo [提示] 使用 Python：
%PY% -c "import sys; print('       ' + sys.executable + '  (' + sys.version.split()[0] + ')')"
echo [提示] 正在启动 Web 服务，就绪后会自动打开 %URL%
echo        关闭本窗口或按 Ctrl+C 停止服务。
echo.
if not "%FISHTOOL_NO_BROWSER%"=="1" call :open_browser_when_ready
rem Match the UTF-8 code page when output is redirected (only affects stdio, not file I/O).
set "PYTHONIOENCODING=utf-8"
%PY% app.py web --host 127.0.0.1 --port %PORT%
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
    echo.
    echo [错误] Web 服务异常退出（退出码 %RC%），具体原因见上面的日志或 logs\ 目录。
    echo        常见原因：端口 %PORT% 被其他程序占用，可换端口：start_web.bat 9000
)
goto :end

rem ============================================================
rem  subroutines
rem ============================================================

:validate_port
rem Digits only, no leading zero, at most 5 digits, not above 65535.
if "%PORT%"=="" exit /b 1
for /f "delims=0123456789" %%x in ("%PORT%") do exit /b 1
if "%PORT:~0,1%"=="0" exit /b 1
if not "%PORT:~5%"=="" exit /b 1
if %PORT% GTR 65535 exit /b 1
exit /b 0

:probe
rem Usage: call :probe <python command...>
rem 3.11/3.12 with deps -> PY; right version but missing deps -> first PY_NODEPS.
if defined PY exit /b 0
%* -c "import sys; sys.exit(0 if (3, 11) <= sys.version_info[:2] <= (3, 12) else 1)" >nul 2>&1
if errorlevel 1 exit /b 0
%* -c "import fastapi, uvicorn, jinja2, yaml, sqlalchemy, aiohttp, cryptography, httpx, bs4, pydantic, qrcode, markdown2, pdfkit, requests" >nul 2>&1
rem No parenthesized block here: a path like "(x86)" would close it early.
if not errorlevel 1 goto :probe_ok
if not defined PY_NODEPS set "PY_NODEPS=%*"
exit /b 0
:probe_ok
set "PY=%*"
exit /b 0

:health_ok
powershell -NoProfile -Command "try { $r = Invoke-RestMethod -TimeoutSec 2 -Uri '%HEALTH%'; if ($r.status -eq 'ok') { exit 0 } } catch {}; exit 1" >nul 2>&1
exit /b %ERRORLEVEL%

:port_free
rem Try to bind 127.0.0.1:PORT (the address app.py web listens on); exit 0 if it is free.
powershell -NoProfile -Command "try { $l = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, %PORT%); $l.ExclusiveAddressUse = $true; $l.Start(); $l.Stop(); exit 0 } catch { exit 1 }" >nul 2>&1
exit /b %ERRORLEVEL%

:open_browser_now
if "%FISHTOOL_NO_BROWSER%"=="1" exit /b 0
start "" "%URL%"
exit /b 0

:open_browser_when_ready
rem Poll /health in the background (about 2 minutes max), then open the page.
rem The poller lives in this console and ends with it; if the server never comes up it times out silently.
start "" /b powershell -NoProfile -Command "for ($i = 0; $i -lt 120; $i++) { try { $r = Invoke-RestMethod -TimeoutSec 2 -Uri '%HEALTH%'; if ($r.status -eq 'ok') { Start-Process '%URL%'; exit 0 } } catch {}; Start-Sleep -Seconds 1 }"
exit /b 0

rem Keep the tail ASCII-only: the original code page is restored on the last line.
:end
if not "%RC%"=="0" if not "%FISHTOOL_NONINTERACTIVE%"=="1" (
    echo.
    pause
)
endlocal & chcp %_FT_CP% >nul & exit /b %RC%
