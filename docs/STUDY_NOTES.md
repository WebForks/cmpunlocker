# Reproducibility notes for `main.pdf`

## Paper mechanism

The study describes a signed GA100 SEC2 Booter Load image whose LS signature path
copies a host-controlled signature blob to a fixed DMEM destination at `0x800`. The
WPR metadata signature length is not bounded to the destination. A length of `0xf800`
reaches the top of the 64 KiB DMEM and overwrites the global stack guard at `0x6340`,
saved stack-canary copies, and return state.

For the published proof, every payload dword is `0x4a7`. Under the paper's reported
DMEM/stack layout, the master guard, saved copy, and saved PC therefore become equal;
the canary comparison passes and execution returns to a reported self-loop at
`0x4a7`. This is proof of program-counter control, not a productive unlock.

The proposed productive path uses Heavy Secure execution to open privilege-level
masks (PLMs), then uses host BAR0 writes to change product feature overrides. The
paper says the override values survive function-level reset and a clean stock-driver
reload, while a cold power cycle clears them.

## Independently checked against NVIDIA 580

The official GA100 production Booter Load extracted from NVIDIA 580.105.08 is 60,160
bytes with SHA-256:

```text
edacea964e7199268b031ac0baac60358eb6845b38456c0b3e7dc8e2c35daf86
```

Envytools decodes its clear stub as Falcon v6. The stub writes and reads DMEM
`0x6340`:

```text
000000df: mov r15 0x2d706
000000e3: mov r9 0x6340
000000e7: st b32 D[r9] r15
000000eb: mov r15 0x6340
000000ef: ld b32 r15 D[r15]
```

The clear stub authenticates and calls the application at `0x100`. Its store/load
corroborates the address `0x6340` only; it does not independently establish RNG
reseeding or that the encrypted application uses that address in the vulnerable
canary epilogue. The remaining code through `0x86ff` is ciphertext. Community
addresses `0x10b9` and `0x810d` are inside that ciphertext, so raw disassembly cannot
establish their semantics. A decrypted IMEM dump or the author's emulator is
required.

NVIDIA's open `kernel_gsp.c` reads `.fwsignature_ga100` from the GSP ELF and uses the
section size for the signature allocation. `kernel_gsp_tu102.c` copies the aligned
size into the WPR metadata `sizeOfSignature`. This supports the host-side oversized
signature delivery route, but it does not prove the closed booter's memory corruption
or the productive continuation.

The paper's live procedure uses a driverless userland Falcon loader. This repository
instead relies on the NVIDIA module consuming a temporarily expanded installed GSP
ELF. The open-module data flow makes that alternative plausible, but it is not the
paper's loader and has not been independently demonstrated by this project. One
external 580.173.02/20c2 report is described below. NVIDIA's published 580
compatible-GPU tables still omit CMP IDs `2082` and `20c2`, so a stock module may
never bind far enough on another host to exercise this delivery path.

## Missing reproduction artifacts

The paper does not publish:

- the Falcon emulator, its configuration, or decrypted image;
- the driverless Falcon loader;
- the productive continuation/ROP derivation;
- a point release, GSP hash, module hash, board/subsystem ID, or VBIOS version;
- the complete PLM/register map or PCIe recipe;
- the static DMA checker's source/raw output and analyzed-image corpus;
- raw emulator-trace artifacts, identifiable silicon mailbox logs, benchmark logs,
  memory test code/output, or thermal logs.

It refers to a verification appendix that is not present. Its silicon cross-check is
described as prior work that was not rerun during the no-hardware drafting pass, and
no identifiable artifact for that prior work is supplied.

## Community prototype audit

The audited initial prototype commit is dated July 14, 2026. Its payload adds values
absent from the paper: canary replacement `0xfaceb13d`, frame start `0xff48`, stride
`0x18`, BAR0-write gadget `0x10b9`, tail `0x810d`, and host compute writes at
`0x82381c` and `0x823820`.

That repository should not be run as published:

- its compute path requests a missing `feat_ovr_plm` constant and always fails;
- the exception occurs before firmware restoration and there is no `try/finally`;
- it accepts A100 PCI ID `20b0` as though it were a CMP 170HX;
- it accepts arbitrary `580.x` firmware without a module/GSP/booter hash gate;
- it forces process termination and module removal and can leave services stopped;
- it overwrites the Turing signature sections in the shared GSP ELF;
- its tests do not execute the pipeline or validate payload semantics.

The initial implementation reused the productive frame constants and compute register
values, labeled them unverified, and repeated the compute PLM write in place of the
prototype's two undocumented HS frames. It added exact manifests, structural
preservation, readback gates, and restoration. Repeating PLM reduced the immediate
memory-side-effect surface but was speculative and was not semantically equivalent to
the original chain.

