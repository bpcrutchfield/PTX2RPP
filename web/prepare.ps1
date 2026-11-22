# Run from any working directory: .\web\prepare.ps1
# Assumes this web folder is inside the PTX2RPP repository, beside ptx2rpp/.
$ErrorActionPreference = 'Stop'
$web = Split-Path -Parent $MyInvocation.MyCommand.Path
$repo = Split-Path -Parent $web
$source = Join-Path $repo 'ptx2rpp'
$dist = Join-Path $web 'dist'
if (-not (Test-Path (Join-Path $source 'converter.py'))) {
    throw "Cannot find $source\converter.py. Put the 'web' folder inside the PTX2RPP repository."
}
if (Test-Path $dist) { Remove-Item -Recurse -Force $dist }
New-Item -ItemType Directory -Path $dist | Out-Null
Copy-Item (Join-Path $web 'index.html') $dist
Copy-Item (Join-Path $web 'main.js') $dist
Copy-Item (Join-Path $web 'worker.js') $dist
Copy-Item (Join-Path $web 'styles.css') $dist
New-Item -ItemType Directory -Path (Join-Path $dist 'ptx2rpp') | Out-Null
Copy-Item (Join-Path $source '*.py') (Join-Path $dist 'ptx2rpp')
Write-Host 'PTX2RPP Web prototype prepared:' -ForegroundColor Green
Write-Host "  $dist"
Write-Host 'Start a local web server from the repository folder:'
Write-Host '  python -m http.server 8000 --directory .\web\dist'
Write-Host 'Then visit http://localhost:8000'
