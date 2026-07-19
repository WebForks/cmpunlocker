# CMP 170HX GA100 unlock research tool

This repository implements and audits host-side mechanisms related to
[`main.pdf`](main.pdf), *A Canary in the Crypto Mine: Defeating Stack Protection
in a GPU Secure Coprocessor*. The default 580 workflow remains an offline-first,
fail-closed compute experiment. A separate, quarantined
[`experimental/610-memory`](experimental/610-memory/) workflow now reconstructs
the supplied NVIDIA 610.43.03 module approach for exactly one `10de:20c2` card.
Neither path is a certified or independently reproduced hardware unlock.

The evidence has five distinct levels:

1. The paper's offline proof-of-control payload is reproduced exactly.
2. One external report exercised the original three-frame continuation on a
   `20c2` card with driver `580.173.02`, after adding an `nvidia-smi`
   initialization trigger. It reported compute near 12.1 TFLOP/s FP32 and
   6.3 TFLOP/s FP64, but still showed only 8192 MiB.
3. A separate 610.43.03 report and two screenshots show a software-visible
   65,536-MiB value and a compute result. They do not include a raw module hash,
   full-range distinct-address output, error log, thermal log, or long-duration
   stability artifact. A displayed capacity is not proof of physical,
   non-aliased, reliable memory.
4. The productive gadgets and register meanings remain community-derived. No
   public source supplies the paper's decrypted derivation, complete register
   map, or raw reproduction bundle.
5. This project has not independently reproduced any CMP 170HX hardware result.

Do not interpret a successful firmware patch, PLM readback, register readback,
or `nvidia-smi` capacity as proof of a working unlock. Compute needs a correct
benchmark. Expanded memory needs full-range address-dependent writes and reads,
repeated stress, clean kernel logs, and adequate cooling. Even a complete pass
only describes that card at the tested temperature and duration.

## Quick install

On Linux, Python 3.10 or newer and the Python `venv` module are required. Run the
installer as your normal user, not with `sudo`:

```bash
./install.sh
.venv/bin/cmpunlock profile list
```

