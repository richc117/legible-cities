# Phosphor icons

Four icons from [Phosphor Icons][phosphor], regular weight, taken unmodified
from the `@phosphor-icons/core` **2.1.1** release on npm (`assets/regular/`),
under the MIT licence; its text is the `LICENSE` file beside them.

| File | Used for |
|---|---|
| `map-trifold.svg` | the geographic view |
| `graph.svg` | the schematic view |
| `line-segments.svg` | the linear view |
| `clock.svg` | the time chart |

The repository's git tags stop at 2.0.8, so the npm release is the source.
Regular, not light: at 16px the regular weight's stroke is one device pixel
at 1x, and light's breaks up there.

Copy a replacement over the file, never edit one by hand, so the next person
can check it against the release. The site applies the files as CSS masks
over the copies Eleventy passes through (`site/.eleventy.js`), and
`animate.py` base64-encodes the file bytes into a `data:` URI, because an
animation page has to stay one self-contained file. Both routes ship the same
bytes. The page draws the glyphs at 16px over `currentColor`, so they take
the theme and invert with the pressed state; none needs a turn.

These replaced four icons from Esri's design system, whose Master License
Agreement forbids combining them in a manner that would subject them to
GPL-style terms, and this repository is GPL-3.0-or-later (issue 19). The
desktop app draws its own chrome in Phosphor too.

[phosphor]: https://phosphoricons.com/
