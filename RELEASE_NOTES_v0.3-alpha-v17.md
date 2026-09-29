# FS25 CropPorter v0.3-alpha — v17 Baseline

Build marker:

~~~text
supplemental-height-registry-pipeline-v17
~~~

Reference script:

~~~text
fs_25_crop_porter_v_0_3.py
~~~

Source SHA256:

~~~text
fe3231006edc7fc8c52241206f588a5b88db4d80ef25758f1830f5038e4a4af3
~~~

## Why this baseline matters

v17 is the first Python baseline in this project that has passed the current portability gate across two materially different FS25 target-map architectures using the same generic engine.

It is being retained as the reference point before development moves to a separate C#/.NET/WPF desktop application.

## Major capabilities

- Reusable .cropporter.zip packages.
- Multi-package target builds.
- Shared/sibling foliage-family handling.
- Runtime asset dependency resolution.
- Inline or separate fruit-registry handling.
- Supplemental target registries where needed.
- Append-only I3D density-map reference replacement.
- Fruit-density capacity expansion.
- Terrain-detail-height channel/type-index expansion.
- Logical height-bit preservation during layout changes.
- Explicit established-save height-map migration with backup and round-trip validation.
- JSON/Markdown apply and migration reports.
- Internal self-tests covering the portability fixes accumulated through v17.

## Current validation set

Twelve BallySpring crop packages:

- barley;
- wheat;
- triticale;
- rye;
- clover;
- fieldgrass;
- flowering catch crop;
- green rye;
- humus active;
- mustard;
- silage maize;
- vetch rye.

Barley and wheat packages also reconcile their winter sibling family assets.

## Target validation

### Castlereagh

Passed:

- multi-package apply;
- static registry/I3D validation;
- fresh save;
- save/quit/reload;
- explicit established-save height-map migration;
- reload after migration.

### Hobo's Hollow

Passed:

- multi-package apply;
- inline fruit registry handling;
- direct GDM-to-PNG append-only conversion;
- supplemental height registry;
- terrain-detail-height 12 -> 14 channel conversion;
- fresh save;
- save/quit/reload.

## Important caveat

The real established Castlereagh save used for the final migration test had an all-zero densityMap_height.gdm.

Synthetic non-zero type/height preservation tests pass, but preservation of an actual pre-existing non-zero loose-material heap has not yet been field-tested in FS25.

## Not a universal guarantee

v17 should be treated as the best current portability baseline, not as proof that every custom FS25 map can be converted automatically.

Future failures should add reusable structural capability and regression coverage rather than map-specific exceptions.
