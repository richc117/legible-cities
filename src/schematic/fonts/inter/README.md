# Inter

The engine's `inter` label face (`Style.label_font`, issue 76): Inter 4.1,
weight 400, whose name table says "Version 4.001;git-9221beed3", under the SIL
Open Font License 1.1. Made by `bin/build-fonts`; rebuild with it, never edit
a file here by hand, since `tests/test_fonts.py` holds the table to the font.

| File | What it is |
|---|---|
| `regular.woff2` | the subset, as WOFF2: 10,672 bytes, 14,232 as base64 |
| `advances.json` | each code point's advance in font units, 2,048 to the em, with the average (1171.07) and the ranges |
| `OFL.txt` | the licence, copied from the release |

- Source: `web/Inter-Regular.woff2` from the release file
  <https://github.com/rsms/inter/releases/download/v4.1/Inter-4.1.zip>, sha256
  `9883fdd4a49d4fb66bd8177ba6625ef9a64aa45899767dde3d36aa425756b11e`.
  `OFL.txt` is its `LICENSE.txt`.
- Subset: U+0020-U+007E, U+00A0-U+017F, U+2010-U+2026. The face maps 340 of
  their 342 code points; not in it: U+00AD, U+0149. A name with one of those
  is measured at the average, and the browser draws that character in the next
  face of the stack.
- Made with fontTools 4.66.1 and brotli 1.2.0, unhinted and without OpenType
  layout features (no kerning, ligatures or alternates), so a name is drawn
  exactly as wide as the sum of its advances, which is what the placer
  measures. Name records 0, 1, 2, 3, 4, 5, 6, 13, 14 are kept, the licence's
  description and URL among them.

The licence opens:

> Copyright (c) 2016 The Inter Project Authors (https://github.com/rsms/inter)

It names no Reserved Font Name. Subsetting and a change of format make a
Modified Version (OFL FAQ 2.2 and 2.6), which the licence allows, and without
a reserved name the subset keeps the family name Inter. An SVG the engine
writes with this face embeds the subset, which the licence allows in full or
in part (OFL FAQ 1.12).
