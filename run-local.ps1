$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = "E:\codex_work\.tr-venv\Scripts\python.exe"
$Sources = Join-Path (Split-Path -Parent $Root) "folo_sources.json"

if (-not (Test-Path -LiteralPath $Python)) {
    throw "Python environment not found: $Python"
}

$env:AI_API_KEY = [Environment]::GetEnvironmentVariable("AI_API_KEY", "User")
if ([string]::IsNullOrWhiteSpace($env:AI_API_KEY)) {
    throw "AI_API_KEY user environment variable is missing."
}
$env:AI_API_BASE = "https://jizhiapi.site/v1"
$env:AI_MODEL = "gpt-5.4-mini"

& $Python (Join-Path $Root "build_translated_rss.py") --sources $Sources
exit $LASTEXITCODE
