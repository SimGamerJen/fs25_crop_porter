# FS25 CropPorter

**FS25 CropPorter** is an experimental Python utility for Farming Simulator 25 map modders and advanced users. It helps analyse and port **standard field-style custom crops** from one map into another by copying crop assets and patching the relevant XML/i3d references.

This is an **alpha tool**. It is not a one-click universal crop converter.

Current focus:

- Field crops
- Cereal-style crops
- Bean/pulse-style crops
- Standard `fruitType` / `fillType` / foliage-based crops

Tested examples:

- `BLACKBEAN`
- `PINTOBEAN`

Tested target workflows:

- Pirambeiras → Estancia Lapacho
- Pirambeiras / 3 Marias → BR163 Brazil

---

## Status

Current public release:

```text
FS25 CropPorter v0.1.0-alpha
```

This release is intended for public testing by users who are comfortable working with extracted FS25 map folders, XML files, map i3d files, and disposable test saves.

---

## What it does

CropPorter can:

- Scan a source map for detected crops
- Probe a selected crop and its dependencies
- Run a preflight report before applying changes
- Copy detected crop assets
- Copy `.i3d` and `.i3d.shapes` foliage dependencies
- Insert `fruitType` registry entries
- Insert `fillType` entries
- Insert `densityMapHeightType` entries where detected
- Patch `fillTypeCategory` entries
- Patch `fruitTypeCategory` entries
- Patch l10n entries
- Patch map `.i3d` foliage layer references
- Remap conflicting i3d IDs such as `fileId`, `fruitId`, and `foliageId`
- Patch fruit density channel configuration
- Generate JSON and Markdown reports

---

## What it does not do

CropPorter v0.1 does **not** currently support:

- Plantation crops
- Vine crops
- Row-placeable crops
- Coffee rows
- Grapes/olive-style custom systems
- Greenhouse-only crops
- Crops that require custom Lua
- Production-chain migration
- Vehicle migration
- Harvester/tool migration
- Guaranteed savegame migration
- Guaranteed compatibility with every map

Plantation/vine crop support is being explored separately in an experimental v0.2 development branch and is **not part of this v0.1 alpha release**.

---

## Important safety warning

This tool modifies map folders and XML/i3d configuration files.

Always:

1. Work on a copy of the target map.
2. Use a fresh output folder.
3. Test in a disposable savegame first.
4. Keep the original source and target maps backed up.
5. Do not run this directly against your only working copy of a map.
6. Do not test on an important save until the generated map has loaded cleanly in a disposable save.

CropPorter is intended for advanced FS25 users and map modders.

---

## Requirements

- Windows
- Python 3.10 or newer recommended
- Farming Simulator 25 map mods
- Source and target maps as either:
  - `.zip` map mods, or
  - extracted map folders

No third-party Python libraries are currently required for the v0.1 workflow.

---

## Basic workflow

The safest workflow is:

```text
scan-source
probe-crop
preflight
apply
patch-density-config
test in disposable save
```

---

## Example: scan a source map

```powershell
py .\python\Crop_Porter\fs_25_crop_porter_v_0_1.py scan-source `
  '.\mods\FS25_SourceMap.zip' `
  --include-basegame
```

This lists detected crops and helps confirm the crop name to use.

---

## Example: probe a crop

```powershell
py .\python\Crop_Porter\fs_25_crop_porter_v_0_1.py probe-crop `
  '.\mods\FS25_SourceMap.zip' `
  blackbean
```

The probe reports detected dependencies such as:

- `fruitType` nodes
- `fillType` nodes
- `heightType` nodes
- referenced assets
- warnings

---

## Example: preflight before applying

```powershell
py .\python\Crop_Porter\fs_25_crop_porter_v_0_1.py preflight `
  --source '.\mods\FS25_SourceMap.zip' `
  --target '.\mods\FS25_TargetMap.zip' `
  --crops blackbean `
  --output '.\cropporter_reports\source_to_target_blackbean'
```

This writes:

```text
CropPorter_Preflight.json
CropPorter_Preflight.md
```

Review these before applying.

---

## Example: apply a crop to a target map

```powershell
py .\python\Crop_Porter\fs_25_crop_porter_v_0_1.py apply `
  --source '.\mods\FS25_SourceMap.zip' `
  --target '.\mods\FS25_TargetMap.zip' `
  --crops blackbean `
  --output '.\mods\FS25_TargetMap_CropPorted_Blackbean_v1'
```

The apply process writes:

```text
CropPorter_Apply.json
CropPorter_Apply.md
```

---

## Patch density channel config

After adding a new fruitType, run:

