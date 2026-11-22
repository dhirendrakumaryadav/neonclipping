param([switch]$NoBrowser)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$runtime = Join-Path $root 'runtime'
$python = Join-Path $root 'app\.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { $python = Join-Path $root '.venv\Scripts\python.exe' }
if (-not (Test-Path -LiteralPath $python)) { throw 'Create the .venv and install requirements.txt as described in the README.' }
New-Item -ItemType Directory -Path (Join-Path $runtime 'temp') -Force | Out-Null
$workflow = Join-Path $PSScriptRoot 'workflow.py'
$url = 'http://127.0.0.1:8772'
function Test-Page($address,$title) {
    try {
        $page = Invoke-WebRequest -UseBasicParsing -Uri $address -TimeoutSec 2
        return $page.Content.Contains("<title>$title</title>")
    } catch { return $false }
}
if (-not (Test-Page 'http://127.0.0.1:8770' 'NeonClipping')) {
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $root 'Start NeonClipping.ps1') -NoBrowser
    if ($LASTEXITCODE) { throw 'Could not start NeonClipping.' }
}
if (-not (Test-Page $url 'NeonClipping YouTube Queue')) {
    if (Get-NetTCPConnection -LocalPort 8772 -State Listen -ErrorAction SilentlyContinue) {
        throw 'Port 8772 is already in use.'
    }
    $env:PYTHONUTF8 = '1'
    $env:PATH = "$(Join-Path $runtime 'ffmpeg\bin');$env:PATH"
    $env:HF_HOME = Join-Path $runtime 'huggingface'
    $env:TEMP = Join-Path $runtime 'temp'
    $env:TMP = $env:TEMP
    $log = Join-Path $runtime 'youtube-workflow.log'
    $server = Start-Process -FilePath $python -ArgumentList ('"' + $workflow + '"') -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -RedirectStandardError $log -PassThru
    $deadline = (Get-Date).AddSeconds(30)
    do {
        Start-Sleep -Milliseconds 500
        $ready = Test-Page $url 'NeonClipping YouTube Queue'
    } until ($ready -or $server.HasExited -or (Get-Date) -ge $deadline)
    if (-not $ready) { throw "Could not start the YouTube queue. Check $log." }
}
if (-not $NoBrowser) { Start-Process $url }
Write-Output "YouTube queue is running at $url"
