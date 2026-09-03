@echo off
rem =====================================================================
rem  codex-deepseek.cmd - run the REAL codex CLI against the Bridge's
rem  dedicated DeepSeek CODEX_HOME (Desktop Codex keeps OpenAI).
rem
rem  * sets CODEX_HOME=%LOCALAPPDATA%\local-codex-bridge\codex-deepseek
rem    ONLY for this child process (setlocal scopes the change)
rem  * never touches %USERPROFILE%\.codex (Desktop), auth.json, state or
rem    history
rem  * never prints any key; the DeepSeek key is provided by the Bridge
rem    bootstrap (DPAPI-protected) only to the Bridge app-server process
rem =====================================================================
setlocal
if "%LOCALAPPDATA%"=="" (
    echo error: LOCALAPPDATA is not set; run this from a normal user session 1>&2
    exit /b 1
)
set "CODEX_HOME=%LOCALAPPDATA%\local-codex-bridge\codex-deepseek"
where codex >nul 2>nul
if errorlevel 1 (
    echo error: real codex CLI not found on PATH; install it first 1>&2
    exit /b 1
)
codex %*
exit /b %errorlevel%
