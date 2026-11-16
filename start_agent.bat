@echo off
rem  omini AI qurilma agenti (companion) - Windows ishga tushirgichi.
rem  Birinchi ishga tushirishda quyidagi ikki qatorni o'zingizga moslang:
set "BOT_URL=https://omini-ai-bot.onrender.com"
set "ROOT=%USERPROFILE%"

cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
  echo Python topilmadi. python.org dan o'rnatib, 'Add python.exe to PATH' belgisini qo'ying.
  pause
  exit /b 1
)
python companion\agent.py --url "%BOT_URL%" --root "%ROOT%"
pause
