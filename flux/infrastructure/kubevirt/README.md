# KubeVirt

KubeVirt runs virtual machines as Kubernetes workloads. This directory installs
the KubeVirt control plane and the Containerized Data Importer (CDI):

| Component | Version | Namespace | Source |
| --- | --- | --- | --- |
| KubeVirt operator + CRDs | v1.10.0 | `kubevirt` | `kubevirt-operator.yaml` release asset |
| KubeVirt CR | v1.10.0 | `kubevirt` | `cr/kubevirt-cr.yaml` |
| CDI operator + CRDs | v1.66.1 | `cdi` | `cdi-operator.yaml` release asset |
| CDI CR | v1.66.1 | `cdi` | `cr/cdi-cr.yaml` |

The operator manifests are vendored from the upstream release assets so Flux
does not depend on a remote kustomize base:

- <https://github.com/kubevirt/kubevirt/releases/download/v1.10.0/kubevirt-operator.yaml>
- <https://github.com/kubevirt/containerized-data-importer/releases/download/v1.66.1/cdi-operator.yaml>

KubeVirt v1.10 is built for Kubernetes v1.37, matching this cluster.

## Requirements and node placement

KubeVirt needs `/dev/kvm` on the node. Talos includes the KVM host support;
virtualization must be enabled in the BIOS. The nodes are KubeVirt-schedulable
(`kubectl get nodes -l kubevirt.io/schedulable=true`); `winsrv01` currently
runs on `glemmen60` with KVM acceleration. A node with
virtualization disabled would be marked unschedulable unless `useEmulation`
is enabled.

The `kubevirt` namespace is marked privileged in the upstream manifest because
virt-handler mounts host devices. `cdi` is patched to privileged as well; the
virt-launcher and CDI importer pods run in the VM namespace, which is also
privileged.

## Configuration choices

- **Live migration** is enabled on the KubeVirt CR and `workloadUpdateStrategy`
  uses `LiveMigrate`. VM disks are on shared Ceph RBD. `winsrv01` uses
  Hyper-V reenlightenment and is reported as non-live-migratable without a
  TSC frequency; it can be restarted on another node with a brief downtime.
- **CDI scratch space** is `nvme-rbd`. CDI imports images through a scratch
  volume; this avoids installing `local-path-provisioner` just for CDI and
  keeps the 6 GiB Windows ISO off node-local disk.
- **No software emulation.** Nodes without KVM stay unschedulable; the VMs are
  not silently slowed down by TCG.
- **Persistence**: UEFI NVRAM and vTPM persistence (`VMPersistentState`) are
  not enabled. The Windows VM writes a UEFI fallback boot loader so it still
  boots when KubeVirt recreates the virt-launcher pod with fresh OVMF variables.

## Checking the stack

```sh
kubectl -n kubevirt get kubevirt,deploy,ds,pods
kubectl -n cdi get cdi,deploy,pods
kubectl get kubevirt.io/v1 -A
kubectl get cdi.kubevirt.io/v1beta1 -A
```

## Updating

1. Download the new release assets and replace
   `operator/kubevirt-operator.yaml` and `operator/cdi-operator.yaml`.
2. Update the `version`/`sku` in `cr/kubevirt-cr.yaml` and this README.
3. Commit; Flux rolls the operators and live-migrates running VMs when
   `workloadUpdateStrategy` is `LiveMigrate`.

`scripts/check-updates.py` finds the image tags in the vendored operator
manifests and reports newer KubeVirt/CDI releases.
