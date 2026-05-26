# FS25 CropPorter Commands

## scan-source

Scans a source map and lists detected crops.

```powershell
py .\python\Crop_Porter\fs_25_crop_porter_v_0_1.py scan-source `
  '.\mods\FS25_SourceMap.zip' `
  --include-basegame
```

## probe-crop

Inspects one crop and reports detected dependencies.

```powershell
py .\python\Crop_Porter\fs_25_crop_porter_v_0_1.py probe-crop `
  '.\mods\FS25_SourceMap.zip' `
  blackbean
```

## preflight

Checks whether a crop looks ready to apply into a target map.

```powershell
py .\python\Crop_Porter\fs_25_crop_porter_v_0_1.py preflight `
  --source '.\mods\FS25_SourceMap.zip' `
  --target '.\mods\FS25_TargetMap.zip' `
  --crops blackbean `
  --output '.\cropporter_reports\source_to_target_blackbean'
```

## apply

Applies the crop to a copied output map folder.

```powershell
py .\python\Crop_Porter\fs_25_crop_porter_v_0_1.py apply `
  --source '.\mods\FS25_SourceMap.zip' `
  --target '.\mods\FS25_TargetMap.zip' `
  --crops blackbean `
  --output '.\mods\FS25_TargetMap_CropPorted_Blackbean_v1'
```

## patch-density-config

Patches detected `densityMap_fruits` i3d channel configuration.

```powershell
py .\python\Crop_Porter\fs_25_crop_porter_v_0_1.py patch-density-config `
  '.\mods\FS25_TargetMap_CropPorted_Blackbean_v1'
```

## probe-fruit-registry

Shows the active fruit registry entries detected in the generated map.

```powershell
py .\python\Crop_Porter\fs_25_crop_porter_v_0_1.py probe-fruit-registry `
  '.\mods\FS25_TargetMap_CropPorted_Blackbean_v1'
```

## probe-density

Reports detected fruit density layer configuration.

```powershell
py .\python\Crop_Porter\fs_25_crop_porter_v_0_1.py probe-density `
  '.\mods\FS25_TargetMap_CropPorted_Blackbean_v1' `
  --include-fruits
```
