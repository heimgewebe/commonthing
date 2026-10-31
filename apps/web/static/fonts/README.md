# Commonthing Garn

Commonthing's original textile treatment lives in `src/app.css` and
`src/lib/styles/tokens.css`. It is a CSS optical relief system, not a new font
or a modification of the open-source outlines. B is the single product default;
A/C exist only in the temporary visual review outside this repository.

The unmodified base is **Atkinson Hyperlegible Next**, Latin, normal, variable
weight 200–800, Fontsource version 5.3.0. Local `font-display: swap` keeps fallback
ink visible. Native controls and metadata inherit the same family. Existing
sizes, line heights and weights are retained.

- Font source: <https://cdn.jsdelivr.net/fontsource/fonts/atkinson-hyperlegible-next:vf@5.3.0/latin-wght-normal.woff2>
- Matching package license: <https://cdn.jsdelivr.net/npm/@fontsource-variable/atkinson-hyperlegible-next@5.3.0/LICENSE>
- Project: <https://github.com/googlefonts/atkinson-hyperlegible-next>
- License: SIL OFL-1.1; the accompanying license is copied verbatim.
- WOFF2: 33,996 bytes; SHA-256 `18b2a1a39a2fa298b0ba5390aca68462669826c90925656f1c1f6796e0e1bbaf`.

The continuous optical grades use a near-invisible edge at <=13px, a small
highlight/shade pair at 14–20px and capped fine relief at >=22px. They use opaque
ink and standard text shadows: no missing strokes, internal noise, masks,
filters, duplicated text or selection overlays. Small sizes deliberately show
less textile relief. No Safari-specific compositing is required; actual browser
validation for this change uses Chrome, not Safari.

Map markers and CSS icons retain their artwork. The map subtree is excluded
from relief. **MapLibre tile labels use glyph atlases**, not this DOM font;
this change makes no claim about their font. MapLibre's own control typography
also remains under its stylesheet.

The final production measurement is 3,492 bytes login CSS gzip (previously
3,163), and 3,683 bytes settings CSS gzip. The canonical route contract therefore
raises only those CSS caps from 3,200 to 3,550 and 3,600 to 3,750 respectively.
The separately fetched WOFF2 costs 33,996 response-body bytes once per cold
cache; it is not counted as initial CSS. There are no runtime font-CDN requests
or additional packages. Font plus license occupies 38,595 bytes.
