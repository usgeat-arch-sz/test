@echo off
rem Stops Quote Calculator and removes its start-at-boot task and firewall rule.
rem Your data folder (QuoteData) is NOT deleted.
net session >nul 2>&1
if errorlevel 1 (
  echo Run this file as administrator.
  pause
  exit /b 1
)
schtasks /End /TN "QuoteCalculator" >nul 2>&1
schtasks /Delete /TN "QuoteCalculator" /F >nul 2>&1
taskkill /IM QuoteCalculator.exe /F >nul 2>&1
netsh advfirewall firewall delete rule name="QuoteCalculator" >nul 2>&1
echo Removed. The QuoteData folder was kept.
pause
