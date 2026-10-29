<#
  First-time setup for Stanton Trade Optimizer.
  run.cmd calls this automatically the first time. Run setup.cmd yourself to repair or update.

  1. Finds 64-bit Python 3.12 or newer, or offers to install Python 3.13 with winget.
  2. Creates a private environment in .venv and installs the pinned packages from requirements.txt.
  3. Checks Windows OCR, then offers a desktop shortcut and (if you use Tabby) a Tabby profile.

  Nothing here needs administrator rights. Python installs for your Windows user only.
#>
param(
    [switch]$Yes,        # answer yes to every question (unattended)
    [switch]$NoExtras    # skip the shortcut and Tabby questions
)

# Native tools write warnings to stderr; check exit codes instead of stopping on them.
$ErrorActionPreference = 'Continue'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $Root
$Venv = Join-Path $Root '.venv'
$VenvPy = Join-Path $Venv 'Scripts\python.exe'
$Marker = Join-Path $Venv '.setup-ok'
$Requirements = Join-Path $Root 'requirements.txt'

function Say([string]$Text, [string]$Color = 'Gray') { Write-Host $Text -ForegroundColor $Color }
function Step([string]$Text) { Write-Host ''; Write-Host "== $Text" -ForegroundColor DarkYellow }
function Fail([string]$Text) { Write-Host ''; Write-Host $Text -ForegroundColor Red; exit 1 }

function Ask([string]$Question, [bool]$Default = $true) {
    if ($Yes) { return $true }
    $hint = '[y/N]'
    if ($Default) { $hint = '[Y/n]' }
    $answer = Read-Host "$Question $hint"
    if ([string]::IsNullOrWhiteSpace($answer)) { return $Default }
    return $answer.Trim().ToLower().StartsWith('y')
}

function Get-PythonVersion([string]$Exe, [string[]]$Pre = @()) {
    # "3.13" for a usable 64-bit Python 3.12 or newer, otherwise $null.
    try {
        $out = & $Exe @Pre -c "import sys, struct; print('%d.%d %d' % (sys.version_info[0], sys.version_info[1], struct.calcsize('P') * 8))" 2>$null
    } catch {
        return $null
    }
    if ($LASTEXITCODE -ne 0 -or -not $out) { return $null }
    $parts = "$out".Trim().Split(' ')
    if ($parts.Count -ne 2) { return $null }
    if (([version]$parts[0]) -ge ([version]'3.12') -and $parts[1] -eq '64') { return $parts[0] }
    return $null
}

function Find-Python {
    $candidates = New-Object System.Collections.ArrayList

    # The py launcher, wherever it is (PATH may not be refreshed right after an install).
    $launchers = @()
    $py = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($py) { $launchers += $py.Source }
    $launchers += (Join-Path $env:LOCALAPPDATA 'Programs\Python\Launcher\py.exe'), (Join-Path $env:WINDIR 'py.exe')
    foreach ($launcher in ($launchers | Select-Object -Unique)) {
        if (Test-Path -LiteralPath $launcher) {
            foreach ($v in '-3.13', '-3.12', '-3.14') { [void]$candidates.Add(@($launcher, $v)) }
        }
    }

    # Standard install folders.
    foreach ($dir in (Join-Path $env:LOCALAPPDATA 'Programs\Python'), $env:ProgramFiles, 'C:\') {
        Get-ChildItem -LiteralPath $dir -Directory -Filter 'Python3*' -ErrorAction SilentlyContinue | ForEach-Object {
            $exe = Join-Path $_.FullName 'python.exe'
            if (Test-Path -LiteralPath $exe) { [void]$candidates.Add(@($exe)) }
        }
    }

    # python on PATH, but not the Microsoft Store stub (running it opens the Store).
    $onPath = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($onPath -and $onPath.Source -notlike '*\WindowsApps\*') { [void]$candidates.Add(@($onPath.Source)) }

    foreach ($c in $candidates) {
        $pre = @()
        if ($c.Count -gt 1) { $pre = @($c[1]) }
        $ver = Get-PythonVersion $c[0] $pre
        if ($ver) { return @{ Exe = $c[0]; Pre = $pre; Version = $ver } }
    }
    return $null
}

function Install-Python {
    $winget = Get-Command winget.exe -ErrorAction SilentlyContinue
    if (-not $winget) {
        Say 'winget (the Windows Package Manager) is not available on this PC.' Yellow
        Say 'Install Python 3.13, 64-bit, from the page that is opening now, then run setup.cmd again.' Yellow
        Say 'During that install, tick "Add python.exe to PATH".' Yellow
        Start-Process 'https://www.python.org/downloads/windows/'
        return
    }
    Say 'Installing Python 3.13 for your Windows user (about 30 MB, no admin needed)...'
    & $winget.Source install --id Python.Python.3.13 -e --scope user --silent --accept-package-agreements --accept-source-agreements
}

# ---------------------------------------------------------------------------

Say ''
Say 'STANTON TRADE OPTIMIZER: setup' Cyan
Say "Folder: $Root" DarkGray

