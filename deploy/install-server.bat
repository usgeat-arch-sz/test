@echo off
rem Installs Quote Calculator as an always-on service on this Windows server.
rem Right-click this file -> "Run as administrator". QuoteCalculator.exe must be in the same folder.
setlocal
cd /d "%~dp0"
set PORT=8765

net session >nul 2>&1
if errorlevel 1 (
  echo.
  echo This needs administrator rights.
  echo Close this window, right-click install-server.bat and choose "Run as administrator".
  pause
  exit /b 1
)
if not exist "%~dp0QuoteCalculator.exe" (
  echo.
  echo QuoteCalculator.exe was not found next to this file. Extract the whole zip first.
  pause
  exit /b 1
)

echo Opening firewall port %PORT% ...
netsh advfirewall firewall delete rule name="QuoteCalculator" >nul 2>&1
netsh advfirewall firewall add rule name="QuoteCalculator" dir=in action=allow protocol=TCP localport=%PORT% >nul

echo Creating the start-at-boot task ...
schtasks /Delete /TN "QuoteCalculator" /F >nul 2>&1
schtasks /Create /TN "QuoteCalculator" /SC ONSTART /RU SYSTEM /RL HIGHEST /F /TR "\"%~dp0QuoteCalculator.exe\" --host 0.0.0.0 --port %PORT% --no-browser --data-dir \"%~dp0QuoteData\""
if errorlevel 1 (
  echo.
  echo FAILED to create the task.
  pause
  exit /b 1
)
schtasks /Run /TN "QuoteCalculator" >nul

echo.
echo Done. The program is running and will start again after every reboot.
echo   Data folder : %~dp0QuoteData
echo   On THIS server open  http://127.0.0.1:%PORT%/  to create the administrator account.
echo   Colleagues open      http://THIS-SERVER-IP:%PORT%/   (find the IP with the command: ipconfig)
echo.
pause
