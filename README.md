# CMP 170HX GA100 unlock research tool

This repository implements and audits the host-side mechanism described in
[`main.pdf`](main.pdf), *A Canary in the Crypto Mine: Defeating Stack Protection
in a GPU Secure Coprocessor*. It is an offline-first, fail-closed research tool,
not a certified or independently reproduced hardware unlock.

The evidence has three distinct levels:

1. The paper's offline proof-of-control payload is reproduced exactly.
2. The productive compute continuation and register values come from the public
   [`fulracoco/cmpunlocker`](https://github.com/fulracoco/cmpunlocker) prototype and
   remain community-derived and unverified.
3. No CMP 170HX hardware throughput result has been reproduced by this project.

Do not interpret a successful firmware patch, PLM readback, or register readback
as proof of a working or stable compute unlock. The final proof is a correct
before/after compute benchmark on the target hardware with adequate cooling.

## Quick install

On Linux, Python 3.10 or newer and the Python `venv` module are required. Run the
installer as your normal user, not with `sudo`:

```bash
./install.sh
.venv/bin/cmpunlock profile list
```

`install.sh` replaces the previous virtual-environment setup commands. It creates
or reuses `.venv`, installs this checkout in editable mode, and runs an offline
smoke test. It does not inspect a GPU, invoke `sudo`, require root, unload a
module, patch firmware, enable systemd, or execute the experimental path.

The first package build needs access to `setuptools>=77`, either from the network
or a configured local wheel cache. If virtual-environment creation fails on
Debian or Ubuntu, install the matching `python3-venv` package and rerun the script.
An incomplete existing `.venv` is never deleted automatically.

For offline-only use on Windows:

```powershell
py -3 -m venv .venv
.venv\Scripts\python -m pip install -e .
.venv\Scripts\cmpunlock profile list
```

BAR0, driver, recovery, and state commands are Linux-only.

## What is verified

- NVIDIA's GA100 Booter Load is Falcon v6. Its clear stub writes and reads DMEM
  `0x6340`, independently corroborating that address. The encrypted application
  prevents an independent check of its RNG reseeding or vulnerable epilogue use.
- NVIDIA's 580 open kernel code obtains the GA100 signature pointer and length
  from `.fwsignature_ga100` and propagates that length into WPR metadata.
- The paper's proof image is reproducible: `0xf800` bytes made from little-endian
  dword `0x4a7`. Under the paper's reported DMEM/stack layout, this makes the
  overwritten guard, saved copy, and return PC equal.
- Bundled profiles pin exact authentic `gsp_tu10x.bin` images, signature layout,
  point release, and embedded GA100 production-booter SHA-256.
- The ELF patcher preserves `.fwimage`, `.fwversion`, build ID, both Turing
  signatures, symbol/string tables, and every other file-backed section. It
  reparses the result and rejects overlaps.
- Tests exercise both official 580 firmware images, payload layout, malformed
  ELF input, BAR0 bounds, exact live-profile allowlists, transaction locking,
  atomic restoration, recovery validation, pre-boot audit durability, and
  failure paths. Live transaction tests use mocks, not a physical GPU.

## What is not verified

- Productive gadget `0x10b9` and tail `0x810d` lie inside authenticated,
  encrypted Falcon IMEM. The DMEM frame start `0xff48`, stride `0x18`, and
  replacement canary `0xfaceb13d` likewise have no published emulator
  derivation. No decrypted image or emulator was published.
- PLM `0x823804` and compute overrides `0x82381c`/`0x823820`. The paper omits
  the full register map; these values are inherited from the public prototype.
- The prototype's two other HS writes (`0x9a0204 <- 0x02779000` and
  `0x100ce0 <- 0x20b`). Their purpose and safety are undocumented, so this repo
  does not execute them. Repeating the compute PLM write preserves the community
  chain length but does not prove the resulting continuation works.
- The paper's driverless Falcon loader. Both public codebases instead enlarge
  the installed GSP signature section and use `modprobe nvidia`; this is a
  plausible alternative delivery path, not an exact reproduction of the paper.
