# NVIDIA 610.43.03 CMP 170HX experimental memory patch

## Status and exact base

This patch is an experimental, fail-closed implementation for NVIDIA PCI device `10de:20c2` only. It is not a claim that a 64 GiB configuration is safe on any particular board, and it must not be treated as accepted unless it passes a real cold-boot validation and destructive memory test on disposable hardware.

The only supported source base is NVIDIA's official open-gpu-kernel-modules commit:

```text
tag:     610.43.03
commit:  452cec62d827034798072827d3866d1881662b77
patch:   patches/0001-cmp170hx-61043-experimental-memory.patch
sha256:  f377efcb000035449a4520c3f306d0983c4de9b3dbe8a71f2ee616a5c0571c6b
```

The patch is intentionally kept separate from the repository's stable path. Do not apply it to a different driver version or a vendor-modified source tree.

## Clean-build verification

The current `f377efcb...` revision passes exact-base patch application,
whitespace, source-equivalence, and static checks. A fresh clean build against
Ubuntu `6.8.0-134-generic` headers produced all five modules with driver version
`610.43.03` and matching vermagic. The final linked `nvidia.ko` contains
`memmgrCmp170hxLateExtendHighPmaRegion` plus all required gate, unlock,
metadata, PMA-success, PMA-overlap, PMA-NUMA, PMA-overflow, and PMA-init-failure
markers.

That build proves only that the reviewed source path reached loadable modules.
It does not prove that a module loads on a CMP 170HX or that
memory above the native 8 GiB is physically present, non-aliased, stable, or
adequately refreshed.

## What was carried over from the supplied archive

The supplied `other-project/cmp170hx-unlock/open-gpu-kernel-modules-610.43.03` tree contains a memory-unlock path built around the SEC2/Booter post-boot timing behavior. This patch preserves the functional parts of that path:

- a built-in `0xf800` Booter payload, with no external `dmem.bin` dependency;
- device-PLM writes for `WPR_CFG`, `FBPA`, `WPR`, and `FEAT`;
- host writes for `SS0`, `SS1`, `CFG1`, and `LMR`;
- restoration of the stock GSP signature before normal GSP bootstrap;
- a validated 8 GiB-to-64 GiB GSP static-memory descriptor extension;
- late PMA registration of the high range only after PMA already exists;
- the supplied 20c2-only persistent-state, BAR0/PRAMIN, scrub-PTE, and CE-mode workarounds.

The production GA100 Booter embedded in official 610.43.03 is the same Booter used by the repository's established profiles. Audit hashes recorded for that official payload are:

```text
compressed SHA-256:   7529534162bc4d5d17d26d998d5f7413af2526fd0ccf38f0018548d64237fe71
decompressed SHA-256: edacea964e7199268b031ac0baac60358eb6845b38456c0b3e7dc8e2c35daf86
```

## Why the BAR0 and scrub changes remain

These are not optional cosmetic changes once RM advertises a 64 GiB FB address space:

- `kbusSetupDefaultBar0Window()` would otherwise derive the default PRAMIN window from the synthetic 64 GiB top. For `20c2` only, the patch keeps PRAMIN at the top of the original 8 GiB aperture so boot/control accesses do not move into the newly exposed allocation range.
- The high PMA region is deliberately non-compressible. The 20c2 scrubber therefore uses the generic-memory PTE kind, and CE utilities avoid the virtual/comptag path that assumes the stock compressed-memory layout.

Every one of these branches is gated by `(PCIDeviceID >> 16) == 0x20C2`. Other NVIDIA device IDs retain the stock path.

## Hardening added here

The supplied tree was useful research material, but it was not suitable to merge directly. This implementation adds the following controls:

1. The NVIDIA stock WPR2 rejection remains unchanged for every non-20c2 GPU. Recovery past an already-raised WPR2 is permitted only inside the exact 20c2 gate.
2. Each required PLM is attempted at most twice and must have an exact MMIO readback. Missing even one readback aborts before host writes or memory metadata changes.
3. The Booter return code remains diagnostic when the exact target PLM readback proves that specific write occurred. A nonzero Booter status is **not** considered generically harmless and never substitutes for readback.
4. All four original host-register values are captured before the first host write. Every target write is read back. A partial sequence is restored in reverse order with readback; incomplete rollback emits `COLD_POWER_CYCLE_REQUIRED`.
5. Stock-signature ownership is explicit. A fully allocated and populated replacement descriptor is prepared before the payload descriptor is released. The original signature bytes remain owned until teardown so a controlled GSP boot retry can safely re-arm and restore the payload/signature pair.
6. GSP metadata is committed only for an exact, internally consistent 8 GiB starting layout with a valid, reserved final region. Region count is checked before indexing.
7. Late PMA registration validates FB-region count, one unambiguous fully reserved candidate, alignment, non-overlap, PMA array capacity, FB array capacity, registration status, managed-range coverage, and total-memory growth before publishing the high range. Every registered PMA descriptor is checked with the overflow-safe inclusive intersection rule `newBase <= oldLimit && oldBase <= newLimit`; the containment-oriented `pmaIsPmaManaged()` helper is not used as a pre-registration overlap test. Reserved/usable RAM accounting is converted by exactly the registered growth only after those checks pass.
8. The supplied global `gpuValidateRegOps()` bypass is not included. There is no global debug-register access relaxation.
9. No external payload file is read. This avoids an unversioned, root-writable firmware-path input to kernel MMIO behavior.

