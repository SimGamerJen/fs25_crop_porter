# Changelog

## [0.3-alpha / v17 baseline] - 2026-09-29

### Added

- Reusable .cropporter.zip package export.
- Package probing and library listing.
- Single-package and multi-package apply workflows.
- Optional complete target-map ZIP generation.
- Shared/sibling foliage-family reconciliation.
- Runtime dependency discovery for local crop assets.
- Token-aware crop-family matching to avoid substring collisions such as RYE vs GREENRYE/VETCHRYE.
- Detection of inline <fruitTypes> registries in map descriptors.
- Supplemental local fill-type and density-map-height-type registry support where the target has no suitable local registry.
- Append-only I3D density-map reference replacement.
- densityMap_fruits GDM/GRLE conversion and fruit type-index channel promotion.
- terrainDetailHeight layout inspection and type/height-bit repacking.
- Safe retirement of stale compiled densityMap_height.gdm files after PNG expansion.
- Narrow v0.3 GDM encoder used by explicit save migration and self-tests.
- Explicit migrate-save command with dry-run, timestamped backup, staging and GIANTS-converter round-trip validation.
- Internal regression tests for crop matching, asset scope, inline fruit registries, supplemental height registries, append-only density references, RGB height migration and GDM round trips.

### Validated

Current 12-package BallySpring source set successfully applied to two materially different target architectures:

- Castlereagh
  - existing local registries;
  - terrain-detail-height expansion;
  - fresh-save load/save/reload;
  - explicit established-save migration.
- Hobo's Hollow
  - inline fruit registry;
  - direct GDM references;
  - no original local height registry;
  - supplemental height registry;
  - 12 -> 14 channel terrain-detail-height expansion;
  - fresh-save load/save/reload.

### Safety / behaviour changes

- Existing target I3D <File> entries are treated as append-only.
- Density conversion appends a replacement PNG file entry and repoints the active layer instead of rewriting an existing file entry.
- Normal map/package apply operations do not modify savegames.
- Save mutation occurs only through the explicit migrate-save command.
- The porting engine remains structure-driven; no target map names or fixed I3D file IDs are used as compatibility switches.

### Known limitations

- The tool is still alpha and is not proven against every FS25 map architecture.
- Real pre-existing non-zero loose-material heap preservation has passed synthetic tests but has not yet been field-tested in an established real save.
- Plantation/vine workflows remain experimental.
- Vehicle/tool compatibility, sell points, contracts and economy integration are not comprehensively migrated.
- The development script contains a machine-specific default GRLE converter path; use --grle-converter on other systems.

---

## [0.1.0-alpha] - 2026-05-26

### Added

Initial public alpha release of FS25 CropPorter.

Core field-crop workflow:

- Added source map scanning.
- Added crop probing.
- Added preflight reporting.
- Added apply workflow for standard field-style crops.
- Added JSON and Markdown reports.
- Added crop asset copying.
- Added .i3d.shapes dependency copying.
- Added fruitType registry insertion.
- Added fillType insertion.
- Added densityMapHeightType insertion where detected.
- Added l10n patching.
- Added fillTypeCategory patching.
- Added fruitTypeCategory patching.
- Added map i3d foliage layer copying.
- Added i3d ID remapping for conflicting fileId, fruitId, and foliageId values.
- Added density config probing.
- Added density channel config patching.
- Added fruit registry probing.

### Tested

Confirmed working field-crop ports:

- BLACKBEAN
- PINTOBEAN

Tested into:

- Estancia Lapacho
- BR163 Brazil

### Known limitations

- Alpha tool.
- Field crops only.
- Plantation/vine/row crops were not supported in the v0.1 release.
- Production-chain migration was not supported.
- Vehicle migration was not supported.
- Existing saves could lose compatibility when map identity changed.
- Density map binary/channel data was not fully handled in v0.1.
