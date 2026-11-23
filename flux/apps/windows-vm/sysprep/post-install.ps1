# Runs once, at the first Administrator logon, from the sysprep ISO that
# KubeVirt builds from the winsrv01-sysprep Secret.
#
# Installs the virtio-win driver package (including NetKVM) and the QEMU guest
# agent from the virtio ISO, enables RDP, creates a UEFI fallback boot entry,
# stages the phase 2/3 provisioning scripts on C:\provision, registers the
# startup tasks that install AD DS/DNS/IIS/SQL, then reboots.
$ErrorActionPreference = 'Continue'
Start-Transcript -Path C:\post-install.log -Append

function Find-FileOnDrives {
    param([string]$RelativePath)
    foreach ($drive in Get-PSDrive -PSProvider FileSystem) {
        $candidate = Join-Path $drive.Root $RelativePath
        if (Test-Path $candidate) {
            return $candidate
        }
    }
    return $null
}

$virtioMsi = Find-FileOnDrives 'virtio-win-gt-x64.msi'
if ($virtioMsi) {
    Write-Host "Installing virtio drivers from $virtioMsi"
    Start-Process msiexec.exe -ArgumentList "/i `"$virtioMsi`" /qn /norestart" -Wait
} else {
    Write-Host 'virtio-win-gt-x64.msi not found'
}

$guestAgentMsi = Find-FileOnDrives 'guest-agent\qemu-ga-x86_64.msi'
if ($guestAgentMsi) {
    Write-Host "Installing QEMU guest agent from $guestAgentMsi"
    Start-Process msiexec.exe -ArgumentList "/i `"$guestAgentMsi`" /qn /norestart" -Wait
} else {
    Write-Host 'qemu-ga-x86_64.msi not found'
}

# Remote Desktop.
Set-ItemProperty -Path 'HKLM:\System\CurrentControlSet\Control\Terminal Server' `
    -Name 'fDenyTSConnections' -Value 0
Enable-NetFirewallRule -DisplayGroup 'Remote Desktop'

# Allow ICMP echo so the VM can be pinged from the LAN (ICMP is not forwarded
# through the MetalLB RDP service, but this helps inside the cluster/console).
New-NetFirewallRule -DisplayName 'ICMPv4 Echo' -Protocol ICMPv4 -IcmpType 8 `
    -Direction Inbound -Action Allow -ErrorAction SilentlyContinue

# The pod network is classified as an unidentified/public network. Mark it
# private so discovery behaves normally; ignore failures while the NIC driver
# is still loading.
Get-NetConnectionProfile -ErrorAction SilentlyContinue |
    Set-NetConnectionProfile -NetworkCategory Private -ErrorAction SilentlyContinue

# KubeVirt starts OVMF with a fresh NVRAM on every virt-launcher pod. Copy the
# Windows boot manager to the UEFI fallback path so the disk boots without the
# machine-specific NVRAM entry.
try {
    mountvol S: /S 2>$null | Out-Null
    if (Test-Path 'S:\EFI\Microsoft\Boot\bootmgfw.efi') {
        New-Item -ItemType Directory -Force -Path 'S:\EFI\Boot' | Out-Null
        Copy-Item 'S:\EFI\Microsoft\Boot\bootmgfw.efi' 'S:\EFI\Boot\BOOTX64.EFI' -Force
    }
    mountvol S: /D 2>$null | Out-Null
} catch {
    Write-Host "UEFI fallback copy failed: $_"
}

# Stage the phase 2/3 scripts and credentials on the system disk. The tasks
# run as SYSTEM at startup, so no interactive logon is needed after this one.
$source = $PSScriptRoot
if (-not $source) {
    $source = Split-Path -Parent $MyInvocation.MyCommand.Path
}
New-Item -ItemType Directory -Force -Path 'C:\provision' | Out-Null
foreach ($file in @('phase2-ad.ps1', 'phase3-configure.ps1', 'secrets.json')) {
    Copy-Item -Path (Join-Path $source $file) -Destination 'C:\provision' -Force
}
icacls 'C:\provision' /inheritance:r /grant:r 'SYSTEM:(OI)(CI)F' 'Administrators:(OI)(CI)F' | Out-Null

function Register-StartupTask {
    param([string]$TaskName, [string]$ScriptPath)
    $action = New-ScheduledTaskAction -Execute 'powershell.exe' `
        -Argument "-ExecutionPolicy Bypass -NoProfile -WindowStyle Hidden -File `"$ScriptPath`""
    $trigger = New-ScheduledTaskTrigger -AtStartup
    $principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -RestartCount 3 `
        -RestartInterval (New-TimeSpan -Minutes 5) -ExecutionTimeLimit (New-TimeSpan -Hours 2)
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
        -Principal $principal -Settings $settings -Force | Out-Null
}
Register-StartupTask -TaskName 'WinSrv01-Phase2' -ScriptPath 'C:\provision\phase2-ad.ps1'
Register-StartupTask -TaskName 'WinSrv01-Phase3' -ScriptPath 'C:\provision\phase3-configure.ps1'

# Do not leave the password in the AutoAdminLogon registry value.
Set-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon' `
    -Name 'AutoAdminLogon' -Value '0'
Remove-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon' `
    -Name 'DefaultPassword' -ErrorAction SilentlyContinue

# Keep the cached answer file from being picked up again.
if (Test-Path 'C:\Windows\Panther\unattend.xml') {
    Rename-Item 'C:\Windows\Panther\unattend.xml' 'unattend.install.xml' -Force
}

Stop-Transcript
Restart-Computer -Force
