# Third-party notices

The productive payload frame layout and these constants were derived from the
GPL-2.0-only `cmpunlocker` prototype at commit
`9b9fb2f27a618f13e6b016adfc6e86b1e60fa84d`:

- DMEM payload layout and replacement canary
- frame start, stride, and field offsets
- Falcon BAR0-write gadget and tail return addresses
- compute PLM and host override register values
- the original FBPA/LMR/PLM Heavy Secure frame sequence

The original repository was removed shortly after publication. A public mirror is
available at <https://github.com/fulracoco/cmpunlocker>.

The implementation here is a substantial rewrite and remains licensed under
GPL-2.0-only in compliance with the source license.

The experimental NVIDIA 610.43.03 kernel-module patch was independently
reconstructed against NVIDIA's `open-gpu-kernel-modules` commit
`452cec62d827034798072827d3866d1881662b77`. Its CMP-specific Booter, framebuffer,
PMA, PRAMIN, scrubber, and persistent-state mechanics were informed by the
GPL-2.0-only `amoghmunikote/cmpunlocker` repository at commit
`0a5f0624cc6f4cbbf3f2e8d357e891c4a64cc8a2` and by the separately supplied
610.43.03 source tree. The patch in this repository is a fail-closed rewrite;
it is not a verbatim copy of either patch set. NVIDIA's upstream files retain
their original dual MIT/GPL notices when the patch is applied.

No NVIDIA runfile, firmware image, prebuilt kernel module, build artifact, or
third-party screenshot is redistributed. The build workflow fetches the exact
official source commit, and the operator must separately obtain the matching
NVIDIA userspace and firmware under NVIDIA's terms.

The README-only `abobasixseven/unlock-cmp-170hx` guide and its issue tracker were
used as factual evidence about one reported run; no source code was copied from
that unlicensed guide repository.
