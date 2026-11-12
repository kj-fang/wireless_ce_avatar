@echo off
rem Starts the Beacon SERVER: IntelAvatar in the server role (scheduled analysis rounds, live view).
rem The exe name carries the build version, so pick whichever IntelAvatar_Server_v*.exe sits next to this file.
setlocal
set "EXE="
for %%f in ("%~dp0IntelAvatar_Server_v*.exe") do set "EXE=%%~ff"
if "%EXE%"=="" (
  echo No IntelAvatar_Server_v*.exe found next to this file.
  pause
  exit /b 1
)
start "" "%EXE%" --handsfree-role server
endlocal
