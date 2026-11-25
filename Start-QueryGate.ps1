$launcher = Join-Path $PSScriptRoot 'start_querygate.py'

foreach ($candidate in @('py.exe', 'python.exe')) {
    $interpreter = Get-Command $candidate -CommandType Application -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if (-not $interpreter) {
        continue
    }

    $pythonArgs = @()
    if ($candidate -eq 'py.exe') {
        $pythonArgs = @('-3')
    }

    & $interpreter.Source @pythonArgs -c 'import sys; sys.exit(sys.version_info < (3, 11))' *> $null
    if ($LASTEXITCODE -eq 0) {
        & $interpreter.Source @pythonArgs $launcher @args
        exit $LASTEXITCODE
    }
}

[Console]::Error.WriteLine('Install Python 3.11 or newer and open a new terminal.')
exit 1