The root `install.sh` replaces the previous virtual-environment setup commands. It creates
or reuses `.venv`, installs this checkout in editable mode, and runs an offline
smoke test. It does not inspect a GPU, invoke `sudo`, require root, unload a
module, patch firmware, enable systemd, or execute the experimental 610 path.
The 610 experiment has its own reviewed installer and explicit acknowledgements;
see [64-GiB experimental workflow](#64-gib-experimental-workflow-for-local-llms).

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

## 64-GiB experimental workflow for local LLMs

The honest answer is conditional: this can make one `10de:20c2` CMP 170HX
*attempt* to expose 64 GiB, but software cannot promise that the disabled HBM on
your particular binned card is reliable. Do not load a model merely because
`nvidia-smi` says `65536 MiB`. Wait for the repository validator to print the
literal final line `LLM_READY`.

Use a disposable native Linux x86-64 host, not WSL or a VM. The CMP must be the
only NVIDIA PCI function and must not drive a display. You need out-of-band
console access, a way to remove AC power, server-grade forced airflow over the
passive heatsink, matching running-kernel headers, normal C/kernel build tools,
`git`, `kmod`, `binutils`, util-linux `flock`, an initramfs tool, and CUDA
`nvcc`. Secure Boot and kernel lockdown must be disabled.

The transaction currently supports `update-initramfs` plus `lsinitramfs`, or
`dracut` plus `lsinitrd`. It also requires the kernel module `updates` directory
and `/var/lib/cmpunlocker-610-memory/archives` to share a filesystem so module
directory renames are atomic; preflight refuses other layouts.

First install the exact official NVIDIA **open** 610.43.03 driver, userspace, and
firmware through the method appropriate for your distribution. This repository
does not automate that system-wide vendor installation. The scripts require a
working stock `nvidia-smi`, module version `610.43.03`, and the exact official
`gsp_tu10x.bin` (29,352,832 bytes, SHA-256
`73065619db9ec921d19fc4e519dd04d91a9199b525eaca9b257b89fb8c5ec52c`) before
they will build or install anything.

Because distribution/local kernel-module builds do not have one published
universal `.ko` hash, the scripts additionally require the loaded core
`srcversion` to match the resolved file and record the exact five predecessor
paths/hashes for rollback. That proves consistency with the selected predecessor,
not original NVIDIA authorship of those locally installed module bytes.

From this checkout, build as your normal user and install only the verified
artifact as root:

```bash
cd experimental/610-memory

./build.sh --dry-run
./build.sh \
  --acknowledge I-ACCEPT-UNVERIFIED-610-MEMORY-KERNEL-BUILD
./build-validator.sh

sudo ./install.sh --dry-run
sudo ./install.sh \
  --acknowledge I-ACCEPT-UNVERIFIED-610-MEMORY-KERNEL-INSTALL
```

The split privilege boundary is deliberate. `build.sh` fetches only NVIDIA
commit `452cec62d827034798072827d3866d1881662b77`, verifies the patch hash, and
builds for the running kernel without root. The installer neither runs a package
manager nor the NVIDIA runfile, stops no service, kills no process, unloads no
module, and does not hot-apply the experiment. It installs five verified modules
under `updates/cmpunlocker-610-memory`, records the exact five-module predecessor
set, backs up the exact initramfs bytes, runs `depmod`, rebuilds the current
initramfs, rejects any competing/duplicate current-kernel NVIDIA module inside
that image, and leaves the currently loaded stock driver untouched. An
initramfs that legitimately contains no NVIDIA modules is accepted.

Install, removal, cold-cycle confirmation, and validation share a nonblocking
exclusive lock at `/var/lib/cmpunlocker-610-memory/operation.lock`; none can race
another, and the lock remains held for the entire stress run. The first root
install preflight may create only the root-owned 0700 state root and its 0600
lock file; the first operation on a legacy pre-lock state tree may create that
lock file too. `--dry-run` does not install modules or change boot metadata.

Shut the machine down, remove AC power until the board has fully lost power,
then cold-start it. Do not substitute an ordinary warm reboot. After boot, stop
all GPU jobs and validate:

```bash
cd experimental/610-memory

sudo ./validate.sh --preflight-only \
  --cold-cycle-acknowledge I-CONFIRM-FULL-AC-POWER-CYCLE-AFTER-610-MEMORY-INSTALL

sudo ./validate.sh --passes 5 \
  --cold-cycle-acknowledge I-CONFIRM-FULL-AC-POWER-CYCLE-AFTER-610-MEMORY-INSTALL \
  --acknowledge I-ACCEPT-UNVERIFIED-610-MEMORY-STRESS-AND-CONFIRM-FORCED-AIRFLOW
```

The fixed validator checks all five resolved module files, loads the verified
`nvidia_uvm` if CUDA has not already loaded it, and verifies the loaded core/UVM
provenance, exact PCI identity, exact on-disk GSP hash, loaded GSP firmware
version, current-boot driver markers, all
PLM/host-write/metadata/PMA gates,
reported capacity, idle state, and kernel errors. It then allocates 8, 16, 32,
48, and 60 GiB while a separate poller enforces the temperature limit. For every
stage and pass it writes and reads three
bijective address-dependent patterns across every 64-bit word. Any mismatch,
allocation/CUDA error, Xid, missing marker, excessive temperature, or changed
module stops the run without `LLM_READY`. Logs are retained under
`/var/log/cmp170-memory-validation` by default. Each run directory also retains
the root-owned installation record, five-module checksum manifest, resolved
path/hash/version/source-version table, NVIDIA-SMI/NVML and loaded/on-disk GSP
facts, boot IDs, kernel logs, temperatures, and per-stage output needed to audit
which exact stack produced the result.

Only after `LLM_READY` should you start an LLM backend, as your normal user. Plan
against the tested 60-GiB allocation, not the displayed 64-GiB total: CUDA
contexts, workspaces, KV cache, and the inference engine need headroom. In
practice, keep model weights comfortably below roughly 55-58 GiB and monitor
temperature and `dmesg` during initial long sessions. A 70B-class 4-bit model is
a plausible fit; a 70B 8-bit model generally is not. This path does not pool
cards, add ECC, enable NVLink, or improve PCIe, so model load and CPU offload can
remain bottlenecked by the card's link.

If any gate fails, do not run a model. Preserve the reported log directory and
remove the experiment:

```bash
sudo ./remove.sh --dry-run
sudo ./remove.sh \
  --acknowledge REMOVE-CMPUNLOCKER-610-MEMORY-WITHOUT-HOT-UNLOAD
```

Removal archives rather than deletes the experimental module set, restores the
recorded predecessor, rebuilds module metadata/initramfs, and deliberately does
not hot-unload the running driver. Fully remove AC power again, cold-start, then
record that physical clearing boundary:

```bash
sudo ./remove.sh --confirm-cold-cycle \
  --acknowledge I-CONFIRM-FULL-AC-POWER-CYCLE-AFTER-610-MEMORY-REMOVAL
```

Before changing the kernel, NVIDIA driver/userspace/firmware, or initramfs
tooling, run removal and complete its cold-cycle confirmation while the exact
recorded predecessor still exists. Only then update, boot the new kernel, and
build/install a new kernel-specific artifact. If an update was already applied,
boot the recorded old kernel and restore its exact predecessor stack/tooling so
`remove.sh` can verify and remove the old experiment; do not delete its target or
state by hand. The full operational contract, failure behavior, and file layout
are documented in
[`experimental/610-memory/README.md`](experimental/610-memory/README.md).

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
- Tests exercise all three exact official 580 firmware images, payload layout, malformed
  ELF input, BAR0 bounds, exact live-profile allowlists, transaction locking,
  atomic restoration, recovery validation, pre-boot audit durability, and
  failure paths. Live transaction tests use mocks, not a physical GPU.
- The supplied 610.43.03 runfile matches NVIDIA's published SHA-256. Its exact
  Turing GSP firmware and embedded GA100 Booter were independently fingerprinted;
  the Booter is byte-identical to the pinned 580 copies.
- The experimental 610 build starts from exact official open-module commit
  `452cec62d827034798072827d3866d1881662b77`; no supplied object, prebuilt module,
  runfile, or firmware blob is imported into this repository.
- Frozen patch SHA-256
  `f377efcb000035449a4520c3f306d0983c4de9b3dbe8a71f2ee616a5c0571c6b`
  applies cleanly and compiled all five `610.43.03` modules against Ubuntu
  `6.8.0-134-generic` headers. The final `nvidia.ko` contains the reviewed PMA
  function and required validation markers. It was not loaded on hardware.

## What is not verified

- Productive gadget `0x10b9` and tail `0x810d` lie inside authenticated,
  encrypted Falcon IMEM. The DMEM frame start `0xff48`, stride `0x18`, and
  replacement canary `0xfaceb13d` likewise have no published emulator
  derivation. No decrypted image or emulator was published.
- PLM `0x823804` and compute overrides `0x82381c`/`0x823820`. The paper omits
  the full register map; these values are inherited from the public prototype.
- The original continuation's other two HS writes (`0x9a0204 <- 0x02779000` and
  `0x100ce0 <- 0x20b`). Their purpose and safety are undocumented. The pinned
  `580.173.02` profile reproduces them because that exact sequence has one
  community-reported compute result, but labels them unverified memory-side-effect
  frames, records their registers only diagnostically, and never claims a capacity
  unlock. The older profiles retain the lower-risk but speculative repeated-PLM
  sequence and have no hardware report.
