$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Push-Location $projectRoot
try {
    if (-not (Test-Path -LiteralPath '.build-venv/Scripts/python.exe')) {
        python -m venv .build-venv
        if ($LASTEXITCODE -ne 0) { throw 'Falha ao preparar o ambiente de compilação.' }
    }
    $buildPython = Join-Path $projectRoot '.build-venv/Scripts/python.exe'
    & $buildPython -m pip install -r requirements-windows-build.txt
    if ($LASTEXITCODE -ne 0) { throw 'Falha ao instalar as bibliotecas.' }
    & $buildPython scripts/make_exe_icon.py
    if ($LASTEXITCODE -ne 0) { throw 'Falha ao gerar o ícone.' }
    & $buildPython scripts/collect_package_licenses.py
    if ($LASTEXITCODE -ne 0) { throw 'Falha ao reunir os avisos das dependências.' }
    & $buildPython -m PyInstaller --noconfirm Apolo.spec
    if ($LASTEXITCODE -ne 0) { throw 'Falha ao gerar o executável.' }
    Write-Host 'Pronto: dist/Apolo/Apolo.exe. Distribua a pasta Apolo inteira.'
} finally {
    Pop-Location
}