- Stock open-driver binding for CMP IDs `2082` and `20c2`. They are absent from
  NVIDIA's 580.126.09 compatible-GPU table. A host whose installed module does
  not already bind the card cannot complete this workflow.
- Memory capacity, PCIe, ECC, or NVLink changes. They are intentionally not
  implemented. The paper did not defeat PCIe Gen3, ECC, or runtime HBM mode
  register programming, and its 80 GB result needed a refresh/performance tradeoff.
- PCI ID `20b0`. It identifies an A100 SXM4 40 GB, not a CMP 170HX, and is
  unconditionally rejected even if a custom profile lists it.

## Cross-check against the paper

| Mechanism | Published in `main.pdf` | Implemented here | Evidence |
|---|---:|---:|---|
| DMA destination `0x800` | Yes, Section 5.5 | Profiled/assumed | Closed-booter behavior unverified |
| DMA length `0xf800` | Yes, Section 5.5 | Host section constructed | Metadata propagation statically checked |
| Guard address `0x6340` | Yes, Sections 5.2-5.5 | Payload layout encoded | Role unverified beyond clear-stub address corroboration |
| Uniform proof dword `0x4a7` | Yes, Section 5.5 | Yes, `--mode proof` | Exact offline reproduction |
| Productive continuation | Described, bytes omitted | Community-derived | Unverified |
| Full PLM/register map | Explicitly omitted | Compute-only community values | Unverified |
| Driverless loader/emulator | Used, not published | Not included | Missing reproduction artifact |
| SM throughput result | Reported in Table 2 | Benchmark included, no result | Not reproduced |

The PDF also omits an exact firmware build, point-release hash, decrypted IMEM,
raw emulator-trace artifacts, identifiable silicon mailbox logs, benchmark logs,
checker source/raw output and the analyzed-image corpus, and the referenced
verification appendix. Those omissions make it impossible to certify either
repository as a working hardware
reproduction from the paper alone. See
[`docs/STUDY_NOTES.md`](docs/STUDY_NOTES.md) for the detailed audit.

## Comparison with `fulracoco/cmpunlocker`

