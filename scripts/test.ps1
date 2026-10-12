param([string]$Python = '')
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'python-runtime.ps1')
$pythonExecutable = Resolve-ProjectPython $Python
$projectDirectory = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$runDirectory = Join-Path $projectDirectory ('.local/tests/' + [guid]::NewGuid().ToString('N'))
if (-not ([IO.Path]::GetFullPath($runDirectory).StartsWith($projectDirectory + [IO.Path]::DirectorySeparatorChar))) { throw 'Invalid test directory' }
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $runDirectory) | Out-Null
Push-Location $projectDirectory
try {
    & $pythonExecutable -m pytest -q --basetemp=$runDirectory --tb=short
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    node --test tests/browser.test.mjs tests/frontend.test.mjs
    exit $LASTEXITCODE
} finally { Pop-Location }
