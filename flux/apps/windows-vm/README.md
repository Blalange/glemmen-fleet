# winsrv01 — Windows Server 2025 on KubeVirt

`flux/apps/windows-vm` defines one Windows Server 2025 VM with the services
below. It runs on KubeVirt; the KubeVirt/CDI stack is in
`flux/infrastructure/kubevirt/`.

| | |
| --- | --- |
| Name | `winsrv01` (namespace `virtualization`) |
| OS | Windows Server 2025 Standard Evaluation (Desktop Experience), en-US |
| Media | Microsoft Evaluation ISO build `26100.1742` |
| CPU / RAM | 2 vCPU / 8 GiB |
| System disk | 64 GiB, `nvme-rbd`, raw block |
| Firmware | UEFI (q35, Secure Boot off) |
| Disk / NIC | virtio-blk / virtio-net |
| Network | KubeVirt pod network (masquerade) |
| LAN VIP | `192.168.6.96` (MetalLB) |

## Services

| Service | Address | Account |
| --- | --- | --- |
| RDP | `192.168.6.96:3389` | `GLEMMEN\simon` or `Administrator` |
| SSH | `192.168.6.96:22` | `GLEMMEN\simon` |
| SQL Server 2025 | `192.168.6.96:1433` | `GLEMMEN\simon`, SQL `simon`, or `sa` |
| DNS | `192.168.6.96:53` UDP+TCP | — |
| FTP | `192.168.6.96:21`, passive `50000-50009` | `GLEMMEN\ftpuser` |
| Active Directory | forest `ad.glemmen.internal` | `GLEMMEN\simon` (Domain Admin), `GLEMMEN\ftpuser` |

The DNS server is authoritative for the AD zone `ad.glemmen.internal` and an
internal forward zone `simon.no` with an apex A record pointing to
`192.168.6.96`. It forwards everything else to `192.168.6.1` and `1.1.1.1`.
This does not change public DNS for `simon.no`.

**AD caveat:** the VM is behind KubeVirt's masquerade NAT. The domain
controller itself works and `GLEMMEN\simon`/`GLEMMEN\ftpuser` can authenticate
to the services above, but AD registers its own address as `10.0.2.15` in the
AD DNS zone. LAN machines cannot join `ad.glemmen.internal` or use the DC for
LDAP/Kerberos without a direct LAN interface (Multus/macvlan). The `simon.no`
zone and the other service ports are exposed through the VIP.

SQL Server is **2025 Enterprise Developer** (free for development/testing, not
licensed for production), default instance `MSSQLSERVER`, mixed-mode
authentication, TCP 1433. `GLEMMEN\simon` is a SQL sysadmin from setup; the
script also creates a SQL login `simon` (same password as the AD user) and the
`sa` login. Database files live on the 64 GiB system disk; add a data disk for
anything large.

FTP is IIS FTP with basic authentication (no anonymous access), root
`C:\ftp\ftpuser`. `GLEMMEN\ftpuser` has read/write on that directory and is
**not** in Remote Desktop Users or `ssh-users`. The data channel is plain FTP
(no FTPS); use SFTPGo or another encrypted service if the data needs
encryption.

## First boot

1. CDI imports the Windows ISO (5.6 GiB), virtio-win ISO (0.9 GiB) and SQL
   ISO (1.2 GiB) into `nvme-rbd` volumes.
2. The blank root disk causes UEFI to boot the Windows ISO. Setup runs
   unattended: Standard Evaluation (Desktop Experience), EFI/MSR/NTFS,
   `winsrv01`, `W. Europe Standard Time`. It loads `viostor` from the virtio
   ISO.
3. At the first Administrator logon, `post-install.ps1` installs the virtio
   drivers and QEMU guest agent, enables RDP, writes the UEFI fallback boot
   loader, stages the phase 2/3 scripts in `C:\provision`, registers startup
   tasks and reboots. AutoAdminLogon is disabled.
4. At startup, `phase2-ad.ps1` installs AD DS, DNS, IIS/FTP, RSAT and OpenSSH,
   points DNS at itself, promotes a new forest and reboots.
5. At the next startup, `phase3-configure.ps1` creates the AD users/groups,
   the `simon.no` DNS zone, SSH, the FTP site, and installs/configures SQL
   Server. It disables its tasks after success.
6. RDP/SSH/SQL/DNS/FTP are available on `192.168.6.96`.

The phase scripts log to `C:\provision\phase2.log` and
`C:\provision\phase3.log`. SQL setup logs live under
`C:\Program Files\Microsoft SQL Server\*\Setup Bootstrap\Log`. On failure the
startup task stays enabled and retries; the built-in Administrator can log in
over RDP the whole time.

The Windows evaluation is valid for 180 days. Activate with a retail/volume
key in the guest when you have one:

```powershell
slmgr /ipk <product-key>
slmgr /ato
```

## Credentials (not in Git)

The answer file and the AD/SQL bootstrap passwords live only in the
`winsrv01-sysprep` Secret in the cluster. The Secret has these keys:

| Key | Contents |
| --- | --- |
| `autounattend.xml` | Windows Setup answer file, including the Administrator password |
| `secrets.json` | `simon_password`, `ftp_password`, `sa_password`, `dsrm_password` |
| `post-install.ps1` | First-logon script |
| `phase2-ad.ps1` | Role install + AD DS promotion |
| `phase3-configure.ps1` | Users, DNS, SSH, FTP, SQL |

