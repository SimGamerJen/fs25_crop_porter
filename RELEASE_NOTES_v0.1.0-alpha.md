# FS25 CropPorter v0.1.0-alpha

This is the first public alpha release of **FS25 CropPorter**, an experimental crop-porting assistant for Farming Simulator 25 map modders and advanced users.

## What this release is for

v0.1.0-alpha focuses on **standard field-style custom crops**.

It can help port crops such as dry beans, pulses, and cereal-style crops from one FS25 map into another by copying detected assets and patching the required XML/i3d references.

This release has been tested with:

- `BLACKBEAN`
- `PINTOBEAN`

## What this release is not for

This release does not support plantation, vine, row-placeable, greenhouse, or production-only crop systems.

Not supported in v0.1:

- Coffee rows
- Grapes/olives-style systems
- Tea/cocoa/orchard systems
- Production-chain migration
- Vehicle migration
- Universal one-click crop conversion

## Safety warning

This is an alpha tool.

It modifies copied map folders and XML/i3d configuration files. Always use a copied map and a disposable savegame.

Do not run this directly against your only working map or an important save.

## Main commands

```powershell
scan-source
probe-crop
preflight
apply
patch-density-config
probe-fruit-registry
probe-density
```

## Example workflow

```powershell
py .\python\Crop_Porter\fs_25_crop_porter_v_0_1.py scan-source `
  '.\mods\FS25_SourceMap.zip' `
  --include-basegame
```

```powershell
py .\python\Crop_Porter\fs_25_crop_porter_v_0_1.py probe-crop `
  '.\mods\FS25_SourceMap.zip' `
  blackbean
```

```powershell
py .\python\Crop_Porter\fs_25_crop_porter_v_0_1.py preflight `
  --source '.\mods\FS25_SourceMap.zip' `
  --target '.\mods\FS25_TargetMap.zip' `
  --crops blackbean `
  --output '.\cropporter_reports\source_to_target_blackbean'
```

```powershell
py .\python\Crop_Porter\fs_25_crop_porter_v_0_1.py apply `
  --source '.\mods\FS25_SourceMap.zip' `
  --target '.\mods\FS25_TargetMap.zip' `
  --crops blackbean `
  --output '.\mods\FS25_TargetMap_CropPorted_Blackbean_v1'
```

```powershell
py .\python\Crop_Porter\fs_25_crop_porter_v_0_1.py patch-density-config `
  '.\mods\FS25_TargetMap_CropPorted_Blackbean_v1'
```

## Please report issues with

- Source map
- Target map
- Crop name
- Command used
- CropPorter report files
- FS25 log errors/warnings
- Whether the generated map loads in a disposable save