- The paper's driverless Falcon loader. Both public codebases instead enlarge
  the installed GSP signature section and use `modprobe nvidia`; this is a
  plausible alternative delivery path, not an exact reproduction of the paper.
- Stock open-driver binding for CMP IDs `2082` and `20c2`. They are absent from
  NVIDIA's published 580 compatible-GPU tables. A host whose installed module does
  not already bind the card cannot complete this workflow.
- The default 580 workflow does not implement memory capacity, PCIe, ECC, or
  NVLink. Its reported profile contains two disclosed memory-related HS side
  effects, but the host never publishes expanded capacity and the result always
  says memory is unverified.
- The quarantined 610 workflow implements a community-derived 8-to-64-GiB
  geometry/PMA experiment and staged address testing through a 60-GiB
  allocation. It has not been executed
  by this project on a CMP 170HX. It does not implement the paper's undocumented
  refresh adjustment, PCIe sequence, ECC, NVLink, or HBM mode-register path.
  The paper did not defeat PCIe Gen3, ECC, or runtime HBM mode-register
  programming, and its stable 80-GB result traded about 32% throughput for more
  frequent refresh.
- PCI ID `20b0`. It identifies an A100 SXM4 40 GB, not a CMP 170HX, and is
  unconditionally rejected even if a custom profile lists it.

## Cross-check against the paper

