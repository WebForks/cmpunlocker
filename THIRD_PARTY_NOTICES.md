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

The README-only `abobasixseven/unlock-cmp-170hx` guide and its issue tracker were
used as factual evidence about one reported run; no source code was copied from
that unlicensed guide repository.
