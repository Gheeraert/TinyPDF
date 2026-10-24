# Construit l'exécutable Windows de TinyPDF avec PyInstaller.
#
#   powershell -ExecutionPolicy Bypass -File build.ps1            -> dist\TinyPDF\TinyPDF.exe (démarrage rapide)
#   powershell -ExecutionPolicy Bypass -File build.ps1 -OneFile   -> dist\TinyPDF.exe (un seul fichier, démarrage plus lent)
#
# Un environnement virtuel dédié (.venv-build) est créé au premier lancement :
# l'exécutable ne contient ainsi que les dépendances strictement nécessaires.

param([switch]$OneFile)

# Pas de $ErrorActionPreference = "Stop" : sous PowerShell 5.1, les messages
# normaux écrits sur stderr par pip/PyInstaller deviendraient des erreurs fatales.
# Chaque étape vérifie plutôt $LASTEXITCODE.
Set-Location $PSScriptRoot

$venv = Join-Path $PSScriptRoot ".venv-build"
$py = Join-Path $venv "Scripts\python.exe"
if (-not (Test-Path $py)) {
    Write-Host "Création de l'environnement de construction..."
    python -m venv $venv
    if ($LASTEXITCODE -ne 0) { throw "Impossible de créer l'environnement virtuel." }
}

& $py -m pip install --quiet --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "Mise à jour de pip impossible." }
& $py -m pip install --quiet -r requirements.txt pyinstaller
if ($LASTEXITCODE -ne 0) { throw "Installation des dépendances impossible." }

Write-Host "Vérification (tests)..."
& $py -m unittest discover -s tests
if ($LASTEXITCODE -ne 0) { throw "Les tests ont échoué : construction interrompue." }

New-Item -ItemType Directory -Force assets | Out-Null
& $py -m tinypdf.ui.icon assets\tinypdf.ico | Out-Null

$env:TINYPDF_ONEFILE = if ($OneFile) { "1" } else { "0" }
& $py -m PyInstaller --noconfirm --clean TinyPDF.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller a échoué." }

$exe = if ($OneFile) { "dist\TinyPDF.exe" } else { "dist\TinyPDF\TinyPDF.exe" }
Write-Host ""
Write-Host "Exécutable prêt : $exe"