| Mechanism | Published in `main.pdf` | Implemented here | Evidence |
|---|---:|---:|---|
| DMA destination `0x800` | Yes, Section 5.5 | Profiled/assumed | Closed-booter behavior unverified |
| DMA length `0xf800` | Yes, Section 5.5 | Host section constructed | Metadata propagation statically checked |
| Guard address `0x6340` | Yes, Sections 5.2-5.5 | Payload layout encoded | Role unverified beyond clear-stub address corroboration |
| Uniform proof dword `0x4a7` | Yes, Section 5.5 | Yes, `--mode proof` | Exact offline reproduction |
| Productive continuation | Described, bytes omitted | Community-derived; exact reported sequence pinned for 580.173.02/20c2 | One external compute report; no independent reproduction |
| Full PLM/register map | Explicitly omitted | Partial community-derived compute-path values; FBPA/LMR meanings unverified | Unverified |
| Memory capacity | 10 to 80 GB; full-range hash and stress described, raw artifacts omitted | Separate experimental 20c2 8-to-64-GiB module path plus staged testing through 60 GiB | Community screenshots only; no local hardware run |
| Driverless loader/emulator | Used, not published | Not included | Missing reproduction artifact |
| SM throughput result | Reported in Table 2 | Benchmark included, no local result | One external 580.173.02 result; not reproduced here |

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
| Hardware gate | Only CMP `2082`/`20c2`; the reported 580.173.02 path is `20c2`-only | Also accepts A100 `20b0` |
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

Only the installer ergonomics were adopted initially: this repo's installer is
setup-only and stops before all hardware operations. The two undocumented HS
writes were initially withheld; they now exist only in the isolated 580.173.02
profile described below.

## Comparison with `abobasixseven/unlock-cmp-170hx`