The comparison was made against upstream commit
[`9b9fb2f`](https://github.com/fulracoco/cmpunlocker/commit/9b9fb2f27a618f13e6b016adfc6e86b1e60fa84d).
This repository had already integrated the useful GPL-2.0 payload layout and
compute constants with attribution, then substantially rewrote the implementation.

| Area | This repository | Public prototype |
|---|---|---|
| Compatibility | Exact GSP, section, version, and booter hashes | Unhashed wildcard: version-sorted by its installer, reverse-lexicographic in its default runtime lookup; its README claims `580.x` |
| Hardware gate | Only CMP `2082`/`20c2`; fixed live contract | Also accepts A100 `20b0` |
| Firmware patch | Relocates overlaps and verifies preservation | Overwrites adjacent ELF sections |
| Failure handling | Backup, journal, atomic restore, lock, state record | Plain copies; no guaranteed `finally` restore |
| Driver handling | Requires operator to unload cleanly | Stops services, kills GPU users, may force removal |
| Persistence | No daemon; state must survive FLR naturally | Intended root watchdog polls each second, but the published missing-key bug prevents its reapply path |
| Claims | Hardware-unverified | Advertises full compute as working |
| Tests | Payload, official images, system/recovery failure paths | Compile/import and YAML top-level keys only |

The upstream runtime pipeline is not merged because:

- `unlock/compute.py` requests `host_bar0_writes.feat_ovr_plm`, but that key is
  absent from `constants.yaml`; the advertised unlock therefore fails at runtime.
- That exception occurs before its firmware restore and there is no `finally`,
  so the installed GSP can remain patched.
- Its patcher destroys overlapping Turing signature and symbol/string sections
  in both exact official firmware fixtures.
- Its installer immediately attempts the unverified path and, if execution
  reaches those steps without an exception, would enable a root daemon after
  destructive process/module handling.

Only the installer ergonomics were adopted: this repo's new installer is
setup-only and stops before all hardware operations. The two undocumented HS
writes were deliberately not imported.

## Supported inputs and live prerequisites

Bundled profiles currently support exact stock firmware from:

- NVIDIA `580.105.08`, SHA-256
  `84e0f47adc5b7f40a5789f1e3d528ca1269bd6184029dec0af6c76f9f282d0d7`
- NVIDIA `580.126.09`, SHA-256
  `a3788bfb368bdd2384a8b1aceeb946f2b0e1dff734d9f3fdca65e7f727ed42b7`

Unknown or modified firmware is rejected. `580.x` is not treated as a compatible
family.

A live test host additionally needs:

- Linux x86-64, root for live apply/recovery, readable PCI sysfs, writable
  `resource0`, and PCI function-level reset support;
- `kmod` tools (`modinfo` and `modprobe`), plus `zstd` when `nvidia.ko` is
  `.ko.zst` compressed;
- `pciutils` (`lspci`) and `psmisc` (`fuser`) for the documented preflight;
- one supported CMP card as the only NVIDIA PCI function in the host;
- an exact matching NVIDIA module, GSP firmware, and userspace release, with a
  stock `nvidia` driver that already binds the CMP ID;
- out-of-band console, complete cold-power-cycle control, and server-grade
  forced airflow for the passive card;
- CUDA toolkit, `nvcc`, and cuBLAS only for the final benchmark.

Use a disposable host with no valuable data and no other NVIDIA GPU. Do not use
the CMP card as a boot or display device.

## Offline firmware workflow

Inspect an exact stock image. The profile is selected by SHA-256:

```bash
.venv/bin/cmpunlock firmware inspect /path/to/gsp_tu10x.bin
```

Build the paper's proof-of-control image. The paper reports that this image spins
at `0x4a7`; this project reproduces the bytes but has not executed that control
path. It is not an unlock image and intentionally does not resume the driver:

```bash
.venv/bin/cmpunlock firmware patch /path/to/gsp_tu10x.bin \
  ./gsp.proof.bin --mode proof
```

Build the community-derived compute-only image:

```bash
.venv/bin/cmpunlock firmware patch /path/to/gsp_tu10x.bin \
  ./gsp.compute.bin --mode compute
```

The input is never overwritten, even with `--force` or when the output is a
symlink/hardlink alias. Forced replacement of a distinct output is atomic. The
tool prints stock, payload, and output hashes plus every relocated ELF section.

## Server preflight

Locate the card, module, and matching installed firmware:

```bash
lspci -Dnn | grep -i NVIDIA
modinfo -F version nvidia
modinfo -n nvidia
find /lib/firmware/nvidia -name gsp_tu10x.bin -print
```

Use one complete point release; do not mix a kernel module, GSP, or userspace
libraries from different releases. Confirm `lspci -k` reports `Kernel driver in
use: nvidia` for the CMP before continuing.

Run the read-only validator while the stock driver is loaded. Replace the BDF
and firmware path with values from the host:

```bash
sudo .venv/bin/cmpunlock system inspect 0000:41:00.0 \
  --firmware /lib/firmware/nvidia/580.126.09/gsp_tu10x.bin

.venv/bin/cmpunlock system plan 0000:41:00.0 \
  --firmware /lib/firmware/nvidia/580.126.09/gsp_tu10x.bin
```

The live commands accept only unchanged bundled profiles and a hard-coded
compute-only device/register contract. Custom JSON remains available for offline
research but cannot authorize new live IDs, control-flow values, or BAR0 writes.

## Experimental compute run

Stop your scheduler, persistence daemon, display manager, and GPU workloads.
This project deliberately does not stop services or kill processes for you.
Confirm no process owns an NVIDIA device, then unload every NVIDIA module cleanly:

```bash
sudo fuser -v /dev/nvidia* /dev/nvidia-caps/*
sudo modprobe -r nvidia_uvm nvidia_drm nvidia_modeset nvidia_peermem nvidia
grep '^nvidia' /proc/modules
```

`fuser` and `grep` should show no remaining clients/modules. Execute only after
reading this README and the study notes:

```bash
sudo .venv/bin/cmpunlock system apply 0000:41:00.0 \
  --firmware /lib/firmware/nvidia/580.126.09/gsp_tu10x.bin \
  --execute \
  --acknowledge UNVERIFIED-CMP170HX-EXPERIMENT
```

Before mutation, the transaction refuses a PLM already at the expected all-open
value, records baseline PLM/compute registers, verifies the CMP ID, exact
firmware/module/booter, single GPU, unloaded modules, and FLR availability. It
then:

1. writes a durable stock backup and transaction journal;
2. atomically installs the patched GSP;
3. fsyncs an audit/state record before attempting the patched driver boot;
4. requires a changed PLM readback consistent with the community continuation;
5. writes and verifies only the two allowlisted compute-rate overrides;
6. unloads cleanly, restores stock in `finally`, performs FLR, loads stock, and
   verifies that the override values persisted.

No readback proves throughput or proves how the PLM changed. It is only a gate
consistent with the expected continuation, followed by mandatory benchmarking.

## Recovery and cold-cycle state

If power or the process is lost while patched firmware is installed, boot through
the out-of-band console and run:

```bash
sudo .venv/bin/cmpunlock system recover \
  --firmware /lib/firmware/nvidia/580.126.09/gsp_tu10x.bin
sudo modprobe nvidia
```

Recovery accepts only the exact expected backup path and a stock image that
matches a bundled profile. It rejects mismatched paths, unknown digests, and
tampered backup bytes.

Once a patched boot may have occurred, a hidden `*.cmpunlock-state.json` remains
beside the firmware. A later apply refuses to overwrite that evidence. The paper
only identifies a power cycle that clears the always-on island as the clearing
boundary, so conservatively do not treat a warm reboot as sufficient. Complete a
real cold power cycle, then clear the record with an explicit acknowledgement:

```bash
sudo .venv/bin/cmpunlock system state-clear \
  --firmware /lib/firmware/nvidia/580.126.09/gsp_tu10x.bin \
  --acknowledge COLD-POWER-CYCLE-COMPLETED
```

`state-clear` validates that the on-disk firmware is authentic stock before
removing the record. Software cannot prove that a reboot was a complete cold
power cycle; the acknowledgement records the operator's physical confirmation.

## Verify compute and cooling

Compile the included pedantic-FP32 cuBLAS benchmark and compare correct results
before and after the experiment:

```bash
nvcc -O3 tools/sgemm_bench.cu -lcublas -o sgemm_bench
nvidia-smi --query-gpu=pci.bus_id,name,temperature.gpu,power.draw,clocks.sm \
  --format=csv -l 1
./sgemm_bench 8192 20
```

The paper reports roughly 0.393 TFLOP/s before and 12.2 TFLOP/s after for FP32
SGEMM, but those figures are not reproduced here. Stop immediately for cooling
problems, Xid/driver errors, or numerical mismatches. An SGEMM pass says nothing
about expanded-memory stability.

## Development checks

```bash
python -m pytest -q
python -m compileall -q cmpunlock tests
bash -n install.sh
```

Tests against authentic firmware run when the exact cached NVIDIA fixtures are
available; otherwise those two cases skip. POSIX installer behavior is tested on
POSIX hosts and skipped on Windows.

## Sources and license

- Local paper: [`main.pdf`](main.pdf), *A Canary in the Crypto Mine* (June 2026)
- Detailed audit: [`docs/STUDY_NOTES.md`](docs/STUDY_NOTES.md)
- NVIDIA open kernel modules:
  <https://github.com/NVIDIA/open-gpu-kernel-modules>
- NVIDIA GSP documentation:
  <https://download.nvidia.com/XFree86/Linux-x86_64/580.105.08/README/gsp.html>
- Independent GA100 register survey:
  <https://gist.github.com/JRex286/0480d2b2b35ad594e57b6543952be307>
- Audited public prototype:
  <https://github.com/fulracoco/cmpunlocker/tree/9b9fb2f27a618f13e6b016adfc6e86b1e60fa84d>

The payload frame format and community constants were derived from GPL-2.0 code,
so this repository is licensed under GPL-2.0-only. See [`LICENSE`](LICENSE) and
[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md). Both notices are included in
built distributions.
