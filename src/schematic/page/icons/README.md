# Calcite UI icons

Three icons from Esri's [Calcite Design System][calcite], vendored verbatim:

| File | Used for |
|---|---|
| `map-16.svg` | the map view |
| `connection-to-connection-16.svg` | the linear view |
| `clock-16.svg` | the time chart |

**Do not edit these files.** Their licence permits redistribution *without
modification* only:

> COPYRIGHT Esri. All rights reserved under the copyright laws of the United
> States and applicable international laws, treaties, and conventions. This
> material is licensed for use under the Esri Master License Agreement (MLA)…
> You may redistribute and use this code without modification, provided you
> adhere to the terms of the MLA and include this copyright notice.

So they are never inlined as hand-edited path data. The site applies them as
CSS masks over the copied files, and `animate.py` base64-encodes the file bytes
into a `data:` URI, because an animation page has to stay one self-contained
file. Both routes ship the same bytes that came from Esri.

This is the only third-party asset here that is not open source, and it is
credited on the site's Framework page.

[calcite]: https://developers.arcgis.com/calcite-design-system/icons/