The late-PMA call is deliberately placed immediately after a successful `RmInitNvDevice()` in `RmInitAdapter()`. At that point `pGpu` has already been obtained from `gpumgrGetGpu()`, GSP initialization has consumed and validated the extended static FB metadata, and `RmInitNvDevice()` has created the stock PMA. It still runs before expanded GPUMGR visibility is disabled, environment verification begins, interrupts are enabled, or a post-init consumer can allocate FB. Placing this call near `osInitNvMapping()` is invalid because `pGpu` is still null there and the compiler correctly removes the entire guarded block.

The late extension rejects NUMA-backed PMA objects. For the supported non-NUMA path, `pmaGetTotalMemory()` sums the sizes of all registered PMA maps, while `pmaRegisterRegion()` creates a map containing exactly `(limit - base + 1) / PMA_GRANULARITY` aligned frames. The required post-registration invariant is therefore exact: `totalAfter == totalBefore + expectedGrowth`. A larger delta is not accepted as harmless; it indicates unexpected concurrent or internal PMA state and fails closed.

## Sole generic allocator safety change

`pmaRegisterRegion()` is shared code and originally dereferences an unchecked region-descriptor allocation. Its blacklist-registration failure path also destroys allocations without clearing the published slots or decrementing `regSize`. A late registration could therefore crash or leave a corrupt PMA object instead of returning an error.

The patch adds the missing null check and restores the published map,
descriptor-slot, and `regSize` state on those failure paths. That is the state
used by this experiment's synchronous (`bAsyncEccScrub = NV_FALSE`),
zero-blacklist registration. It does not claim to restore the separate generic
async-scrub atomic flag for an unrelated asynchronous caller. Successful
registration, including successful non-20c2 registration, keeps the original
ordering and values. This is the sole source change that is not selected by a
20c2 branch; it exists so the new fail-closed caller can safely rely on the
shared API.

## Runtime log contract

All experiment-owned messages use the unique `cmpunlock610:` prefix. A valid current-boot window must contain, in order of dependency:

```text
cmpunlock610: gate-active device=10de:20c2
cmpunlock610: plm-ok name=WPR_CFG ...
cmpunlock610: plm-ok name=FBPA ...
cmpunlock610: plm-ok name=WPR ...
cmpunlock610: plm-ok name=FEAT ...
cmpunlock610: host-ok name=SS0 ...
cmpunlock610: host-ok name=SS1 ...
cmpunlock610: host-ok name=CFG1 ...
cmpunlock610: host-ok name=LMR ...
cmpunlock610: unlock-ok device=10de:20c2
cmpunlock610: metadata-ok bytes=0x0000001000000000 regions=<n>
cmpunlock610: pma-ok regions-before=<n> capacity=32 base=<address> limit=<address> total=<bytes>
```

Validation must reject any `cmpunlock610: fail `, `COLD_POWER_CYCLE_REQUIRED`, NVIDIA Xid, or fallen-off-bus line from the same boot. A `plm-ok` line also records `booter-status`; acceptance still depends on exact readback and every downstream marker.

## Deliberately not implemented

There is no PCIe Gen2 unlock in the supplied modified source, despite the claim in `other-project/Post.txt`. No reviewed device/root-port retrain implementation or reliable rollback was present. This patch therefore makes no PCIe speed claim and does not write PCI configuration space.

The supplied archive also contained build artifacts and a large NVIDIA `.run` file but no built `.ko` proving that its modified source compiled. Those artifacts are not part of this patch and are not evidence of runtime success.

## Acceptance boundary

A clean patch application and successful module build are necessary but not sufficient. Hardware acceptance requires a full AC power-off cycle, the complete current-boot marker set above, the expected 65536 MiB report, no Xids, and a destructive test spanning the high memory range. Until all of those pass, keep this experiment out of the main/stable installer path.
