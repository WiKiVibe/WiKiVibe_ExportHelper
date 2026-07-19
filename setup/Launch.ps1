# WiKiVibe ExportHelper launcher
# Finds app either next to this script (install folder) or under repo\app\

$ErrorActionPreference = "Stop"
$BrandName = "WiKiVibe"
$ProductName = "ExportHelper"
$AppName = "$BrandName $ProductName"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path

function Find-Python {
    $candidates = @(
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python313\python.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python311\python.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python310\python.exe")
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

function Resolve-AppScript {
    # 1) Flat install / portable copy next to launcher
    $candidates = @(
        (Join-Path $here "export_helper.py"),
        (Join-Path $here "app\export_helper.py")
    )
    # 2) Repo layout: setup\Launch.ps1 -> ..\app\export_helper.py
    $repoApp = Join-Path (Split-Path -Parent $here) "app\export_helper.py"
    $candidates += $repoApp
    # 3) Installed location
    $candidates += (Join-Path $env:LOCALAPPDATA "$BrandName\$ProductName\export_helper.py")

    foreach ($c in $candidates) {
        if (Test-Path -LiteralPath $c) { return (Resolve-Path -LiteralPath $c).Path }
    }
    return $null
}

$python = Find-Python
if (-not $python) {
    Add-Type -AssemblyName PresentationFramework
    [System.Windows.MessageBox]::Show(
        "Python 3.10+ not found. Run START_HERE.bat first.`n`n找不到 Python，請先執行 START_HERE.bat。",
        $AppName
    ) | Out-Null
    exit 2
}

$pythonw = Join-Path (Split-Path $python) "pythonw.exe"
if (-not (Test-Path -LiteralPath $pythonw)) { $pythonw = $python }

$script = Resolve-AppScript
if (-not $script) {
    Add-Type -AssemblyName PresentationFramework
    [System.Windows.MessageBox]::Show(
        "export_helper.py not found. Run START_HERE.bat first.`n`n找不到主程式，請先執行 START_HERE.bat。",
        $AppName
    ) | Out-Null
    exit 2
}

& $python -c "import PyQt6" 2>$null
if ($LASTEXITCODE -ne 0) {
    Add-Type -AssemblyName PresentationFramework
    $ans = [System.Windows.MessageBox]::Show(
        "PyQt6 is missing. Install now?`n`n缺少 PyQt6，是否現在安裝？",
        $AppName,
        [System.Windows.MessageBoxButton]::YesNo
    )
    if ($ans -ne [System.Windows.MessageBoxResult]::Yes) { exit 2 }
    & $python -m pip install "PyQt6>=6.6"
    if ($LASTEXITCODE -ne 0) { exit 2 }
}

Set-Location -LiteralPath (Split-Path $script)
& $pythonw $script @args
exit $LASTEXITCODE
