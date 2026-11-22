param([switch]$NoBrowser)

$ErrorActionPreference = 'Stop'
$runtime = Join-Path $PSScriptRoot 'runtime'
$app = Join-Path $PSScriptRoot 'app'
if (-not (Test-Path -LiteralPath (Join-Path $app 'app.py'))) { $app = $PSScriptRoot }
$python = Join-Path $app '.venv\Scripts\python.exe'
$ffmpeg = Join-Path $runtime 'ffmpeg\bin'
$log = Join-Path $runtime 'server-error.log'
New-Item -ItemType Directory -Path (Join-Path $runtime 'temp') -Force | Out-Null
$env:NEONCLIPPING_DATA_DIR = Join-Path $PSScriptRoot 'Data'
$env:NEONCLIPPING_LAN = '0'
$env:NEONCLIPPING_PORT = '8770'
$env:HF_HOME = Join-Path $runtime 'huggingface'
$env:PIP_CACHE_DIR = Join-Path $runtime 'pip-cache'
$env:TEMP = Join-Path $runtime 'temp'
$env:TMP = $env:TEMP
$env:DENO_DIR = Join-Path $runtime 'deno-cache'
$env:DENO_NO_UPDATE_CHECK = '1'
$env:XDG_CACHE_HOME = Join-Path $runtime 'cache'
$deno = Join-Path $runtime 'deno'
$env:Path = "$ffmpeg;$deno;$env:Path"
$url = 'http://127.0.0.1:8770'

function Test-NeonClipping {
    try {
        $page = Invoke-WebRequest -UseBasicParsing -Uri $url -TimeoutSec 2
        return $page.Content.Contains('<title>NeonClipping</title>')
    } catch {
        return $false
    }
}

try {
    if (-not (Test-NeonClipping)) {
        if (Get-NetTCPConnection -LocalPort 8770 -State Listen -ErrorAction SilentlyContinue) {
            throw 'Port 8770 is already in use by another app.'
        }
        if (-not (Test-Path -LiteralPath $python)) {
            throw 'Create the .venv and install requirements.txt as described in the README.'
        }
        if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue) -or
            -not (Get-Command ffprobe -ErrorAction SilentlyContinue)) {
            throw 'Install FFmpeg and add ffmpeg and ffprobe to PATH.'
        }
        $server = Start-Process -FilePath $python -ArgumentList 'app.py' -WorkingDirectory $app -WindowStyle Hidden -RedirectStandardOutput (Join-Path $runtime 'server.log') -RedirectStandardError $log -PassThru
        $deadline = (Get-Date).AddSeconds(90)
        $ready = $false
        do {
            Start-Sleep -Milliseconds 500
            $ready = Test-NeonClipping
        } until ($ready -or $server.HasExited -or (Get-Date) -ge $deadline)
        if (-not $ready) { throw "NeonClipping did not start. Check $log." }
    }
    if (-not $NoBrowser) { Start-Process $url }
    Write-Output "NeonClipping is running at $url"
} catch {
    if (-not $NoBrowser) {
        Add-Type -AssemblyName System.Windows.Forms
        [System.Windows.Forms.MessageBox]::Show($_.Exception.Message, 'NeonClipping') | Out-Null
    }
    throw
}
