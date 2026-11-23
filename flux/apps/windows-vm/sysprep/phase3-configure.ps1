# Phase 3: create AD users/groups, the simon.no DNS zone, SSH, IIS FTP and
# SQL Server. Runs as SYSTEM at startup (WinSrv01-Phase3) after AD DS promotion.
# The script is idempotent and disables its own task only after every step
# succeeds; on failure it leaves the log in C:\provision and the task enabled
# for a retry at the next boot.
$ErrorActionPreference = 'Stop'
Start-Transcript -Path C:\provision\phase3.log -Append
$failed = $false

function Write-Step {
    param([string]$Message)
    Write-Host ("[{0}] {1}" -f (Get-Date -Format o), $Message)
}

try {
    $secrets = Get-Content -Path 'C:\provision\secrets.json' -Raw | ConvertFrom-Json
    Import-Module ActiveDirectory -ErrorAction Stop
    Import-Module DnsServer -ErrorAction Stop
    Import-Module WebAdministration -ErrorAction Stop

    # Wait for AD DS after the promotion reboot.
    $deadline = (Get-Date).AddMinutes(30)
    while ($true) {
        try {
            $domain = Get-ADDomain -ErrorAction Stop
            break
        } catch {
            if ((Get-Date) -gt $deadline) {
                throw "AD DS did not become ready: $_"
            }
            Start-Sleep -Seconds 15
        }
    }
    $dn = $domain.DistinguishedName
    $netbios = $domain.NetBIOSName
    Write-Step "Domain $($domain.DNSRoot) is ready"

    # ------------------------------------------------------------------ AD
    foreach ($groupName in @('ssh-users', 'ftp-users')) {
        if (-not (Get-ADGroup -Filter "SamAccountName -eq '$groupName'" -ErrorAction SilentlyContinue)) {
            New-ADGroup -Name $groupName -SamAccountName $groupName -GroupScope Global -GroupCategory Security -Path "CN=Users,$dn" | Out-Null
            Write-Step "Created AD group $groupName"
        }
    }

    function Ensure-AdUser {
        param([string]$Name, [string]$Password)
        if (-not (Get-ADUser -Filter "SamAccountName -eq '$Name'" -ErrorAction SilentlyContinue)) {
            New-ADUser -Name $Name -SamAccountName $Name -UserPrincipalName "$Name@$($domain.DNSRoot)" `
                -AccountPassword (ConvertTo-SecureString -String $Password -AsPlainText -Force) `
                -Enabled $true -PasswordNeverExpires $true -ChangePasswordAtLogon $false `
                -Path "CN=Users,$dn" | Out-Null
            Write-Step "Created AD user $Name"
        }
    }
    Ensure-AdUser -Name 'simon' -Password $secrets.simon_password
    Ensure-AdUser -Name 'ftpuser' -Password $secrets.ftp_password

    foreach ($groupName in @('Domain Admins', 'Remote Desktop Users', 'ssh-users')) {
        Add-ADGroupMember -Identity $groupName -Members 'simon' -ErrorAction SilentlyContinue
    }
    Add-ADGroupMember -Identity 'ftp-users' -Members 'ftpuser' -ErrorAction SilentlyContinue
    Write-Step 'AD group memberships are set'

    # A domain controller does not grant Domain Users "log on locally". IIS FTP
    # basic authentication impersonates the user interactively, so the dedicated
    # FTP account needs SeInteractiveLogonRight. It is not in Remote Desktop
    # Users or ssh-users, so this does not grant RDP or SSH.
    $ftpUser = Get-ADUser -Identity 'ftpuser'
    $ftpSid = $ftpUser.SID.Value
    $inf = 'C:\provision\secedit-userrights.inf'
    $db = 'C:\provision\secedit-userrights.sdb'
    & "$env:SystemRoot\System32\secedit.exe" /export /cfg $inf /quiet | Out-Null
    $lines = Get-Content -Path $inf
    $rightLine = $lines | Where-Object { $_ -match '^SeInteractiveLogonRight' }
    if (-not $rightLine) {
        $lines += "SeInteractiveLogonRight = *$ftpSid"
        Set-Content -Path $inf -Value $lines -Encoding Unicode
        if (Test-Path $db) { Remove-Item $db -Force }
        & "$env:SystemRoot\System32\secedit.exe" /configure /db $db /cfg $inf /areas USER_RIGHTS /quiet | Out-Null
        Write-Step 'Granted SeInteractiveLogonRight to ftpuser'
    } elseif ($rightLine -notmatch [regex]::Escape($ftpSid)) {
        $newLine = $rightLine + ",*$ftpSid"
        $lines = $lines | ForEach-Object {
            if ($_ -match '^SeInteractiveLogonRight') { $newLine } else { $_ }
        }
        Set-Content -Path $inf -Value $lines -Encoding Unicode
        if (Test-Path $db) { Remove-Item $db -Force }
        & "$env:SystemRoot\System32\secedit.exe" /configure /db $db /cfg $inf /areas USER_RIGHTS /quiet | Out-Null
        Write-Step 'Granted SeInteractiveLogonRight to ftpuser'
    }

    # ----------------------------------------------------------------- DNS
    $zone = 'simon.no'
    if (-not (Get-DnsServerZone -Name $zone -ErrorAction SilentlyContinue)) {
        Add-DnsServerPrimaryZone -Name $zone -ReplicationScope 'Domain' -DynamicUpdate 'None' | Out-Null
        Write-Step "Created DNS zone $zone"
    }
    $existing = @(Get-DnsServerResourceRecord -ZoneName $zone -Name '@' -RRType A -ErrorAction SilentlyContinue)
    $hasAddress = $existing | Where-Object { $_.RecordData.IPv4Address.IPAddressToString -eq '192.168.6.96' }
    if (-not $hasAddress) {
        foreach ($record in $existing) {
            Remove-DnsServerResourceRecord -ZoneName $zone -InputObject $record -Force
        }
        Add-DnsServerResourceRecordA -ZoneName $zone -Name '@' -IPv4Address '192.168.6.96' | Out-Null
    }
    Set-DnsServerForwarder -IPAddress 192.168.6.1, 1.1.1.1 | Out-Null
    Get-NetAdapter | Where-Object { $_.Status -eq 'Up' } | ForEach-Object {
        Set-DnsClientServerAddress -InterfaceIndex $_.ifIndex -ServerAddresses 127.0.0.1
    }
    foreach ($rule in @(
        @{ Name = 'DNS-TCP-53'; Protocol = 'TCP' },
        @{ Name = 'DNS-UDP-53'; Protocol = 'UDP' }
    )) {
        if (-not (Get-NetFirewallRule -Name $rule.Name -ErrorAction SilentlyContinue)) {
            New-NetFirewallRule -Name $rule.Name -DisplayName "DNS ($($rule.Protocol) 53)" `
                -Protocol $rule.Protocol -LocalPort 53 -Direction Inbound -Action Allow | Out-Null
        }
    }
    Write-Step "DNS: simon.no -> 192.168.6.96; forwarders 192.168.6.1, 1.1.1.1"

    # ----------------------------------------------------------------- SSH
    Set-Service -Name sshd -StartupType Automatic -ErrorAction Stop
    Start-Service -Name sshd -ErrorAction Stop
    $sshdConfig = 'C:\ProgramData\ssh\sshd_config'
    if (Test-Path $sshdConfig) {
        $cfg = Get-Content -Path $sshdConfig -Raw
        if ($cfg -match '(?m)^\s*#?\s*PasswordAuthentication\s+') {
            $cfg = [regex]::Replace($cfg, '(?m)^\s*#?\s*PasswordAuthentication\s+.*$', 'PasswordAuthentication yes')
        } else {
            $cfg += "`r`nPasswordAuthentication yes`r`n"
        }
        $cfg = [regex]::Replace($cfg, '(?m)^\s*AllowUsers\s+.*$', '')
        $allow = "AllowUsers simon GLEMMEN\simon`r`n"
        if ($cfg -match '(?m)^Match\s+') {
            $cfg = [regex]::Replace($cfg, '(?m)^Match\s+', $allow + 'Match ', 1)
        } else {
            $cfg = $cfg.TrimEnd() + "`r`n$allow"
        }
        Set-Content -Path $sshdConfig -Value $cfg -Encoding ascii
    }
    Restart-Service -Name sshd -Force
    New-Item -Path 'HKLM:\SOFTWARE\OpenSSH' -Force | Out-Null
    Set-ItemProperty -Path 'HKLM:\SOFTWARE\OpenSSH' -Name DefaultShell `
        -Value 'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe'
    if (-not (Get-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -ErrorAction SilentlyContinue)) {
        New-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -DisplayName 'OpenSSH Server (sshd)' `
            -Enabled True -Direction Inbound -Protocol TCP -Action Allow -LocalPort 22 -Profile Any -RemoteAddress Any | Out-Null
    }
    Set-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -Profile Any -RemoteAddress Any -Enabled True -ErrorAction SilentlyContinue
    if (-not (Get-NetFirewallRule -Name 'SSH-22-Any' -ErrorAction SilentlyContinue)) {
        New-NetFirewallRule -Name 'SSH-22-Any' -DisplayName 'SSH (TCP 22)' `
            -Protocol TCP -LocalPort 22 -Direction Inbound -Action Allow -Profile Any -RemoteAddress Any | Out-Null
    }
    Write-Step 'SSH is enabled for simon'

    # ----------------------------------------------------------------- FTP
    $ftpRoot = 'C:\ftp\ftpuser'
    New-Item -ItemType Directory -Force -Path $ftpRoot | Out-Null
    Set-Service -Name FTPSVC -StartupType Automatic -ErrorAction SilentlyContinue
    Start-Service -Name FTPSVC -ErrorAction SilentlyContinue
    if (-not (Get-WebSite -Name 'FTP' -ErrorAction SilentlyContinue)) {
        New-WebFtpSite -Name 'FTP' -Port 21 -PhysicalPath $ftpRoot | Out-Null
    }
    Set-ItemProperty -Path 'IIS:\Sites\FTP' -Name physicalPath -Value $ftpRoot
    Set-ItemProperty -Path 'IIS:\Sites\FTP' -Name serverAutoStart -Value $true

    # The site's ftpServer element (authentication, SSL policy, external IP)
    # is not exposed as system.ftpServer through the IIS: provider. Edit it
    # through Microsoft.Web.Administration.
    [Reflection.Assembly]::LoadFrom("$env:SystemRoot\System32\inetsrv\Microsoft.Web.Administration.dll") | Out-Null
    $sm = New-Object Microsoft.Web.Administration.ServerManager
    $site = $sm.Sites | Where-Object { $_.Name -eq 'FTP' }
    if (-not $site) { throw 'FTP site not found' }
    $ftp = $site.GetChildElement('ftpServer')
    $security = $ftp.GetChildElement('security')
    $auth = $security.GetChildElement('authentication')
    $auth.GetChildElement('basicAuthentication').SetAttributeValue('enabled', $true)
    $auth.GetChildElement('anonymousAuthentication').SetAttributeValue('enabled', $false)
    $ssl = $security.GetChildElement('ssl')
    $ssl.SetAttributeValue('controlChannelPolicy', 'SslAllow')
    $ssl.SetAttributeValue('dataChannelPolicy', 'SslAllow')
    $ftp.GetChildElement('firewallSupport').SetAttributeValue('externalIp4Address', '192.168.6.96')
    $site.ServerAutoStart = $true
    $sm.CommitChanges()

    # Passive port range is server-level; restart FTPSVC so it takes effect.
    & "$env:SystemRoot\System32\inetsrv\appcmd.exe" set config /section:system.ftpServer/firewallSupport /lowDataChannelPort:50000 /highDataChannelPort:50009 | Out-Null
    Clear-WebConfiguration -PSPath 'IIS:\' -Filter 'system.ftpserver/security/authorization' -Confirm:$false -ErrorAction SilentlyContinue
    Add-WebConfiguration -PSPath 'IIS:\' -Filter 'system.ftpserver/security/authorization' -Value @{ accessType = 'Allow'; users = 'GLEMMEN\ftpuser'; permissions = 3 }
    Restart-Service -Name FTPSVC -Force
    foreach ($rule in @(
        @{ Name = 'FTP-Server-21'; Display = 'FTP Server (TCP 21)'; Port = '21' },
        @{ Name = 'FTP-Passive-50000-50009'; Display = 'FTP Passive (TCP 50000-50009)'; Port = '50000-50009' }
    )) {
        if (-not (Get-NetFirewallRule -Name $rule.Name -ErrorAction SilentlyContinue)) {
            New-NetFirewallRule -Name $rule.Name -DisplayName $rule.Display -Protocol TCP `
                -LocalPort $rule.Port -Direction Inbound -Action Allow -Profile Any -RemoteAddress Any | Out-Null
        }
    }
    icacls 'C:\ftp' /grant 'GLEMMEN\ftpuser:(OI)(CI)RX' /C | Out-Null
    icacls $ftpRoot /grant 'GLEMMEN\ftpuser:(OI)(CI)M' /T /C | Out-Null
    Write-Step "FTP is enabled at $ftpRoot for GLEMMEN\ftpuser"

    # ----------------------------------------------------------------- SQL
    $rebootRequired = $false
    if (-not (Get-Service -Name MSSQLSERVER -ErrorAction SilentlyContinue)) {
        $sqlSetup = $null
        foreach ($drive in Get-PSDrive -PSProvider FileSystem) {
            $candidate = Join-Path $drive.Root 'setup.exe'
            if ((Test-Path $candidate) -and (Test-Path (Join-Path $drive.Root 'resources\1033\setup.rll'))) {
                $sqlSetup = $candidate
                break
            }
        }
        if (-not $sqlSetup) {
            throw 'SQL Server 2025 install media not found on any CD-ROM'
        }
        $sqlArgs = @(
            '/ACTION=Install',
            '/FEATURES=SQLENGINE',
            '/INSTANCENAME=MSSQLSERVER',
            "/SQLSYSADMINACCOUNTS=$netbios\simon",
            '/SECURITYMODE=SQL',
            "/SAPWD=$($secrets.sa_password)",
            '/TCPENABLED=1',
            '/NPENABLED=0',
            '/SQLSVCSTARTUPTYPE=Automatic',
            '/IACCEPTSQLSERVERLICENSETERMS',
            '/SUPPRESSPRIVACYSTATEMENTNOTICE',
            '/Q'
        )
        Write-Step "Installing SQL Server 2025 from $sqlSetup"
        $proc = Start-Process -FilePath $sqlSetup -ArgumentList $sqlArgs -Wait -PassThru
        if ($proc.ExitCode -notin @(0, 3010, 1641)) {
            throw "SQL Server setup failed with exit code $($proc.ExitCode)"
        }
        if ($proc.ExitCode -in @(3010, 1641)) {
            $rebootRequired = $true
        }
        Write-Step "SQL Server setup completed (exit $($proc.ExitCode))"
    }
    Set-Service -Name MSSQLSERVER -StartupType Automatic
    Start-Service -Name MSSQLSERVER
    $sqlcmd = Get-ChildItem -Path 'C:\Program Files\Microsoft SQL Server' -Filter sqlcmd.exe -Recurse -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($sqlcmd) {
        $query = "IF NOT EXISTS (SELECT 1 FROM sys.server_principals WHERE name = N'simon') CREATE LOGIN [simon] WITH PASSWORD = N'$($secrets.simon_password)', CHECK_POLICY = ON; ALTER SERVER ROLE sysadmin ADD MEMBER [simon];"
        & $sqlcmd.FullName -S localhost -U sa -P $secrets.sa_password -C -l 30 -Q $query
        if ($LASTEXITCODE -ne 0) {
            throw "sqlcmd failed with exit code $LASTEXITCODE"
        }
        Write-Step 'Created SQL login simon and granted sysadmin'
    } else {
        Write-Step 'sqlcmd not found; GLEMMEN\simon is still a SQL sysadmin from setup'
    }
    if (-not (Get-NetFirewallRule -Name 'SQL-Server-1433' -ErrorAction SilentlyContinue)) {
        New-NetFirewallRule -Name 'SQL-Server-1433' -DisplayName 'SQL Server (TCP 1433)' `
            -Protocol TCP -LocalPort 1433 -Direction Inbound -Action Allow | Out-Null
    }
    Write-Step 'SQL Server is running'

    # --------------------------------------------------------------- done
    Set-Content -Path 'C:\provision\phase3.done' -Value (Get-Date -Format o)
    Disable-ScheduledTask -TaskName 'WinSrv01-Phase2' -ErrorAction SilentlyContinue | Out-Null
    Disable-ScheduledTask -TaskName 'WinSrv01-Phase3' -ErrorAction SilentlyContinue | Out-Null
    Write-Step 'Phase 3 complete'
    if ($rebootRequired) {
        Write-Step 'SQL Server requested a reboot'
        Restart-Computer -Force
    }
} catch {
    Write-Step "Phase 3 failed: $_"
    $failed = $true
} finally {
    Stop-Transcript
}
if ($failed) {
    exit 1
}
