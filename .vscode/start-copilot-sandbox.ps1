[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Workspace,

    [Parameter(Mandatory = $true)]
    [string]$SandboxName
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$workspacePath = (Get-Item -LiteralPath $Workspace).FullName
$settingsPath = Join-Path $workspacePath '.vscode\copilot-settings.json'
if (-not (Test-Path -LiteralPath $settingsPath -PathType Leaf)) {
    throw "Copilot defaults are missing: $settingsPath"
}
$null = Get-Content -LiteralPath $settingsPath -Raw | ConvertFrom-Json

$command = Get-Command sbx.exe -ErrorAction SilentlyContinue
if ($null -ne $command) {
    $sbx = $command.Source
} else {
    $sbx = Join-Path $env:LOCALAPPDATA 'DockerSandboxes\bin\sbx.exe'
    if (-not (Test-Path -LiteralPath $sbx -PathType Leaf)) {
        throw 'Docker Sandboxes is not installed. Run: winget install --id Docker.sbx --exact'
    }
}

$parentPath = Split-Path -Path $workspacePath -Parent
# Keep the Linux environment off the Windows-backed workspace mount.
$pythonEnvironment = "/home/agent/.venvs/$(Split-Path -Path $workspacePath -Leaf)"

$sshForwarding = & $sbx settings get ssh.agentForwardingEnabled
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
if ($sshForwarding -ne 'false') {
    throw 'SSH agent forwarding must be off. Run: sbx settings set ssh.agentForwardingEnabled false; sbx daemon restart'
}

$udpSetting = & $sbx settings get feature.udp-egress --json
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
if (-not ($udpSetting | ConvertFrom-Json).value.enabled) {
    throw 'UDP access is not enabled. Complete the Docker Sandboxes setup in README.md first.'
}

$sandboxes = @(& $sbx ls --quiet)
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
if ($sandboxes -notcontains $SandboxName) {
    # The first mount sets Copilot's working directory; the parent exposes sibling projects.
    & $sbx create --name $SandboxName --env "UV_PROJECT_ENVIRONMENT=$pythonEnvironment" --skills off copilot $workspacePath $parentPath
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

$githubSecrets = @(& $sbx secret ls --sandbox $SandboxName --service github --quiet)
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
if ($githubSecrets -notcontains 'github') {
    throw "The sandbox's GitHub credential is missing. In host PowerShell, run: & '$sbx' secret set github --sandbox '$SandboxName'. Use only the dedicated Copilot Requests-only token."
}

$applySettings = @'
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const source = path.join('.vscode', 'copilot-settings.json');
JSON.parse(fs.readFileSync(source, 'utf8'));
const home = process.env.COPILOT_HOME || path.join(os.homedir(), '.copilot');
fs.mkdirSync(home, { recursive: true });
fs.copyFileSync(source, path.join(home, 'settings.json'));
'@
& $sbx exec $SandboxName node -e $applySettings
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

& $sbx policy allow network --sandbox $SandboxName --protocol 'tcp,udp' '**'
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

& $sbx run --name $SandboxName --env "UV_PROJECT_ENVIRONMENT=$pythonEnvironment" -- --yolo
exit $LASTEXITCODE
