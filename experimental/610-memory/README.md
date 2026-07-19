# Experimental CMP 170HX 610.43.03 memory path

This directory contains a quarantined, fail-closed reconstruction of the
community NVIDIA 610.43.03 kernel-module approach for one CMP 170HX with PCI ID
`10de:20c2`. Its target is a software-visible 64-GiB framebuffer, followed by a
test of every address within a 60-GiB allocation suitable for deciding whether
to *trial* a local LLM.

This is not a hardware guarantee. The project has not executed this path on a
CMP 170HX. A capacity number from `nvidia-smi` is only a metadata observation;
the final `LLM_READY` gate additionally requires non-aliased reads across the
tested range and clean health checks. Even `LLM_READY` covers only the patterns,
temperature, and duration actually tested.

## Exact compatibility boundary

The scripts accept only:

- native Linux x86-64;
- exactly one NVIDIA PCI function, device `10de:20c2`;
- NVIDIA open driver, NVIDIA-SMI, NVML, and firmware point release `610.43.03`;
- official `gsp_tu10x.bin`, size 29,352,832 and SHA-256
  `73065619db9ec921d19fc4e519dd04d91a9199b525eaca9b257b89fb8c5ec52c`;
- NVIDIA open-module commit
  `452cec62d827034798072827d3866d1881662b77`;
- the running kernel and its exact build headers;
- disabled Secure Boot and disabled kernel lockdown.

The manifest also records NVIDIA's official 610.43.03 runfile SHA-256, but no
runfile, firmware, object, screenshot, or prebuilt module is stored here. The
operator installs the official stock 610.43.03 stack separately. Do not copy the
supplied `other-project` build tree into this directory.

Use a disposable host with out-of-band console access and a controllable AC
power source. The passive CMP heatsink needs server-grade forced airflow. Do not
use the card for display, do not attach another NVIDIA function, and do not run
this from WSL, a container, or a virtual machine.

## Prerequisites

Install the normal build tools and running-kernel headers appropriate for your
distribution. The workflow needs `git`, GNU `make`, a supported compiler,
`sha256sum`, `readelf`, `kmod`, util-linux `flock`, `mokutil` on EFI systems,
and one supported initramfs implementation. CUDA `nvcc` is needed for the
memory validator. The scripts diagnose missing commands; they never invoke a
package manager.

For transactional installation/removal, the target's
`/lib/modules/$(uname -r)/updates` directory and
`/var/lib/cmpunlocker-610-memory/archives` must be on the same filesystem. The
scripts refuse a cross-filesystem move because it would not be atomic. The
supported initramfs combinations are `update-initramfs` with `lsinitramfs`, or
`dracut` with `lsinitrd`; other generators are refused.

Before building, the unmodified 610.43.03 open driver must load the card and this
must succeed:

```bash
nvidia-smi
nvidia-smi --version
modinfo -F version nvidia
cat /proc/driver/nvidia/version
lspci -Dnn | grep -i NVIDIA
```

Expect exactly one `10de:20c2` function, NVIDIA-SMI/NVML/module version
`610.43.03`, and `Open Kernel Module` in the proc version. If stock 610.43.03
cannot initialize this card, stop; the experiment does not repair an unknown or
mixed base installation.

The scripts also require the loaded core module's `srcversion` to match the
resolved on-disk predecessor and record all five predecessor paths and hashes.
They do not have an NVIDIA-published hash for locally built/distribution-packed
`.ko` files, so “stock predecessor” means that exact recorded open-module set
meeting the version, identity, resolution, and native-8192-MiB gates—not a
cryptographic proof that its module bytes came from NVIDIA unchanged.

## Build without root

Run from this directory as your normal user:

```bash
./build.sh --dry-run

./build.sh \
  --acknowledge I-ACCEPT-UNVERIFIED-610-MEMORY-KERNEL-BUILD

./build-validator.sh
```

`build.sh` initializes a new temporary Git checkout, fetches the exact commit by
object ID, verifies the local patch SHA-256, applies it with whitespace checks,
and builds five open modules for `uname -r`. It records the source, patch,
kernel, target BDF, GSP digest, and every module digest under
`artifacts/$(uname -r)/`. It does not install anything. The build tree remains
under `.work/` for audit.

`build-validator.sh` compiles only the fixed sibling CUDA source for GA100
(`sm_80`). It accepts no alternate source or command. The resulting binary and
temporary/previous copies are ignored by Git.

## Install without hot-loading

Review the dry run, then install the verified artifact:

```bash
sudo ./install.sh --dry-run

sudo ./install.sh \
  --acknowledge I-ACCEPT-UNVERIFIED-610-MEMORY-KERNEL-INSTALL
```

