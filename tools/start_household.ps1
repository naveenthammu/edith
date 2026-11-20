[CmdletBinding()]
param(
    [string]$DataDirectory = 'D:\CodexTools\edith-household',
    [string]$PythonExecutable = 'D:\CodexTools\edith-python\Scripts\python.exe',
    [string]$HubExecutable = 'D:\CodexTools\edith-rust\central-target\debug\edith-central-hub.exe',
    [switch]$OwnerSession,
    [switch]$CheckOnly
)
$ErrorActionPreference = 'Stop'
$edithRepository = Split-Path -Parent $PSScriptRoot
$edithServerConfig = Join-Path $DataDirectory 'hub-config.json'
$edithClientName = if ($OwnerSession) { 'owner-client.json' } else { 'desktop-client.json' }
$edithClientConfig = Join-Path $DataDirectory $edithClientName
foreach ($edithRequired in @($PythonExecutable, $HubExecutable, $edithServerConfig, $edithClientConfig)) {
    if (-not (Test-Path -LiteralPath $edithRequired -PathType Leaf)) {
        throw "Required EDITH file is missing: $edithRequired. Follow docs/product-plan/central-engine-runbook.md."
    }
}

$env:EDITH_HUB_CONFIG = $edithServerConfig
$env:EDITH_HUB_CLIENT_CONFIG = $edithClientConfig
$env:EDITH_DATA_DIR = Join-Path $DataDirectory 'runtime'
$env:EDITH_LOCAL_PROVIDER = 'ollama'
$env:EDITH_OLLAMA_URL = 'http://127.0.0.1:11434'
$env:EDITH_OLLAMA_MODEL = 'qwen3:4b-instruct'
$env:EDITH_EMBEDDING_MODEL = 'nomic-embed-text:v1.5'
$edithWorkspaceConfig = Join-Path $DataDirectory 'workspace-config.json'
if ($OwnerSession -and (Test-Path -LiteralPath $edithWorkspaceConfig -PathType Leaf)) {
    $env:EDITH_WORKSPACE_CONFIG = $edithWorkspaceConfig
} else {
    Remove-Item Env:EDITH_WORKSPACE_CONFIG -ErrorAction SilentlyContinue
}
$edithGmailConfig = Join-Path $DataDirectory 'gmail\account.json'
if ($OwnerSession -and (Test-Path -LiteralPath $edithGmailConfig -PathType Leaf)) {
    $env:EDITH_GMAIL_ACCOUNT_CONFIG = $edithGmailConfig
} else {
    Remove-Item Env:EDITH_GMAIL_ACCOUNT_CONFIG -ErrorAction SilentlyContinue
}
$edithMerchantConfig = Join-Path $DataDirectory 'merchant\merchant.json'
if ($OwnerSession -and (Test-Path -LiteralPath $edithMerchantConfig -PathType Leaf)) {
    $env:EDITH_MERCHANT_CONFIG = $edithMerchantConfig
} else {
    Remove-Item Env:EDITH_MERCHANT_CONFIG -ErrorAction SilentlyContinue
}
$edithBusinessConfig = Join-Path $DataDirectory 'whatsapp\connection.json'
if ($OwnerSession -and (Test-Path -LiteralPath $edithBusinessConfig -PathType Leaf)) {
    $env:EDITH_WHATSAPP_CONNECTION_CONFIG = $edithBusinessConfig
} else {
    Remove-Item Env:EDITH_WHATSAPP_CONNECTION_CONFIG -ErrorAction SilentlyContinue
}
$edithOwnedHub = $null
Push-Location -LiteralPath $edithRepository
try {
    if ($CheckOnly) {
        & $PythonExecutable -c "from brain.hub import HubClient; from config import load_config; c=load_config(); client=HubClient.from_env(); assert client is not None; print('Configuration parsed. Local provider:', c.local_provider); print('No microphone or UI started.')"
        if ($LASTEXITCODE -ne 0) { throw 'EDITH configuration check failed.' }
        return
    }
    # Only reuse a hub that accepts this paired client. Never kill an unrelated process.
    & $PythonExecutable -c "from brain.hub import HubClient,HubError; import sys; c=HubClient.from_env();
try: c.capabilities()
except HubError: sys.exit(1)" 2>$null
    if ($LASTEXITCODE -ne 0) {
        $edithOwnedHub = Start-Process -FilePath $HubExecutable -WorkingDirectory $edithRepository -WindowStyle Hidden -PassThru
        $edithReady = $false
        for ($edithAttempt = 0; $edithAttempt -lt 20; $edithAttempt++) {
            if ($edithOwnedHub.HasExited) { throw 'The hub exited. Check configuration, permissions and whether its port is already occupied.' }
            Start-Sleep -Milliseconds 250
            & $PythonExecutable -c "from brain.hub import HubClient,HubError; import sys; c=HubClient.from_env();
try: c.capabilities()
except HubError: sys.exit(1)" 2>$null
            if ($LASTEXITCODE -eq 0) { $edithReady = $true; break }
        }
        if (-not $edithReady) { throw 'The hub did not become ready.' }
    }
    Write-Host 'Starting the existing EDITH interface. This starts microphone capture.'
    & $PythonExecutable main.py
    if ($LASTEXITCODE -ne 0) { throw 'EDITH exited with an error; see the local runtime log.' }
} finally {
    if ($null -ne $edithOwnedHub -and -not $edithOwnedHub.HasExited) {
        Stop-Process -Id $edithOwnedHub.Id
    }
    Pop-Location
}
