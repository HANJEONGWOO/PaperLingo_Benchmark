# Run in Windows PowerShell as Administrator ONLY when WSL's IP changes.
# Preserves all unrelated port forwarding/firewall entries.
param(
    [int]$Port = 4173,
    [string]$WslAddress = ""
)
$ErrorActionPreference = "Stop"
$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Run this script from an Administrator PowerShell window."
}
if (-not $WslAddress) {
    $WslAddress = ((wsl.exe hostname -I).Trim() -split '\s+')[0]
}
$parsedAddress = $null
if (-not [System.Net.IPAddress]::TryParse($WslAddress, [ref]$parsedAddress) -or $parsedAddress.AddressFamily -ne 'InterNetwork') {
    throw "A valid WSL IPv4 address is required."
}
if ($Port -lt 1 -or $Port -gt 65535) { throw "Invalid port." }
netsh interface portproxy add v4tov4 listenaddress=0.0.0.0 listenport=$Port connectaddress=$WslAddress connectport=$Port
if ($LASTEXITCODE -ne 0) { throw "Failed to configure port forwarding." }
$ruleName = "PaperLingo-TCP-$Port"
if (-not (Get-NetFirewallRule -Name $ruleName -ErrorAction SilentlyContinue)) {
    New-NetFirewallRule -Name $ruleName -DisplayName "PaperLingo TCP $Port" -Direction Inbound -Action Allow -Protocol TCP -LocalPort $Port | Out-Null
}
Write-Output "Windows TCP $Port -> WSL ${WslAddress}:$Port configured."
Write-Output "Router forwarding must map external TCP $Port to this Windows PC's LAN address, TCP $Port."
