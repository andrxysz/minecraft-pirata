param([string]$Iscc = "")
$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $projectRoot
try {
    python -m unittest discover -s tests -v
    if ($LASTEXITCODE -ne 0) { throw "Os testes falharam." }
    python build_support/generate_icon.py
    if ($LASTEXITCODE -ne 0) { throw "Não foi possível gerar o ícone." }
    $taskIcon = Join-Path $projectRoot 'build_support\MinePirata.ico'
    $taskVersion = Join-Path $projectRoot 'build_support\version_info.txt'
    python -m PyInstaller --noconfirm --clean --onedir --windowed --noupx --name MinePirata --icon $taskIcon --version-file $taskVersion --specpath build_support --distpath dist --workpath build/pyinstaller launcher.py
    if ($LASTEXITCODE -ne 0) { throw "O empacotamento falhou." }
    if (-not $Iscc) {
        $compiler = Get-Command ISCC.exe -ErrorAction SilentlyContinue
        if ($compiler) { $Iscc = $compiler.Source }
        else {
            $locations = @(
                (Join-Path $projectRoot '.build-tools\inno\ISCC.exe'),
                'C:\Program Files (x86)\Inno Setup 7\ISCC.exe',
                'C:\Program Files\Inno Setup 7\ISCC.exe',
                'C:\Program Files (x86)\Inno Setup 6\ISCC.exe'
            )
            $Iscc = $locations | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
        }
    }
    if (-not $Iscc) { throw "Instale o Inno Setup e informe -Iscc com o caminho do ISCC.exe." }
    & $Iscc /Q build_support/MinePirata.iss
    if ($LASTEXITCODE -ne 0) { throw "A compilação do instalador falhou." }
    Get-Item -LiteralPath MinePirata-Setup-1.0.exe | Select-Object FullName, Length
} finally { Pop-Location }
