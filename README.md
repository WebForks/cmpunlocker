# CMP 170HX GA100 unlock research tool

This repository implements the host-side mechanism described in `main.pdf` as an
offline-first, fail-closed research tool. It is **not a reproduced hardware unlock**.
The paper does not publish the productive Falcon continuation, loader, emulator,
full register map, exact firmware build, or hardware logs. The productive constants
used here came from the first public community prototype and remain unverified.

## What was verified

- NVIDIA's GA100 Booter Load is Falcon v6. Its clear stub writes and reads the stack
  guard at DMEM `0x6340`, independently confirming the paper's address.
- NVIDIA's 580 open kernel code obtains the GA100 signature pointer and length from
  `.fwsignature_ga100` and propagates that length into WPR metadata. Enlarging this
  section is therefore a plausible route to the paper's unbounded `0x800` DMA copy.
- The paper's proof image is reproducible offline: `0xf800` bytes made from dword
  `0x4a7`, which makes the overwritten guard, saved copy, and return PC equal.
- The bundled profiles pin authentic `gsp_tu10x.bin` images and the exact embedded
  GA100 production-booter SHA-256.
- The patcher preserves `.fwimage`, `.fwversion`, build ID, both Turing signatures,
  string tables, and symbol tables. It reparses the result and rejects overlaps.

## What is not verified

- Productive gadget `0x10b9`, tail `0x810d`, frame layout, and replacement canary
  `0xfaceb13d`. They sit in authenticated/encrypted Falcon IMEM; no decrypted image,
  emulator, derivation, or silicon trace was published.
- A hardware compute unlock. The live path requires an explicit experimental-risk
  acknowledgement and stops unless the PLM and every override read back exactly.
- Memory, PCIe, ECC, and NVLink unlocks. They are intentionally not implemented.
  The paper's one-card 80 GB result had 2,796 errors at stock refresh, omitted the
  stabilizing register value, lost about 32% throughput, and did not enable ECC.
- PCI ID `20b0`. That is an A100 SXM4 40 GB and is explicitly rejected. Only CMP
  IDs `2082` and `20c2` are accepted.
- Stock open-driver binding for `2082`/`20c2`. Those IDs are absent from NVIDIA's
  580.126.09 compatible-GPU table, so this code does not assume that a newly installed
  open module will bind the card. `system inspect` requires an already working stock
  `nvidia` binding before you unload anything.

## Install

Python 3.10 or newer is required. Offline inspection and image generation work on
Windows or Linux; BAR0 and driver operations are Linux-only.

```powershell
py -3 -m venv .venv
.venv\Scripts\python -m pip install -e .
.venv\Scripts\cmpunlock profile list
```

On Linux:

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
source .venv/bin/activate
cmpunlock profile list
```

## Offline firmware workflow

Inspect an exact stock image. A matching bundled profile is selected by SHA-256:

```bash
cmpunlock firmware inspect /path/to/gsp_tu10x.bin
```

Build the paper's proof-of-control image. This intentionally spins the secure core
and is not an unlock image:

```bash
cmpunlock firmware patch /path/to/gsp_tu10x.bin ./gsp.proof.bin --mode proof
```

Build the community-derived, compute-only experimental image:

```bash
cmpunlock firmware patch /path/to/gsp_tu10x.bin ./gsp.compute.bin --mode compute
```

The tool never overwrites the input. It prints the stock, payload, and output hashes
plus every relocated ELF section. Unknown firmware is rejected rather than treated
as compatible with all `580.x` releases.

## Server preflight

Use a disposable Linux installation, out-of-band console, complete cold-power-cycle
control, and a single CMP card that is not the boot/display GPU. The CMP 170HX is
passively cooled; provide server-grade forced airflow and monitor temperature. Do
not put valuable data or another NVIDIA GPU in the first test host.

Locate the target and the matching firmware installed by the NVIDIA driver:

```bash
lspci -Dnn | grep -i NVIDIA
modinfo -F version nvidia
modinfo -n nvidia
find /lib/firmware/nvidia -name gsp_tu10x.bin -print
```

Use one complete, matching NVIDIA point release; do not mix a kernel module, GSP
firmware, or userspace libraries from different releases. The tool does not install
or patch the driver. Confirm `lspci -k` reports `Kernel driver in use: nvidia` for the
CMP before continuing.

Run the read-only validator while the driver is still installed. It checks the PCI
ID, exact GSP hash/layout, exact module version, and the decompressed embedded-booter
hash. Replace the BDF and firmware path with the values from your host:

```bash
sudo .venv/bin/cmpunlock system inspect 0000:41:00.0 \
  --firmware /lib/firmware/nvidia/580.126.09/gsp_tu10x.bin

