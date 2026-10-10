# Atkinson Hyperlegible Next

The engine's `atkinson-hyperlegible-next` label face (`Style.label_font`,
issue 76): Atkinson Hyperlegible Next 2.001, weight 400, whose name table says
"Version 2.001; ttfautohint (v1.8.4.7-5d5b)", under the SIL Open Font License
1.1. Made by `bin/build-fonts`; rebuild with it, never edit a file here by
hand, since `tests/test_fonts.py` holds the table to the font.

| File | What it is |
|---|---|
| `regular.woff2` | the subset, as WOFF2: 9,396 bytes, 12,528 as base64 |
| `advances.json` | each code point's advance in font units, 1,000 to the em, with the average (532.05) and the ranges |
| `OFL.txt` | the licence, copied from the release |

- Source:
  <https://raw.githubusercontent.com/googlefonts/atkinson-hyperlegible-next/5d633f80fc654ef5fffa7cfc257528685158dcef/fonts/ttf/AtkinsonHyperlegibleNext-Regular.ttf>,
  sha256 `88ed5c31a71584c7772963b02d04bef1eb7e3d2e9c8b9cb204339b1f82cf432c`.
  `OFL.txt` is
  <https://raw.githubusercontent.com/googlefonts/atkinson-hyperlegible-next/5d633f80fc654ef5fffa7cfc257528685158dcef/OFL.txt>,
  sha256 `aca6a428580965d2297d1b718042dd427c2a9443ece3b0d02d758e161e0c4030`.
- Subset: U+0020-U+007E, U+00A0-U+017F, U+2010-U+2026. The face maps 294 of
  their 342 code points; not in it: U+00AD, U+00B5, U+0108, U+0109, U+0114,
  U+0115, U+011C, U+011D, U+0124, U+0125, U+0128, U+0129, U+012C, U+012D,
  U+0134, U+0135, U+0138, U+013F, U+0140, U+0149, U+014A, U+014B, U+014C,
  U+014D, U+014E, U+014F, U+0156, U+0157, U+015C, U+015D, U+0166, U+0167,
  U+0168, U+0169, U+016C, U+016D, U+017F, U+2010, U+2011, U+2012, U+2015,
  U+2016, U+2017, U+201B, U+201F, U+2023, U+2024, U+2025. A name with one of
  those is measured at the average, and the browser draws that character in
  the next face of the stack.
- Made with fontTools 4.66.1 and brotli 1.2.0, unhinted and without OpenType
  layout features (no kerning, ligatures or alternates), so a name is drawn
  exactly as wide as the sum of its advances, which is what the placer
  measures. Name records 0, 1, 2, 3, 4, 5, 6, 13, 14 are kept, the licence's
  description and URL among them.

The licence opens:

> Copyright 2020-2024 The Atkinson Hyperlegible Next Project Authors
> (https://github.com/googlefonts/atkinson-hyperlegible-next)

It names no Reserved Font Name. Subsetting and a change of format make a
Modified Version (OFL FAQ 2.2 and 2.6), which the licence allows, and without
a reserved name the subset keeps the family name Atkinson Hyperlegible Next.
An SVG the engine writes with this face embeds the subset, which the licence
allows in full or in part (OFL FAQ 1.12).
