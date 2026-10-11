function Resolve-ProjectPython {
    param([string]$Python)
    $projectDirectory = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
    if ($Python) {
        if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) { throw 'The supplied Python executable does not exist.' }
        return [IO.Path]::GetFullPath($Python)
    }
    $candidates = @(
        $env:XIAOYUN_PYTHON,
        (Join-Path $projectDirectory '.venv/Scripts/python.exe'),
        (Join-Path $projectDirectory '.venv/bin/python'),
        'D:\conda\envs_dirs\codex\Scripts\python.exe'
    )
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate -PathType Leaf)) { return $candidate }
    }
    throw 'Create the project .venv first, or pass -Python <path-to-python>.'
}
