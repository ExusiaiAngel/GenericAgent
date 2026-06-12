# ══════════════════════════════════════════════════════════════════════════════
#  GenericAgent — Windows environment setup (PowerShell)
#  Run this before launching GenericAgent on Windows:
#    . .\env.ps1
#  Or create mykey_local.py (see mykey_local.py.example) for persistent config.
# ══════════════════════════════════════════════════════════════════════════════

# API Credentials (overrides mykey.py defaults)
$env:GENERICAGENT_PROXY = 'http://127.0.0.1:1080'

Write-Host "GenericAgent environment loaded (Windows)" -ForegroundColor Green
Write-Host "  Root: $((Get-Item .).FullName)" -ForegroundColor Cyan
Write-Host "  To set DEEPSEEK_API_KEY, create mykey_local.py from mykey_local.py.example" -ForegroundColor Yellow