The installer copies only the five fixed module names into
`/lib/modules/$(uname -r)/updates/cmpunlocker-610-memory`, verifies their hashes,
runs `depmod`, confirms that `modprobe` resolves the isolated `nvidia.ko`, and
rebuilds the current kernel's initramfs. The rebuilt image may omit NVIDIA
modules on a non-early-boot/host-only configuration; if it contains any of the
five NVIDIA module basenames for this kernel, every occurrence must be the
isolated path and must be unique. A competing stock copy is a fatal rollback.
The installer refuses an existing isolated target, active install state, or
pending removal; remove an earlier installation before reinstalling. The exact
pre-install initramfs image is backed up under
`/var/lib/cmpunlocker-610-memory`. A rollback remains armed until the module
directory, module database, initramfs, and durable install state have all
committed.

The installer never unloads or loads a module. It never stops a service, kills a
process, changes userspace or firmware, installs a package, runs NVIDIA's
installer, blacklists a driver, changes PCIe state, or reboots the host.

Every install preflight (including `--dry-run`) takes the same exclusive,
nonblocking `/var/lib/cmpunlocker-610-memory/operation.lock` used by validation,
removal, and cold-cycle confirmation. The first preflight may create only the
root-owned 0700 state root and 0600 lock file; the first operation on a legacy
pre-lock state tree may create that lock file too. A concurrent operation fails
before reading or changing experiment state; validation retains the lock
through the complete CUDA stress run.

When it finishes, shut down cleanly, remove AC power until the board loses power,
then cold-start. This is a physical step; a warm reboot is not equivalent.

## Validate before any LLM

Stop every GPU workload. With forced airflow already running:

```bash
sudo ./validate.sh --preflight-only \
  --cold-cycle-acknowledge I-CONFIRM-FULL-AC-POWER-CYCLE-AFTER-610-MEMORY-INSTALL

sudo ./validate.sh --passes 5 \
  --cold-cycle-acknowledge I-CONFIRM-FULL-AC-POWER-CYCLE-AFTER-610-MEMORY-INSTALL \
  --acknowledge I-ACCEPT-UNVERIFIED-610-MEMORY-STRESS-AND-CONFIRM-FORCED-AIRFLOW
```

The wrapper reads the durable install state, requires a boot ID different from
the install boot, and requires the operator's explicit assertion that this was
a full AC power cycle. It pins the five-module checksum manifest, verifies that
all five names resolve to the isolated directory, loads `nvidia_uvm` if CUDA has
not already loaded it, and requires the loaded core and UVM modules to match the
verified files. It delegates only to `tools/validate-memory.sh`,
which selects the CUDA device by PCI BDF and requires the stress acknowledgement
itself. Full validation refuses fewer than three passes and defaults to five.

Before allocation, the validator requires:

- the installed and loaded `nvidia.ko` version, source version, path, open-module
  identity, and SHA-256 to match;
- the GSP firmware reported as loaded for the target to be version `610.43.03`,
  in addition to the exact on-disk GSP size and SHA-256 gate;
- exactly one NVIDIA PCI function and exact `10de:20c2` sysfs identity;
- current-boot `cmpunlock610:` markers for the device gate, all four PLMs, all
  four host writes, the completed unlock, exact 64-GiB metadata, and PMA total;
- PMA-managed capacity of at least 60 GiB;
- `nvidia-smi` capacity of exactly 65,536 MiB, an idle compute-process list,
  and temperature no higher than 75 C by default;
- no current-boot unlock failure, incomplete rollback, Xid, or fallen-off-GPU
  marker.

It then allocates fixed stages of 8, 16, 32, 48, and 60 GiB. Each pass writes and
reads three full-allocation bijective, address-dependent 64-bit patterns. An
aliased high address cannot simultaneously retain the distinct value expected
at its low alias. The tester returns a separate mismatch status, reports a
representative mismatch, records exact mismatch totals, and treats every CUDA or
allocation error as failure. Health, temperature, logs, module hash, and output
contracts are checked again throughout and at the end.

Only the complete success path prints:

```text
LLM_READY
```

Logs default to `/var/log/cmp170-memory-validation`. Preserve the entire printed
run directory. It includes a copy and hash of the root-owned install state and
five-module checksum manifest; a table of every resolved module path, hash,
version, source version, and loaded state; NVIDIA-SMI/NVML plus loaded/on-disk
GSP identity; current and install boot IDs; kernel logs; temperature samples;
and every stage result. Increasing `--passes` up to 10 provides a longer check,
but no finite pattern test proves lifetime reliability.

## LLM sizing after `LLM_READY`

