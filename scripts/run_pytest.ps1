param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$PytestArgs
)

$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $PSScriptRoot
$sitePackages = Join-Path $projectRoot '.venv\Lib\site-packages'
$fallbackPython = 'C:\Program Files\PostgreSQL\17\pgAdmin 4\python\python.exe'

if (-not (Test-Path $sitePackages)) {
    throw "Не найден каталог site-packages: $sitePackages"
}
if (-not (Test-Path $fallbackPython)) {
    throw "Не найден fallback python: $fallbackPython"
}

if (-not $PytestArgs -or $PytestArgs.Count -eq 0) {
    $PytestArgs = @('guests/tests/test_focus_categories_workbench_view.py')
}

# Отключаем cacheprovider, чтобы не упираться в права на .pytest_cache.
$pytestCall = @(
    'import sys'
    "sys.path.insert(0, r'$projectRoot')"
    "sys.path.insert(0, r'$sitePackages')"
    'import pytest'
)

$escapedArgs = ($PytestArgs | ForEach-Object { $_.Replace("'", "''") })
$argsLiteral = $escapedArgs | ForEach-Object { "'$_'" }
$pytestCall += "raise SystemExit(pytest.main(['-p', 'no:cacheprovider', $($argsLiteral -join ', ')]))"
$inlineCode = $pytestCall -join '; '

& $fallbackPython -c $inlineCode
$exitCode = $LASTEXITCODE
exit $exitCode
