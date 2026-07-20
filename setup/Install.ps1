# WiKiVibe ExportHelper - one-click installer (Windows)
# Prefer: double-click START_HERE.bat at repo root.

param(
    [switch]$Silent,
    [switch]$SkipResolveScript,
    [switch]$SkipShortcut
)

$ErrorActionPreference = "Stop"

# Naming (brand / product)
$BrandName = "WiKiVibe"
$ProductName = "ExportHelper"
$AppName = "$BrandName $ProductName"                 # WiKiVibe ExportHelper
$ResolveScriptName = "${BrandName}_${ProductName}"   # WiKiVibe_ExportHelper
$InstallFolderName = "WiKiVibe_ExportHelper"         # under brand folder

$SetupDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$SourceRoot = Split-Path -Parent $SetupDir
$AppSource = Join-Path $SourceRoot "app"
$InstallRoot = Join-Path $env:LOCALAPPDATA "$BrandName\$InstallFolderName"
$StartMenuDir = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\$BrandName"
$DesktopShortcut = Join-Path ([Environment]::GetFolderPath("Desktop")) ($AppName + ".lnk")
$IconPath = Join-Path $InstallRoot "assets\app.ico"
$ResolveMenuRoot = Join-Path $env:APPDATA "Blackmagic Design\DaVinci Resolve\Support\Fusion\Scripts\Utility"

function Show-Msg([string]$Text, [string]$Title = $AppName) {
    if ($Silent) { Write-Host $Text; return }
    Add-Type -AssemblyName PresentationFramework | Out-Null
    [System.Windows.MessageBox]::Show($Text, $Title) | Out-Null
}

function Find-Python {
    $candidates = @(
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python313\python.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python311\python.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python310\python.exe"),
        "C:\Python311\python.exe",
        "C:\Python310\python.exe"
    )
    foreach ($p in $candidates) {
        if (Test-Path -LiteralPath $p) { return $p }
    }
    try {
        $py = & py -3 -c "import sys; print(sys.executable)" 2>$null
        if ($LASTEXITCODE -eq 0 -and $py) {
            $py = $py.Trim()
            if (Test-Path -LiteralPath $py) { return $py }
        }
    } catch {}
    return $null
}

function New-Shortcut([string]$LinkPath, [string]$TargetPath, [string]$Arguments, [string]$WorkDir, [string]$Icon) {
    $shell = New-Object -ComObject WScript.Shell
    $sc = $shell.CreateShortcut($LinkPath)
    $sc.TargetPath = $TargetPath
    $sc.Arguments = $Arguments
    $sc.WorkingDirectory = $WorkDir
    if ($Icon -and (Test-Path -LiteralPath $Icon)) {
        $sc.IconLocation = "$Icon,0"
    }
    $sc.Save()
}

Write-Host "=== $AppName installer ==="
Write-Host "Source: $SourceRoot"
Write-Host "App source: $AppSource"
Write-Host "Install to: $InstallRoot"

if (-not (Test-Path -LiteralPath (Join-Path $AppSource "export_helper.py"))) {
    Show-Msg "App files not found under app\. Please re-download the ZIP.`n`n找不到 app\export_helper.py，請重新下載解壓。"
    exit 2
}

$python = Find-Python
if (-not $python) {
    $msg = @"
Python 3.10+ was not found.

Please install Python from https://www.python.org/downloads/
(check Add python.exe to PATH), then run START_HERE.bat again.

找不到 Python 3.10+。
請先安裝 Python：https://www.python.org/downloads/
（建議勾選 Add python.exe to PATH），再重新執行 START_HERE.bat。
"@
    Show-Msg $msg
    exit 2
}
Write-Host "Python: $python"

# Install tree (flat for simple Resolve launcher paths)
New-Item -ItemType Directory -Force -Path $InstallRoot | Out-Null
Copy-Item -LiteralPath (Join-Path $AppSource "export_helper.py") -Destination (Join-Path $InstallRoot "export_helper.py") -Force
Copy-Item -LiteralPath (Join-Path $AppSource "config.json") -Destination (Join-Path $InstallRoot "config.json") -Force
$assetsSrc = Join-Path $AppSource "assets"
$assetsDst = Join-Path $InstallRoot "assets"
if (Test-Path -LiteralPath $assetsSrc) {
    if (Test-Path -LiteralPath $assetsDst) { Remove-Item -LiteralPath $assetsDst -Recurse -Force }
    Copy-Item -LiteralPath $assetsSrc -Destination $assetsDst -Recurse -Force
}