Live execution additionally accepts only unchanged bundled profiles under a fixed
CMP/device/register allowlist. It records a pre-boot PLM baseline that must differ from
the expected all-open value, fsyncs an audit record before `modprobe`, refuses to
overwrite unresolved journal/state evidence, and requires an acknowledged complete
cold power cycle before that state can be cleared. The PLM result is described as a
readback consistent with the continuation, not proof that this payload caused it; only
a correct before/after compute benchmark can establish the claimed hardware effect.

## `abobasixseven/unlock-cmp-170hx` audit

The guide was audited at commit
`8eb8046372611a557c98421bfc024a1e8c87f353`. The repository contains only a README:
no implementation, license, test, exact firmware manifest, raw log, screenshot, or
benchmark artifact. It points to `kinako404/cmpunlocker` at commit
`6cf67b319f8d14a396f6d905211071ff11076004` plus manual fixes. The fork at that commit
still has the missing PLM-key defect and does not contain the documented GPU
enumeration/initialization correction.

The useful evidence is Issue #1 in the guide repository. A reporter using PCI ID
`20c2` and driver 580.173.02 found that `modprobe nvidia` returned without actually
initializing the GPU/GSP when persistence services were absent. Calling `nvidia-smi`
after the patched load triggered initialization. After also correcting the missing PLM
configuration key, the run reached the prototype's PLM/compute-readback success path
and reported approximately 12.1 TFLOP/s FP32 and 6.3 TFLOP/s FP64 from `clpeak`.

That is a single-system report, not an independently reproducible artifact bundle. It
does not derive the encrypted control-flow addresses or register map. More importantly,
its `nvidia-smi` output remains at 8192 MiB and the reporter explicitly states that the
memory unlock was unsuccessful.

Unsupported guide claims were not promoted to evidence. Its 65,536-MiB output block is
constructed rather than observed; no artifacts support its daemon, 64-GB stability,
power-limit, llama.cpp, or broad driver-compatibility claims. The guide also tells users
to select an `unlocked_64gb` configuration that does not exist in the referenced fork.
That fork exposes only differently named 40-GB and 80-GB values.

## Pinned 580.173.02 integration

The official NVIDIA 580.173.02 `gsp_tu10x.bin` was independently parsed and pinned:

```text
firmware size       30,471,256
firmware SHA-256    6f3ccbd570c7ac2a7ea910d9d87fc3d23db9ae3dfe82020ea07b17a30954495e
GA100 signature     offset 0x1d0bf0f, size 0x1000
signature SHA-256   f259fed6a47aba40df33d159b4390e996e455f8ad3bceb43bcbdd99d11a13fec
booter SHA-256      edacea964e7199268b031ac0baac60358eb6845b38456c0b3e7dc8e2c35daf86
compute payload     7e776cf71bed542f11833b1fe193867b9d33d0da4866a6b2d61314bff5faeb60
patched firmware    6b57e314f980e0d2f343ee9604e59b440a668f6797e9358acf2b8a7333468c85
```

The new profile is restricted to `20c2`, the only ID in the report. It reproduces the
reported HS sequence exactly:

```text
0x009a0204 <- 0x02779000  FBPA_CFG1_UNVERIFIED
0x00100ce0 <- 0x0000020b  LMR_UNVERIFIED
0x00823804 <- 0xffffffff  FEAT_OVR_PLM
```

The first two are signed-payload side effects only. The host records their BAR0 values
before, after the reported resets, and after stock reload, but never writes them. The
result and durable state always set `memory_capacity_verified` to false.
After any patched-boot attempt, durable state conservatively marks the reported HS
side effects as potentially active even if the later PLM gate prevents every host
compute write.

The reported strategy adds a bounded `nvidia-smi` initialization probe after patched
`modprobe`. A nonzero result or timeout is recorded and tolerated because the patched
GSP path may intentionally fail normal device initialization; it cannot substitute for
the mandatory PLM readback. The strategy then performs FLR #1, clean module unload,
immediately restores stock firmware, performs FLR #2, and requires PLM `0x823804` to
read all-open before either host compute write. No force unload, client killing, or
service manipulation is available. A `finally` restoration remains as the failure
fallback; stock `nvidia-smi`, driver binding, and compute readback must then succeed.

This ordering reproduces the one reported procedure while retaining the paper's key
safety invariant: host overrides are attempted only through an observed open PLM. The
paper says override values survive FLR but warns that PLMs themselves may re-lock, so
the post-FLR gate is essential. It does not convert the report into proof of the guide's
memory or persistence claims.

