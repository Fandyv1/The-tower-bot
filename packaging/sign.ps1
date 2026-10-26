<#
.SYNOPSIS
    Sign a TheTowerBot executable with an Authenticode PFX certificate.

.EXAMPLE
    $env:CODE_SIGN_PFX = "C:\path\cert.pfx"
    $env:CODE_SIGN_PASSWORD = "password"
    .\packaging\sign.ps1 -Path .\dist\TheTowerBot\TheTowerBot.exe
#>
param(
    [Parameter(Mandatory = $true)]
    [string]$Path
)

$pfx = $env:CODE_SIGN_PFX
$password = $env:CODE_SIGN_PASSWORD

if (-not $pfx -or -not (Test-Path -LiteralPath $pfx)) {
    throw "CODE_SIGN_PFX 未设置或文件不存在。请提供代码签名 PFX 证书。"
}
if (-not (Test-Path -LiteralPath $Path)) {
    throw "待签名文件不存在: $Path"
}
if (-not $password) {
    throw "CODE_SIGN_PASSWORD 未设置。"
}

$secure = ConvertTo-SecureString -String $password -AsPlainText -Force
$certPath = (Resolve-Path -LiteralPath $pfx).Path
$cert = [System.Security.Cryptography.X509Certificates.X509Certificate2]::new($certPath, $secure)
if (-not $cert.HasPrivateKey) {
    throw "PFX 中没有私钥，不能用于代码签名。"
}

$result = Set-AuthenticodeSignature `
    -FilePath (Resolve-Path -LiteralPath $Path).Path `
    -Certificate $cert `
    -TimestampServer "http://timestamp.digicert.com" `
    -HashAlgorithm SHA256

if ($result.Status -ne "Valid") {
    throw "签名失败: $($result.Status) $($result.StatusMessage)"
}
Write-Host "Signing OK: $Path"
Write-Host "Subject: $($cert.Subject)"