```powershell
py .\python\Crop_Porter\fs_25_crop_porter_v_0_1.py patch-density-config `
  '.\mods\FS25_TargetMap_CropPorted_Blackbean_v1'
```

This updates the target map i3d foliage density channel configuration where needed.

Important:

```text
This changes the i3d channel config only.
If the density map binary/image itself needs conversion or expansion, manual work may still be required.
```

---

## Verify fruit registry

```powershell
py .\python\Crop_Porter\fs_25_crop_porter_v_0_1.py probe-fruit-registry `
  '.\mods\FS25_TargetMap_CropPorted_Blackbean_v1'
```

Look for the imported crop as a direct `fruitType` registry entry.

Example:

```xml
<fruitType filename="maps/foliage/blackbean/blackbean.xml" />
```

---

## Verify density setup

```powershell
py .\python\Crop_Porter\fs_25_crop_porter_v_0_1.py probe-density `
  '.\mods\FS25_TargetMap_CropPorted_Blackbean_v1' `
  --include-fruits
```

This reports detected density layer configuration, estimated fruitType capacity, and related warnings.

---

## Common warnings

### No growth/calendar entry was matched

The crop may not appear in the seasonal calendar unless a matching growth/calendar entry exists or is added manually.

### No target heightTypes XML file detected

The target map may not use a separate `maps_densityMapHeightTypes.xml`, or it may define height types differently.

### Density map binary/channel capacity was not validated

CropPorter v0.1 does not fully validate or expand binary `.gdm` density maps. Always test in a disposable save.

### Skipped target duplicate node

This usually means the target already had that fillType, heightType, or category entry. This can be normal.

---

## Existing saves and map identity

FS25 saves are tied to the map mod identity. If you create a new output folder such as:

```text
FS25_TargetMap_CropPorted_Blackbean_v1
```

an existing save made on:

```text
FS25_TargetMap
```

may not recognise it as the same map.

Recommended development workflow:

```text
1. Generate versioned output folders.
2. Test in disposable saves.
3. Once happy, copy the final output into a stable DEV folder name.
```

Example:

```text
FS25_TargetMap_CropPorted_DEV
```

---

## Known limitations

CropPorter v0.1 is not guaranteed to work with every FS25 map.

Known risk areas:

- Inline map XML registries
- Non-standard `fruitTypes` layouts
- Maps that use `additionalFiles` differently
- Crops with custom scripts
- Crops with production chains
- Crops that use placeables rather than field foliage
- Vine/orchard/plantation systems
- Missing or incompatible i3d/shapes references
- Density map capacity issues
- Savegame compatibility

---

## Tested crops

### BLACKBEAN

Confirmed working as a field crop.

Test coverage includes:

- Crop registration
- PDA/map visibility
- In-field render
- Harvest-ready state
- FillType support
- Storage/category integration

### PINTOBEAN

Confirmed working as a field crop.

Test coverage includes:

- Crop registration
- PDA/map visibility
- In-field render
- Harvest-ready state
- FillType support
- Storage/category integration

---

## Not included in v0.1

Coffee / row-planted coffee / plantation support is **not included** in this release.

That work is being developed separately in v0.2 and includes different systems:

- `placeable type="vine"`
- row planting
- tree/placeable definitions
- motion path effects
- dedicated production chains
- optional vehicle migration

Do not expect v0.1 to port coffee, grapes, olives, tea, cocoa, orchard systems, or greenhouse-based crops.

---

## Suggested test procedure

After generating a ported map:

1. Move the original target map out of the active mods folder.
2. Leave only the generated ported map active.
3. Start a new disposable save.
4. Check the log for missing file errors.
5. Confirm the crop appears in the map/PDA where expected.
6. Use Easy Dev Controls or similar tools to set the crop state.
7. Confirm the crop renders in field.
8. Confirm seeders/planters support the crop.
9. Confirm harvest works.
10. Confirm trailers/silos/sell points accept the fillType.

---

## Reporting issues

When reporting issues, please include:

- CropPorter version
- Source map name
- Target map name
- Crop name
- Command used
- `CropPorter_Preflight.md`
- `CropPorter_Apply.md`
- Relevant FS25 `log.txt` errors/warnings
- Whether the source/target maps are zipped or extracted folders
- Whether the test was done in a new disposable save

---

## Licence and Permissions

Copyright © 2026 SimGamerJen. All rights reserved.

You may download and use this software for personal use. You may not modify, redistribute, re-upload, or publish this software, in whole or in part, or any derivative version without prior written permission from SimGamerJen.

---

## Disclaimer

FS25 CropPorter is an unofficial tool. It is not affiliated with GIANTS Software.

Use at your own risk. Always back up your maps and saves.