## Supplied NVIDIA 610.43.03 module path

The later supplied bundle contains an official NVIDIA 610.43.03 runfile and an
already-built source tree with local edits and thousands of compiler artifacts. The
runfile is authentic: it is 461,538,429 bytes and its SHA-256 is
`45e2d4c134a23c35e50f253a4aa63e7e5e8d17e3d185d4a07c8a58e9612ed392`, exactly the
digest published by NVIDIA. The extracted `gsp_tu10x.bin` is 29,352,832 bytes with
SHA-256 `73065619db9ec921d19fc4e519dd04d91a9199b525eaca9b257b89fb8c5ec52c`.
Its decompressed GA100 production Booter is byte-identical to the three pinned 580
images. The bundle does not contain a final set of loadable `.ko` modules, and neither
the 461-MB runfile nor its stale object files belong in this repository.

After line-ending and build-output normalization, the supplied source has eleven
meaningful changes relative to NVIDIA's exact 610.43.03 release commit
`452cec62d827034798072827d3866d1881662b77`. Its intended sequence is materially
closer to the paper's mechanism than a capacity-number spoof alone:

1. For PCI device `20c2`, enlarge the stock signature allocation and repeatedly run
   the vendor-signed GA100 Booter with the community continuation.
2. Open framebuffer, WPR, WPR-configuration, and feature-override PLMs and read them
   back.
3. Host-write the SM, framebuffer geometry, and LMR overrides, restore the authentic
   signature, then continue the normal GSP boot.
4. Report 64 GiB through GSP static metadata, keep PRAMIN in the native 8-GiB
   aperture, avoid unsupported compressed/virtual scrub paths, and add the high
   framebuffer range to PMA.
5. Reapply the sequence at each patched-module initialization rather than relying on
   a root watchdog.

That is still not the paper's exact implementation. The paper uses a separate
driverless host loader, omits its productive continuation and full register map, and
reports 10-to-80-GB hardware rather than an identifiable `20c2` 8-to-64-GB run. Its
stable 80-GB result also changes a refresh interval at a measured throughput cost;
the supplied 610 path does not implement or derive that adjustment. It does not
implement the paper's XVE/root-port sequence either, so it provides no basis for a
PCIe Gen2 claim.

The supplied installer and edits are not safe to use unchanged:

- four PLM attempts can fail while initialization continues into host writes and
  fabricated 64-GiB metadata;
- a WPR2 error is weakened globally, including for unrelated GPUs;
- `gpuValidateRegOps` returns success unconditionally for every device;
- an optional root-controlled `dmem.bin` silently replaces the compiled payload;
- partial SS/CFG1/LMR writes are not rolled back;
- saved signature memory is leaked, region/PMA capacities are insufficiently
  bounded, and the late-PMA result is logged but ignored;
- the installer changes packages and userspace, stops services, attempts live
  module removal, overwrites configuration, masks some initramfs failures, and has
  no precise rollback to the prior module set;
- its instruction to treat early Booter errors as generally harmless is not a
  valid success criterion.

The linked `amoghmunikote/cmpunlocker` repository was separately audited at commit
`0a5f0624cc6f4cbbf3f2e8d357e891c4a64cc8a2`. It usefully separates patches and has
an uninstall script, but its first patch is malformed against its pinned source and
its profile rewrite currently matches no geometry constants. Its two screenshots
show a software-visible 65,536-MiB value and a compute result; they do not provide
raw kernel provenance, a full-range distinct-address test, error logs, thermals, or
long-duration stability evidence.

The quarantined `experimental/610-memory` integration therefore starts from the
exact official source commit and retains only `20c2`-scoped changes. It restores
stock behavior for every other device, keeps register-operation validation intact,
requires exact PLM and host-write readbacks before publishing expanded metadata,
validates region/PMA capacity, uses stable current-boot success markers, installs
only an auditable module set, and provides a reversible removal path. It deliberately
does not import the supplied runfile, objects, root one-shot installer, PCIe claim, or
screenshots.

Finally, `nvidia-smi` displaying 65,536 MiB is treated as necessary but not proof.
The validator allocates 8, 16, 32, 48, then 60 GiB on the exact PCI function and
writes several bijective address-dependent patterns to every 64-bit word. It emits
`LLM_READY` only if all reads match, the expected patched module is loaded, current-
boot unlock/PMA markers are complete, and no Xid or unlock-failure marker appears.
Even that result certifies only the tested patterns, temperature, and duration; it
cannot guarantee lifetime reliability of binned HBM.