The supplied repository was reviewed at commit
[`8eb8046`](https://github.com/abobasixseven/unlock-cmp-170hx/tree/8eb8046372611a557c98421bfc024a1e8c87f353).
It contains one README and no executable code, license, tests, hashes, or raw
result artifacts. Its proposed implementation is the
[`kinako404/cmpunlocker`](https://github.com/kinako404/cmpunlocker/tree/6cf67b319f8d14a396f6d905211071ff11076004)
fork plus manual corrections. At that exact fork commit, the documented PLM-key
and GPU-enumeration fixes are still absent.

The useful new evidence is
[`Issue #1`](https://github.com/abobasixseven/unlock-cmp-170hx/issues/1).
On one `20c2` card with driver `580.173.02`, the reporter found that loading the
module alone did not initialize the GPU/GSP. Running `nvidia-smi` after the
patched `modprobe` triggered initialization; the corrected run passed the
pipeline/readback gate and produced a compute benchmark. The same report showed
8192 MiB and says the memory unlock was unsuccessful.

| Area | This repository after integration | Guide / referenced kinako fork |
|---|---|---|
| Source of evidence | Exact guide/fork commits and the one hardware-report issue are distinguished | README mixes instructions, expected output, and unsupported claims |
| 580.173.02 compatibility | Exact official GSP, section, signature, and embedded-booter hashes; only PCI ID `20c2` | No exact firmware or booter hashes |
| HS frames | Exact reported FBPA, LMR, PLM order only in the pinned profile | Same values, but their derivation and memory meaning are not demonstrated |
| Initialization | Bounded, recorded `nvidia-smi` probe after patched load; nonzero/timeout is diagnostic, PLM is the gate | Manual correction appears only in the issue, not kinako HEAD |
| Reset sequence | FLR #1, clean unload, immediate stock restore, FLR #2, then mandatory PLM gate | Stops services, kills users, and may force module removal |
| Firmware safety | Relocates overlapping ELF sections, journals, and restores stock in `finally` | Overwrites adjacent sections and can skip restoration on failure |
| Host BAR0 writes | Only the two compute overrides; FBPA/LMR are never host-written | Watchdog attempts undocumented memory-related writes |
| Memory claim | Always reports `memory_capacity_verified: false` | Guide displays a constructed 65,536-MiB example despite the real report remaining at 8 GiB |
| Persistence | One-shot transaction; no daemon | One-second root watchdog proposed |

What was adopted:

- the exact `580.173.02` official-firmware profile, restricted to the reported
  `20c2` device;
- the original three HS frames in their reported order;
- a 15-second, target-scoped initialization probe immediately after patched
  module load;
- the reported two-FLR ordering, with stock restored immediately after clean
  unload and a mandatory PLM readback after both resets;
- a mandatory successful `nvidia-smi` after restoring stock firmware.

What was not adopted: the root watchdog, force unloading, automatic process or
service termination, arbitrary firmware matching, destructive ELF overwrite,
A100 support, host FBPA/LMR writes, and the unsupported 64-GB claim.

### Why this was not in the earlier implementation

The earlier audit followed the then-supplied `fulracoco/cmpunlocker` repository
and `main.pdf`; it did not broaden the search to this separate guide's issue
tracker. Missing the `nvidia-smi` initialization report was a real search gap.

The FBPA/LMR frames themselves were already visible in the original prototype
but were deliberately withheld because the paper omits the register map, no
derivation or successful hardware log was available, and both writes can affect
memory configuration. Repeating the PLM frame was a conservative substitution,
but it was also speculative and not semantically equivalent. Issue #1 now
justifies preserving the exact sequence in a narrowly pinned profile for compute
research. It does not justify the guide's memory, daemon, or broad-compatibility
claims.

## Comparison with the supplied 610.43.03 tree and `amoghmunikote/cmpunlocker`

The supplied `other-project/Post.txt`, 461-MB NVIDIA runfile, and expanded
`open-gpu-kernel-modules-610.43.03` tree were audited without executing their
root installer or importing their binaries. The runfile is authentic, but the
source tree contains 4,148 generated build files, no final loadable module set,
and eleven local source changes. Ten match the public direction of the linked
[`amoghmunikote/cmpunlocker`](https://github.com/amoghmunikote/cmpunlocker/tree/0a5f0624cc6f4cbbf3f2e8d357e891c4a64cc8a2);
the extra supplied change disables register-operation validation globally.

The linked repository was pinned at commit
[`0a5f062`](https://github.com/amoghmunikote/cmpunlocker/commit/0a5f0624cc6f4cbbf3f2e8d357e891c4a64cc8a2).
As currently published, its first patch is malformed against its pinned source,
and its profile rewrite matches no geometry constants. Its screenshots are useful
leads, but capacity text and a benchmark window cannot establish non-aliasing or
HBM stability.

| Area | This repository's quarantined integration | Supplied/public 610 path |
|---|---|---|
| Input | Exact NVIDIA commit, patch SHA, GSP size/hash, driver point release | Supplied pre-expanded tree and artifacts; public script accepts a small 610 family |
| Scope | Exactly one NVIDIA PCI function, exactly `10de:20c2` | Claims multi-GPU support; some global behavior is weakened |
| PLM gate | All four exact readbacks are mandatory before host overrides | Failed attempts are logged and initialization can continue |
| Host writes | Exact SS0/SS1/CFG1/LMR readback; partial failure attempts rollback and blocks metadata/PMA | Writes are printed but not a complete fail-closed transaction |
| Driver integrity | Stock regops validation; no optional external payload; stock behavior for every other device | Global regops bypass in the supplied tree; optional root-path `dmem.bin` |
| Capacity publication | Validated region count/limits and PMA capacity; stable success/failure markers | Metadata/PMA manipulation can continue after earlier failures |
| Install | Non-root pinned build, isolated module directory, no package/user-space/service/process changes, initramfs backup, reversible removal | One root workflow installs packages/userspace, stops services, rewrites configuration, and masks some recovery failures |
| Proof | Requires fixed 8/16/32/48/60-GiB address-pattern stages and clean current-boot logs | `nvidia-smi` plus suggested short `gpu_burn`; no raw supplied output |
| PCIe | Explicitly not implemented or claimed | Post claims Gen2; public README says platform-dependent; code does not supply a complete paper-derived root-port path |

What was retained, because the high-range path depends on it: repeated signed
Booter PLM writes, exact `20c2` SS/CFG1/LMR values, authentic-signature rebuild,
64-GiB GSP metadata, late PMA registration, the native-aperture PRAMIN clamp,
the `20c2` scrub/CE workaround, and per-boot reapplication. All were rewritten
against the exact official source and kept behind the one-device gate.

What was removed or changed: the global regops bypass, global WPR2 recovery,
optional `dmem.bin`, non-fatal PLM/PMA paths, unchecked region growth, signature
leak, PCIe claim, package installation, service stopping, live module removal,
configuration overwrite, prebuilt blobs, and the instruction to regard Booter
errors as generically harmless. See
[`docs/STUDY_NOTES.md`](docs/STUDY_NOTES.md#supplied-nvidia-6104303-module-path)
for the source-level audit.

### Why the 610 memory path was not implemented earlier

The earlier inputs were `main.pdf` and the public 580-oriented prototype. The
paper deliberately omits its productive continuation, complete register map,
loader, and raw validation artifacts, while the prototype did not provide this
610 GSP-metadata/PMA integration. The later supplied 610.43.03 source tree made
that additional sequence inspectable. It still could not be copied directly:
its global validation bypasses, non-fatal failure paths, unchecked PMA growth,
live root installer, and missing final module/test evidence were unsafe. The
quarantined workflow was added only after rewriting those parts against an exact
official source commit and adding reversible installation and high-range tests.

## Supported inputs and live prerequisites for the 580 workflow

Bundled profiles currently support exact stock firmware from:

- NVIDIA `580.105.08`, SHA-256
  `84e0f47adc5b7f40a5789f1e3d528ca1269bd6184029dec0af6c76f9f282d0d7`
- NVIDIA `580.126.09`, SHA-256
  `a3788bfb368bdd2384a8b1aceeb946f2b0e1dff734d9f3fdca65e7f727ed42b7`
- NVIDIA `580.173.02`, SHA-256
  `6f3ccbd570c7ac2a7ea910d9d87fc3d23db9ae3dfe82020ea07b17a30954495e`
  (`20c2` only; community-reported two-phase strategy)

Unknown or modified firmware is rejected. `580.x` is not treated as a compatible
family.

A live test host additionally needs:

- Linux x86-64, root for live apply/recovery, readable PCI sysfs, writable
  `resource0`, and PCI function-level reset support;
- `kmod` tools (`modinfo` and `modprobe`), `nvidia-smi`, plus `zstd` when
  `nvidia.ko` is `.ko.zst` compressed;
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

Build the community-derived compute-path image. The 580.173.02 profile includes
the disclosed memory-related HS frames described above; this is not a
memory-capacity workflow or success claim:

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
  --firmware /lib/firmware/nvidia/580.173.02/gsp_tu10x.bin

.venv/bin/cmpunlock system plan 0000:41:00.0 \
  --firmware /lib/firmware/nvidia/580.173.02/gsp_tu10x.bin
```

The live commands accept only unchanged bundled profiles and a hard-coded
compute-result contract. Host BAR0 writes remain compute-only; the reported
profile's signed HS frames are separately pinned and disclosed. Custom JSON remains
available for offline research but cannot authorize new live IDs, control-flow
values, or BAR0 writes.

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
  --firmware /lib/firmware/nvidia/580.173.02/gsp_tu10x.bin \
  --execute \
  --acknowledge UNVERIFIED-CMP170HX-REPORTED-PATH-WITH-MEMORY-SIDE-EFFECTS
```

That command is the closest implementation of the one community-reported
compute run. It accepts only the exact 580.173.02 stock image and PCI ID `20c2`.
The stronger acknowledgement is required because its signed HS payload contains
the two undocumented FBPA/LMR frames even though the host never writes those
registers and this tool does not claim a memory-capacity result.

The read-only `system inspect` command also requires a successful, target-scoped
stock `nvidia-smi` query while the known stock driver, firmware, and userspace are
loaded. This catches an NVML/userspace mismatch before modules are unloaded.

Before mutation, the transaction refuses a PLM already at the expected all-open
value, records baseline PLM/compute/FBPA/LMR registers, verifies the CMP ID,
exact firmware/module/booter, `nvidia-smi`, single GPU, unloaded modules, and FLR
availability. It then:

1. writes a durable stock backup and transaction journal;
2. atomically installs the patched GSP;
3. fsyncs an audit/state record before attempting the patched driver boot;
4. loads `nvidia`, runs a bounded `nvidia-smi` initialization probe, and records
   its output; failure/timeout is tolerated only in this patched phase;
5. performs FLR #1, unloads `nvidia` cleanly, immediately restores stock
   firmware, and performs FLR #2;
6. requires the PLM to remain all-open after both resets, records FBPA/LMR
   diagnostically, then host-writes and verifies only the two compute overrides;
7. retains `finally` restoration as a fallback, loads stock `nvidia`, requires a
   successful final `nvidia-smi`, and verifies that the compute values persisted.

No force-removal or process-kill fallback exists. If clean unload, the post-FLR
PLM gate, restoration, stock initialization, binding, or readback fails, the
transaction records partial state and requires a cold power cycle.
For the reported profile, that state conservatively marks signed HS side effects
as potentially active as soon as a patched boot is attempted, even when the PLM
gate fails before any host compute write.

The older 580.105.08 and 580.126.09 profiles keep their legacy compute-only
ordering, which places the PLM gate and host compute writes before one FLR. They use acknowledgement
`UNVERIFIED-CMP170HX-EXPERIMENT`, retain the speculative repeated-PLM HS chain,
and have no published hardware result. `system plan` prints the strategy and
required acknowledgement for whichever exact firmware profile was selected.

No readback proves throughput, proves how the PLM changed, or proves memory
capacity. It is only a gate consistent with the expected continuation, followed
by mandatory compute and thermal validation.

## Recovery and cold-cycle state

If power or the process is lost while patched firmware is installed, boot through
the out-of-band console and run:

```bash
sudo .venv/bin/cmpunlock system recover \
  --firmware /lib/firmware/nvidia/580.173.02/gsp_tu10x.bin
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
  --firmware /lib/firmware/nvidia/580.173.02/gsp_tu10x.bin \
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
SGEMM. The external 580.173.02 issue reports about 12.1 TFLOP/s FP32 and
6.3 TFLOP/s FP64 from `clpeak`; neither result is independently reproduced here.
Stop immediately for cooling problems, Xid/driver errors, or numerical
mismatches. An SGEMM pass says nothing about expanded-memory stability, and this
tool does not implement or report a memory-capacity unlock.

## Development checks

```bash
python -m pytest -q
python -m compileall -q cmpunlock tests
bash -n install.sh
bash experimental/610-memory/tests/static.sh
```

Tests against authentic firmware run when the exact cached NVIDIA fixtures are
available; otherwise those three cases skip. POSIX installer behavior is tested on
POSIX hosts and skipped on Windows. The CUDA validator compiles when `nvcc` is
available; hardware allocation tests are never part of the unit suite.

## Sources and license

- Local paper: [`main.pdf`](main.pdf), *A Canary in the Crypto Mine* (June 2026)
- Detailed audit: [`docs/STUDY_NOTES.md`](docs/STUDY_NOTES.md)
- NVIDIA open kernel modules:
  <https://github.com/NVIDIA/open-gpu-kernel-modules>
- Exact NVIDIA open-module 610.43.03 release:
  <https://github.com/NVIDIA/open-gpu-kernel-modules/releases/tag/610.43.03>
- NVIDIA 610.43.03 published runfile checksum:
  <https://download.nvidia.com/XFree86/Linux-x86_64/610.43.03/NVIDIA-Linux-x86_64-610.43.03.run.sha256sum>
- NVIDIA GSP documentation:
  <https://download.nvidia.com/XFree86/Linux-x86_64/580.105.08/README/gsp.html>
- NVIDIA 580.173.02 supported-products table:
  <https://download.nvidia.com/XFree86/Linux-x86_64/580.173.02/README/supportedchips.html>
- Independent GA100 register survey:
  <https://gist.github.com/JRex286/0480d2b2b35ad594e57b6543952be307>
- Audited public prototype:
  <https://github.com/fulracoco/cmpunlocker/tree/9b9fb2f27a618f13e6b016adfc6e86b1e60fa84d>
- Audited CMP 170HX guide (README-only):
  <https://github.com/abobasixseven/unlock-cmp-170hx/tree/8eb8046372611a557c98421bfc024a1e8c87f353>
- Community 580.173.02 hardware report and corrections:
  <https://github.com/abobasixseven/unlock-cmp-170hx/issues/1>
- Audited kinako fork referenced by that guide:
  <https://github.com/kinako404/cmpunlocker/tree/6cf67b319f8d14a396f6d905211071ff11076004>
- Audited 610 module implementation:
  <https://github.com/amoghmunikote/cmpunlocker/tree/0a5f0624cc6f4cbbf3f2e8d357e891c4a64cc8a2>

The payload frame format, community constants, and experimental 610 module
mechanics were derived from GPL-2.0 code, so this repository is licensed under
GPL-2.0-only. NVIDIA files retain their upstream dual MIT/GPL notices after the
patch is applied. See [`LICENSE`](LICENSE) and
[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md). Both notices are included in
built distributions.