if ($Root -match '\\Temp\d*_[^\\]*\.zip') {
    Say 'This is running from inside the zip, from a temporary folder Windows deletes.' Yellow
    Fail 'Extract the zip first (right-click > Extract All), then run run.cmd from the extracted folder.'
}
if ($Root -like "$env:ProgramFiles*" -or $Root -like "${env:ProgramFiles(x86)}*") {
    Say 'This folder is inside Program Files, where Windows blocks normal writes.' Yellow
    Fail 'Move the folder somewhere like Documents or your Desktop, then run it again.'
}

if ($Root.Length -gt 150) {
    Say "Note: this folder's path is $($Root.Length) characters long. Windows OCR fails in very deep folders;" Yellow
    Say 'if setup warns about that below, move the folder somewhere shorter like C:\Games\Trade Optimizer.' Yellow
}

Step 'Python'
$python = Find-Python
if (-not $python) {
    Say 'No 64-bit Python 3.12 or newer found on this PC.'
    if (-not (Ask 'Install Python 3.13 now? It comes from python.org through winget, and accepts its license for you.')) {
        Fail 'Setup needs Python. Install Python 3.12 or newer from python.org and run setup.cmd again.'
    }
    Install-Python
    $python = Find-Python
    if (-not $python) {
        Fail 'Python still was not found. Close this window, open a new one and run setup.cmd again.'
    }
}
Say "Using Python $($python.Version): $($python.Exe) $($python.Pre -join ' ')" Green

Step 'App packages'
if ((Test-Path -LiteralPath $VenvPy) -and -not (Get-PythonVersion $VenvPy)) {
    Say 'The old .venv is broken; rebuilding it.'
    Remove-Item -LiteralPath $Venv -Recurse -Force
}
if (-not (Test-Path -LiteralPath $VenvPy)) {
    Say 'Creating a private Python environment in .venv ...'
    $pre = $python.Pre
    & $python.Exe @pre -m venv $Venv
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $VenvPy)) { Fail 'Could not create .venv.' }
}
Say 'Installing packages (first time takes a minute or two)...'
& $VenvPy -m pip install --disable-pip-version-check --quiet --upgrade pip
& $VenvPy -m pip install --disable-pip-version-check -r $Requirements
if ($LASTEXITCODE -ne 0) { Fail 'Installing packages failed. Check your internet connection and run setup.cmd again.' }

Step 'Checking'
& $VenvPy -c "import textual, mss, keyboard, PIL, tomlkit, winrt.windows.media.ocr; import sto.tui.app"
if ($LASTEXITCODE -ne 0) { Fail 'The packages installed but the app does not load. Run setup.cmd again; if it keeps failing, send the text above.' }
$ocrMessage = & $VenvPy -m sto --check-ocr 2>$null
$ocrCode = $LASTEXITCODE
if ($ocrCode -eq 0) {
    Say 'Windows OCR is ready (reads your balance from the mobiGlas).' Green
} elseif ($ocrCode -eq 2) {
    Say 'This folder path is too long for Windows to load the OCR files, so F1 balance reading will not work.' Yellow
    Say 'Move the whole folder somewhere shorter (for example C:\Games\Trade Optimizer) and run setup.cmd again.' Yellow
    Say 'Everything else works; press w in the app to type your balance.' Yellow
} elseif ($ocrCode -eq 3) {
    Say 'Windows OCR has no language for your account, so the F1 balance reading will not work.' Yellow
    Say 'The app still works: press w to type your balance. To turn OCR on, add English' Yellow
    Say '(Settings > Time and language > Language and region > Add a language), then restart the app.' Yellow
} else {
    Say "Windows OCR could not start: $ocrMessage" Yellow
    Say 'The app still works: press w in the app to type your balance.' Yellow
}
Copy-Item -LiteralPath $Requirements -Destination $Marker -Force

if (-not $NoExtras) {
    Step 'Extras'
    if (Ask 'Put a "Trade Optimizer" shortcut on your desktop?') {
        $desktop = [Environment]::GetFolderPath('Desktop')
        $shell = New-Object -ComObject WScript.Shell
        $link = $shell.CreateShortcut((Join-Path $desktop 'Trade Optimizer.lnk'))
        $link.TargetPath = Join-Path $Root 'run.cmd'
        $link.WorkingDirectory = $Root
        $link.WindowStyle = 3   # open maximized: the app wants a big window
        $link.Description = 'Stanton Trade Optimizer'
        $link.Save()
        Say 'Shortcut added (it opens the app maximized).' Green
    }
    $tabby = Join-Path $env:LOCALAPPDATA 'Programs\Tabby\Tabby.exe'
    if ((Test-Path -LiteralPath $tabby) -and (Ask 'Tabby is installed. Add a "Trade Terminal" profile to it?' $false)) {
        & $VenvPy (Join-Path $Root 'tools\install_tabby_profile.py')
    }
}

Step 'Done'
Say 'Setup finished. Start the app with run.cmd (or the desktop shortcut).' Green
exit 0
