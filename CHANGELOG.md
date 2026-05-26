# Changelog

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
- Added `.i3d.shapes` dependency copying.
- Added `fruitType` registry insertion.
- Added `fillType` insertion.
- Added `densityMapHeightType` insertion where detected.
- Added l10n patching.
- Added `fillTypeCategory` patching.
- Added `fruitTypeCategory` patching.
- Added map i3d foliage layer copying.
- Added i3d ID remapping for conflicting `fileId`, `fruitId`, and `foliageId` values.
- Added density config probing.
- Added density channel config patching.
- Added fruit registry probing.

### Tested

Confirmed working field-crop ports:

- `BLACKBEAN`
- `PINTOBEAN`

Tested into:

- Estancia Lapacho
- BR163 Brazil

### Known limitations

- This is an alpha tool.
- Field crops only.
- Plantation/vine/row crops are not supported in v0.1.
- Coffee is not supported in v0.1.
- Production-chain migration is not supported.
- Vehicle migration is not supported.
- Existing saves may not recognise generated map folders as the same map.
- Density map binary/channel data is not fully validated or expanded.
- Some maps use non-standard XML layouts that may require manual intervention.

### Notes

Always test generated maps in a disposable savegame.
