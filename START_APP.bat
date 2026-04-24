@echo off
echo ============================================================
echo   MQL Lead Router - Starting up...
echo ============================================================
echo.
echo Remote access URL (permanent, share with coworkers):
echo.
echo   https://scarring-bullfrog-faster.ngrok-free.dev
echo.
echo Coworkers can open that link from ANY location, no VPN needed.
echo ============================================================
echo   Local access: http://localhost:5000
echo ============================================================
echo.
echo Press Ctrl+C in THIS window to stop the app.
echo.
start "ngrok tunnel" ngrok http --domain=scarring-bullfrog-faster.ngrok-free.dev 5000
timeout /t 3 /nobreak >nul
cd /d "%~dp0webapp"
python app.py
pause
