@echo off
rem Starts the Beacon CLIENT: IntelAvatar on the Beacon review page (your own IPS cases).
rem The exe name carries the build version, so pick whichever IntelAvatar_Client_v*.exe sits next to this file.
setlocal
set "EXE="
for %%f in ("%~dp0IntelAvatar_Client_v*.exe") do set "EXE=%%~ff"
if "%EXE%"=="" (
  echo No IntelAvatar_Client_v*.exe found next to this file.
  pause
  exit /b 1
)
start "" "%EXE%" --handsfree --handsfree-role client
endlocal