# Launcher stubs inside install folder
Copy-Item -LiteralPath (Join-Path $SetupDir "Launch.ps1") -Destination (Join-Path $InstallRoot "Launch.ps1") -Force
$launchBat = @"
@echo off
REM ASCII-only.
title $AppName
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0Launch.ps1" %*
exit /b %ERRORLEVEL%
"@
Set-Content -LiteralPath (Join-Path $InstallRoot "Launch.bat") -Value $launchBat -Encoding ASCII

# Docs / deps from source root
foreach ($item in @("requirements.txt", "README.md", "LICENSE")) {
    $src = Join-Path $SourceRoot $item
    if (Test-Path -LiteralPath $src) {
        Copy-Item -LiteralPath $src -Destination (Join-Path $InstallRoot $item) -Force
    }
}

Write-Host "Installing Python packages (PyQt6)..."
& $python -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) {
    Show-Msg "pip upgrade failed / pip 升級失敗"
    exit 3
}
$req = Join-Path $InstallRoot "requirements.txt"
& $python -m pip install -r $req
if ($LASTEXITCODE -ne 0) {
    Show-Msg "pip install failed. Try: python -m pip install PyQt6`n`npip 安裝失敗，可手動執行：python -m pip install PyQt6"
    exit 3
}

$pythonwCandidate = Join-Path (Split-Path $python) "pythonw.exe"
if (Test-Path -LiteralPath $pythonwCandidate) {
    $pythonw = $pythonwCandidate
} else {
    $pythonw = $python
}

if (-not $SkipShortcut) {
    New-Item -ItemType Directory -Force -Path $StartMenuDir | Out-Null
    $launchPs1 = Join-Path $InstallRoot "Launch.ps1"
    $psArgs = "-NoProfile -ExecutionPolicy Bypass -File `"$launchPs1`""
    $startLink = Join-Path $StartMenuDir ($AppName + ".lnk")
    New-Shortcut -LinkPath $startLink -TargetPath "powershell.exe" -Arguments $psArgs -WorkDir $InstallRoot -Icon $IconPath
    New-Shortcut -LinkPath $DesktopShortcut -TargetPath "powershell.exe" -Arguments $psArgs -WorkDir $InstallRoot -Icon $IconPath
    Write-Host "Shortcuts created (Desktop + Start Menu\$BrandName)."
}

if (-not $SkipResolveScript) {
    try {
        New-Item -ItemType Directory -Force -Path $ResolveMenuRoot | Out-Null
        $menuScript = Join-Path $ResolveMenuRoot ($ResolveScriptName + ".py")
        $menuLines = @(
            "# Auto-generated by $AppName installer.",
            "import subprocess",
            "from pathlib import Path",
            "",
            "HELPER = Path(r'$InstallRoot') / 'export_helper.py'",
            "PYTHONW = Path(r'$pythonw')",
            "CWD = Path(r'$InstallRoot')",
            "",
            "def main():",
            "    if not HELPER.is_file():",
            "        raise SystemExit(f'ExportHelper not found: {HELPER}')",
            "    exe = str(PYTHONW if PYTHONW.is_file() else 'python')",
            "    subprocess.Popen([exe, str(HELPER)], cwd=str(CWD))",
            "",
            "if __name__ == '__main__':",
            "    main()",
            ""
        )
        Set-Content -LiteralPath $menuScript -Value $menuLines -Encoding UTF8

        $legacyScripts = @(
            "WiKiVibe_DRT_Timeline_Export.py",
            "WiKiVibe_ResolveTimelinePackager.py",
            "ResolveTimelinePackager.py"
        )
        foreach ($legacy in $legacyScripts) {
            $legacyPath = Join-Path $ResolveMenuRoot $legacy
            if (Test-Path -LiteralPath $legacyPath) {
                Remove-Item -LiteralPath $legacyPath -Force
                Write-Host "Removed legacy Resolve script: $legacy"
            }
        }
        Write-Host "Resolve script installed: $menuScript"
        Write-Host "Resolve menu: Workspace > Scripts > $ResolveScriptName"
    } catch {
        Write-Host "Resolve script install skipped: $_"
    }
}

$done = @"
Install complete / 安裝完成

$AppName

Installed to:
$InstallRoot

How to open / 如何開啟:
1. Desktop: $AppName
2. Start Menu: $BrandName > $AppName
3. Or Launch.bat in the install folder
4. Resolve (restart first):
   Workspace > Scripts > $ResolveScriptName

Tips:
- Resolve Studio must be running before export
- Open your DRT timeline, then click Export
"@
Show-Msg $done
Write-Host $done
exit 0