cmpunlock system plan 0000:41:00.0 \
  --firmware /lib/firmware/nvidia/580.126.09/gsp_tu10x.bin
```

Both checks must pass. A profile match does not prove the unpublished continuation.

## Experimental compute run

Stop your own scheduler, persistence daemon, display manager, and GPU workloads.
This project deliberately does not kill processes or stop services for you. Confirm
that no process owns an NVIDIA device, then unload the modules cleanly:

```bash
sudo fuser -v /dev/nvidia* /dev/nvidia-caps/*
sudo modprobe -r nvidia_uvm nvidia_drm nvidia_modeset nvidia_peermem nvidia
grep '^nvidia' /proc/modules
```

The `fuser` and `grep` commands should produce no device users/modules before the
run. Execute only after reading this file and `docs/STUDY_NOTES.md`:

```bash
sudo .venv/bin/cmpunlock system apply 0000:41:00.0 \
  --firmware /lib/firmware/nvidia/580.126.09/gsp_tu10x.bin \
  --execute \
  --acknowledge UNVERIFIED-CMP170HX-EXPERIMENT
```

The transaction makes a durable stock backup, journals recovery, atomically installs
the patch, loads only `nvidia`, requires the PLM to read `0xffffffff`, writes only the
two compute-rate registers, restores stock firmware in `finally`, performs FLR, loads
the stock driver, and checks persistence. It never uses forced module removal.

If a failure occurs after an override may have been written, the command records a
hidden `*.cmpunlock-state.json` beside the firmware and reports that path. Treat any
such partial-state warning as requiring a full cold power cycle. A successful run
also leaves the state file as an audit record until the next cold power cycle.

If power or the process is lost while the patched file is installed, boot through
the out-of-band console and run:

```bash
sudo .venv/bin/cmpunlock system recover \
  --firmware /lib/firmware/nvidia/580.126.09/gsp_tu10x.bin
sudo modprobe nvidia
```

A cold power cycle clears override state. Do not use a warm reboot as the recovery
boundary described by the paper. `system recover` restores the file on disk; it
cannot clear a register override that already reached the GPU.

## Verify compute and cooling

Successful register readback is only an intermediate result. Compile the included
pedantic-FP32 cuBLAS benchmark with the CUDA toolkit and compare before/after results:

```bash
nvcc -O3 tools/sgemm_bench.cu -lcublas -o sgemm_bench
nvidia-smi --query-gpu=pci.bus_id,name,temperature.gpu,power.draw,clocks.sm \
  --format=csv -l 1
./sgemm_bench 8192 20
```

The paper reports roughly 0.393 TFLOP/s before and 12.2 TFLOP/s after for FP32 SGEMM,
but those figures are not reproduced here. Stop immediately for cooling problems,
Xid errors, driver errors, or numerical mismatches. Do not infer memory stability
from an SGEMM pass.

## Sources and license

- Local study: `main.pdf`, *A Canary in the Crypto Mine* (June 2026)
- NVIDIA open kernel modules: <https://github.com/NVIDIA/open-gpu-kernel-modules>
- NVIDIA GSP documentation: <https://download.nvidia.com/XFree86/Linux-x86_64/580.105.08/README/gsp.html>
- Independent GA100 register survey: <https://gist.github.com/JRex286/0480d2b2b35ad594e57b6543952be307>
- Audited public prototype: <https://github.com/fulracoco/cmpunlocker>

The payload-frame format and community constants were derived from GPL-2.0 code, so
this repository is licensed under GPL-2.0-only. See `LICENSE` and
`THIRD_PARTY_NOTICES.md`.
