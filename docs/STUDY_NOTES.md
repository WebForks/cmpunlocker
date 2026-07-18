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
paper's loader and has not been demonstrated on CMP hardware. In particular, NVIDIA's
published 580 compatible-GPU tables omit CMP IDs `2082` and `20c2`, so a stock module
may never bind far enough to exercise this delivery path.

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

This implementation reuses only the productive frame constants and register values,
labels them unverified, repeats the compute PLM write in place of the prototype's two
undocumented HS BAR0/register writes, and adds exact manifests, structural
preservation, readback gates, and restoration. Those changes reduce operational risk;
they do not turn missing exploit evidence into hardware validation.

Live execution additionally accepts only unchanged bundled profiles under a fixed
CMP/device/register allowlist. It records a pre-boot PLM baseline that must differ from
the expected all-open value, fsyncs an audit record before `modprobe`, refuses to
overwrite unresolved journal/state evidence, and requires an acknowledged complete
cold power cycle before that state can be cleared. The PLM result is described as a
readback consistent with the continuation, not proof that this payload caused it; only
a correct before/after compute benchmark can establish the claimed hardware effect.
