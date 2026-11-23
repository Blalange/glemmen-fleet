# Phase 2: install the Windows roles and promote the VM to the first domain
# controller of a new forest. Runs as SYSTEM at startup (WinSrv01-Phase2) and is
# idempotent: if the domain already exists it just disables its own task.
$ErrorActionPreference = 'Stop'
Start-Transcript -Path C:\provision\phase2.log -Append
$failed = $false

try {
    $secrets = Get-Content -Path 'C:\provision\secrets.json' -Raw | ConvertFrom-Json

    Write-Host 'Installing AD DS, DNS and RSAT features'
    $core = Install-WindowsFeature -Name 'AD-Domain-Services', 'DNS', 'RSAT-AD-PowerShell', 'RSAT-DNS-Server' -IncludeManagementTools
    if (-not $core.Success) {
        throw "Core feature installation failed: $($core.FeatureResult | Out-String)"
    }

    Write-Host 'Installing IIS and FTP features'
    $iis = Install-WindowsFeature -Name 'Web-Server', 'Web-Ftp-Server', 'Web-Ftp-Service', 'Web-Mgmt-Console' -IncludeManagementTools
    if (-not $iis.Success) {
        Write-Host "IIS/FTP feature installation failed: $($iis.FeatureResult | Out-String)"
    }

    Write-Host 'Installing OpenSSH Server'
    if (-not (Get-Service -Name sshd -ErrorAction SilentlyContinue)) {
        $capability = Get-WindowsCapability -Online |
            Where-Object Name -like 'OpenSSH.Server*' |
            Select-Object -First 1
        if ($capability) {
            try {
                Add-WindowsCapability -Online -Name $capability.Name -ErrorAction Stop | Out-Null
                Write-Host "Installed $($capability.Name)"
            } catch {
                Write-Host "OpenSSH capability install failed: $_"
            }
        }
    }
    if (-not (Get-Service -Name sshd -ErrorAction SilentlyContinue)) {
        # Fallback for images where the FoD payload is not available from
        # Windows Update: use the official Win32-OpenSSH release.
        try {
            [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
            $zip = 'C:\provision\OpenSSH-Win64.zip'
            Invoke-WebRequest -Uri 'https://github.com/PowerShell/Win32-OpenSSH/releases/latest/download/OpenSSH-Win64.zip' -OutFile $zip -UseBasicParsing
            Expand-Archive -Path $zip -DestinationPath 'C:\Program Files\OpenSSH' -Force
            $installer = Get-ChildItem -Path 'C:\Program Files\OpenSSH' -Filter 'install-sshd.ps1' -Recurse |
                Select-Object -First 1
            if ($installer) {
                Push-Location $installer.DirectoryName
                & $installer.FullName
                Pop-Location
                Write-Host 'Installed OpenSSH from the Win32-OpenSSH release'
            }
        } catch {
            Write-Host "OpenSSH fallback install failed: $_"
        }
    }

    # AD DS needs its own DNS. Point every up interface at localhost before
    # promotion; forwarders are configured in phase 3.
    Get-NetAdapter | Where-Object { $_.Status -eq 'Up' } | ForEach-Object {
        Set-DnsClientServerAddress -InterfaceIndex $_.ifIndex -ServerAddresses 127.0.0.1
    }
    Set-Service -Name DNS -StartupType Automatic -ErrorAction SilentlyContinue
    Start-Service -Name DNS -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 10

    $promoted = $false
    try {
        Get-ADDomain -ErrorAction Stop | Out-Null
        $promoted = $true
    } catch {
        $promoted = $false
    }

    if ($promoted) {
        Write-Host 'This server is already a domain controller; phase 2 is done.'
        Disable-ScheduledTask -TaskName 'WinSrv01-Phase2' -ErrorAction SilentlyContinue | Out-Null
        exit 0
    }

    Write-Host 'Promoting to a new AD forest ad.glemmen.internal'
    Import-Module ADDSDeployment
    $dsrm = ConvertTo-SecureString -String $secrets.dsrm_password -AsPlainText -Force
    Install-ADDSForest `
        -DomainName 'ad.glemmen.internal' `
        -DomainNetbiosName 'GLEMMEN' `
        -InstallDns `
        -SafeModeAdministratorPassword $dsrm `
        -NoRebootOnCompletion `
        -Force

    Write-Host 'AD DS promotion completed; rebooting'
    Restart-Computer -Force
    exit 0
} catch {
    Write-Host "Phase 2 failed: $_"
    $failed = $true
} finally {
    Stop-Transcript
}
if ($failed) {
    exit 1
}
