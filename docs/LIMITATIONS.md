# Limitations

FS25 CropPorter v0.1.0-alpha is limited to standard field-style crops.

## Supported crop style

A crop is most likely to work if it uses:

- `fruitType` XML
- `fillType` XML
- foliage i3d
- foliage `.i3d.shapes`
- standard map i3d foliage layer entries
- standard fillType/fruitType category entries
- standard l10n labels

## Not supported in v0.1

- Plantation crops
- Vine crops
- Row-placeable crops
- Coffee rows
- Greenhouse-only crops
- Custom Lua-driven crops
- Production-only crops
- Vehicle migration
- Production migration
- Pallet migration
- Full sell-point migration
- Savegame conversion

## Density map limitations

CropPorter can patch i3d density channel configuration, but it does not guarantee binary/image density map compatibility.

Always test generated maps in a disposable save.

## Existing save limitations

Changing the map mod folder name can make existing saves unable to find the map.

Use stable folder names for continued testing, or create new disposable saves.