Run the inference engine normally as a non-root user. Treat 60 GiB as the tested
allocation ceiling. Leave several GiB for the CUDA context, kernels, allocator
fragmentation, workspaces, and KV cache; do not select weights that nearly equal
the `65536 MiB` display. Roughly 55-58 GiB of weights is an upper planning bound,
not a promised usable amount. Context length and concurrency can materially
increase KV-cache use.

Watch temperature and kernel health during first sessions:

```bash
nvidia-smi --query-gpu=pci.bus_id,memory.used,temperature.gpu,power.draw \
  --format=csv -l 2
sudo dmesg --follow | grep -Ei 'NVRM|Xid|cmpunlock610'
```

Stop immediately for a mismatch, Xid, CUDA error, temperature limit, incorrect
model output, or driver reset. The paper reports that its separate 10-to-80-GB
sample needed a more frequent refresh setting to become stable, with a throughput
penalty. That register recipe is not published or implemented here, so there is
no automatic fallback if this 8-to-64-GiB card fails stress.

## Reversible removal

Removal changes the on-disk next-boot module selection but deliberately leaves
the running driver alone:

```bash
sudo ./remove.sh --dry-run

sudo ./remove.sh \
  --acknowledge REMOVE-CMPUNLOCKER-610-MEMORY-WITHOUT-HOT-UNLOAD
```

The experiment directory is atomically archived under
`/var/lib/cmpunlocker-610-memory`, the exact recorded stock predecessor set is
reselected, `depmod` and initramfs are rebuilt, and a
pending cold-cycle record is written. Do not keep using the running GPU after
this command. Shut down, physically remove AC power, then cold-start and record
that boundary:

```bash
sudo ./remove.sh --confirm-cold-cycle \
  --acknowledge I-CONFIRM-FULL-AC-POWER-CYCLE-AFTER-610-MEMORY-REMOVAL
```

The confirmation requires a different boot ID, the exact stock 610.43.03 stack,
the recorded predecessor modules (loading the verified stock `nvidia_uvm` if
needed), and native 8-GiB capacity before it archives the pending record. Its
acknowledgement is still the operator's physical assertion;
software alone cannot distinguish every warm reboot from removal of board power.

## Before system or NVIDIA updates

Remove the experiment and complete the cold-cycle confirmation **before**
changing the kernel, NVIDIA driver/userspace/firmware, or initramfs tooling. The
durable state is tied to the running kernel and exact hashed predecessor paths;
changing them first can make safe removal refuse, and later booting the old
kernel can select its still-installed experiment again.

If an update was already applied, boot the recorded old kernel and restore its
exact predecessor NVIDIA stack and initramfs tooling, then run the documented
removal and cold-cycle confirmation there. Do not manually delete the isolated
module directory or `/var/lib/cmpunlocker-610-memory` state. After confirmed
removal, update normally and build a new artifact for the new running kernel.

## What the patch changes

For `20c2` only, the patch:

1. allocates the community continuation internally and uses the authentic signed
   Booter repeatedly to open WPR-configuration, FBPA, WPR, and feature PLMs;
2. gates SS0, SS1, CFG1, and LMR host writes on exact PLM readback and gates all
   later capacity publication on exact host readback;
3. rebuilds the authentic stock signature before normal GSP boot and frees the
   saved signature on every lifetime path;
4. validates and publishes 64-GiB GSP metadata only after those gates;
5. retains the native 8-GiB PRAMIN window and avoids unsupported high-range
   compressed/virtual scrub behavior;
6. validates framebuffer/PMA array capacity and registers only the intended high
   range, requiring a final PMA total of at least 60 GiB;
7. best-effort restores partial host writes and emits
   `COLD_POWER_CYCLE_REQUIRED` if restoration is incomplete;
8. marks all success and failure transitions with stable `cmpunlock610:` log
   records for the validator.

Every successful non-`20c2` path remains stock. The only generic change is a
minimal failure cleanup in the shared PMA registration function: for the
synchronous late registration used here, an allocation or blacklist failure no
longer dereferences a null descriptor or leaves published PMA slots/accounting.
It does not alter successful registration or claim generic async-scrub rollback.
See [`PATCH_NOTES.md`](PATCH_NOTES.md) for the source-level map.

The patch intentionally retains NVIDIA's register-operation validator, rejects
an external `dmem.bin`, does not weaken WPR2 handling globally, and contains no
PCIe/root-port, ECC, NVLink, refresh, package, daemon, or process-management
logic.

## Development checks

These checks do not require a GPU:

```bash
bash tests/static.sh
bash -n build.sh build-validator.sh install.sh lib.sh remove.sh validate.sh \
  tools/validate-memory.sh
python -m pytest -q ../../tests/test_610_memory_tools.py
```

The definitive build check is `build.sh` on a native Linux host with matching
headers. The definitive capacity result is the hardware validator, not successful
compilation.