Create or rotate it with the values you want:

```sh
ADMIN_PASSWORD='...'
SIMON_PASSWORD='...'
FTP_PASSWORD='...'
SA_PASSWORD='...'
DSRM_PASSWORD='...'

sed "s|__ADMIN_PASSWORD__|${ADMIN_PASSWORD}|g" \
  flux/apps/windows-vm/sysprep/autounattend.xml.tpl > /tmp/autounattend.xml
sed -e "s|__SIMON_PASSWORD__|${SIMON_PASSWORD}|g" \
    -e "s|__FTP_PASSWORD__|${FTP_PASSWORD}|g" \
    -e "s|__SA_PASSWORD__|${SA_PASSWORD}|g" \
    -e "s|__DSRM_PASSWORD__|${DSRM_PASSWORD}|g" \
  flux/apps/windows-vm/sysprep/secrets.json.tpl > /tmp/secrets.json

kubectl -n virtualization create secret generic winsrv01-sysprep \
  --from-file=autounattend.xml=/tmp/autounattend.xml \
  --from-file=secrets.json=/tmp/secrets.json \
  --from-file=post-install.ps1=flux/apps/windows-vm/sysprep/post-install.ps1 \
  --from-file=phase2-ad.ps1=flux/apps/windows-vm/sysprep/phase2-ad.ps1 \
  --from-file=phase3-configure.ps1=flux/apps/windows-vm/sysprep/phase3-configure.ps1 \
  --dry-run=client -o yaml | kubectl apply -f -

rm -f /tmp/autounattend.xml /tmp/secrets.json
```

Retrieve them later:

```sh
kubectl -n virtualization get secret winsrv01-sysprep \
  -o jsonpath='{.data.secrets\.json}' | base64 -d | jq
kubectl -n virtualization get secret winsrv01-sysprep \
  -o jsonpath='{.data.autounattend\.xml}' | base64 -d |
  grep -A2 AdministratorPassword
```

The Secret is only read when KubeVirt builds the sysprep CD at VM start. If you
change it before the first boot, restart the VMI (`kubectl -n virtualization
delete vmi winsrv01`) so the new files take effect. Rotating a password after
installation must be done in AD/SQL, not by editing the Secret.

The sysprep CD remains attached and contains `secrets.json`; remove the
`sysprep` volume from `vm.yaml` after provisioning if that matters. The
`virtio` and `sql` CDs can also be removed once the install is stable.

## Day-2 commands

```sh
kubectl -n virtualization get vm,vmi,dv,pvc,svc
kubectl -n virtualization get vmi winsrv01 -o wide
kubectl -n virtualization describe vm winsrv01
kubectl -n virtualization logs -f <virt-launcher-pod> -c compute
```

Console (needs `virtctl` from the KubeVirt release page):

```sh
virtctl vnc winsrv01 -n virtualization
```

Useful client commands:

```sh
ssh simon@192.168.6.96
sqlcmd -S 192.168.6.96,1433 -U simon -P '<simon-password>'
sqlcmd -S 192.168.6.96,1433 -U sa -P '<sa-password>'
ftp 192.168.6.96            # passive; user GLEMMEN\ftpuser
dig @192.168.6.96 simon.no  # -> 192.168.6.96
```

RDP host `192.168.6.96`, user `GLEMMEN\simon` (or `GLEMMEN\Administrator`,
the built-in domain Administrator with the same password as the local
Administrator before promotion).

## Changing the VM

- **More CPU/RAM/disk**: edit `vm.yaml` (CPU/memory) or `pvc.yaml` (disk;
  `nvme-rbd` supports expansion). Resize the Windows volume inside the guest.
- **More ports**: add them to the `winsrv01` Service in `service.yaml`. The
  MetalLB pool also has `.97` if a separate VIP is needed.
- **FTP passive range**: if you change 50000-50009, update both
  `phase3-configure.ps1` and the Service.
- **Reinstall Windows**: delete the `winsrv01` VM and the `winsrv01-root` PVC;
  Flux recreates the PVC and the VM starts from the ISOs again. The ISO
  DataVolumes are kept.
- **Re-run provisioning**: in the guest as Administrator:

  ```powershell
  Remove-Item C:\provision\phase3.done
  Enable-ScheduledTask WinSrv01-Phase3
  Start-ScheduledTask WinSrv01-Phase3
  ```

  The scripts are idempotent.
- **Different Windows edition**: change `/IMAGE/INDEX` in
  `autounattend.xml.tpl` (1 Standard Core, 2 Standard Desktop, 3 Datacenter
  Core, 4 Datacenter Desktop).

## Security notes

- `GLEMMEN\simon` is in Domain Admins so it can administer the VM, SQL and AD.
  Remove it from Domain Admins and use delegated groups if that is too broad.
- FTP is unencrypted. RDP, SSH, SQL, DNS and FTP are reachable from the server
  VLAN on `192.168.6.96`; restrict them at the network edge if needed.
- The SQL Server Developer edition is not licensed for production.
- The Windows Server evaluation expires after 180 days.
- `winsrv01-sysprep` contains bootstrap passwords. Access to the
  `virtualization` namespace or the Secret is equivalent to access to the VM.
