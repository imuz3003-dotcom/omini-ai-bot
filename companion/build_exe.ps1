# omini-agent.exe — Python o'rnatilmagan kompyuterda ham ishlaydigan bitta fayl.
#
# Ishga tushirish (repositoriyning istalgan joyidan):
#   powershell -ExecutionPolicy Bypass -File companion\build_exe.ps1
# Natija: dist\omini-agent.exe — uni mijozga beramiz, ikki marta bosiladi.
#
# `--noupx` ataylab: UPX qatlami ba'zi antiviruslarda shubha uyg'otadi.
# `--paths companion` + `--hidden-import`: exe'laninganda `agent.py` paket ichida
# bo'lmaydi, shuning uchun uning zaxira yo'li (`import ops`/`import protocol`)
# ishishi uchun bu ikki modul alohida o'raladi.
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

python -m pip install --upgrade pyinstaller
python -m PyInstaller --noconfirm --clean --onefile --console --noupx `
    --paths companion --hidden-import ops --hidden-import protocol `
    --name omini-agent companion\agent.py

Write-Host ">> Tayyor: $((Get-Location).Path)\dist\omini-agent.exe"
