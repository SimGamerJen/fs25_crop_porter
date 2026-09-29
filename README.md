# FS25 CropPorter

**FS25 CropPorter** is a Python reference/prototype for porting Farming Simulator 25 custom crops between map mods.

The project is deliberately conservative: it inspects the source and target map structures, builds a compatibility plan, copies the crop-related assets it can resolve, patches the target, and validates the result as far as possible before FS25 is launched.

> **Alpha software.** CropPorter is not yet proven against every FS25 map architecture. Always work from backups and test generated maps in disposable saves first.

## Project direction

This repository remains the **Python reference implementation**.

Current development baseline:

~~~text
Version:      0.3-alpha
Build marker: supplemental-height-registry-pipeline-v17
Source SHA256: fe3231006edc7fc8c52241206f588a5b88db4d80ef25758f1830f5038e4a4af3
Script:       fs_25_crop_porter_v_0_3.py
~~~

A separate Windows desktop application is planned in **C# / .NET / WPF**. The Python implementation is being retained as the known-good behavioural reference while that application is built.

The historical v0.1 script remains in this repository for reference.

## Current capabilities

The v0.3 Python baseline can:

- scan and probe source maps;
- preflight source-to-target crop ports;
- export reusable .cropporter.zip crop packages;
- inspect packages and package libraries;
- apply one package or a batch of packages to a pristine target map;
- produce a complete release ZIP;
- copy crop foliage XML, I3D, shapes, textures and resolved local runtime dependencies;
- handle shared/sibling foliage families such as normal/winter barley and wheat;
- patch fruit types, fill types, height types, categories, converters and l10n;
- support both separate fruit registries and inline <fruitTypes> blocks in the main map XML;
- create supplemental local fill-type/height-type registries where the target requires them;
- preserve existing I3D <File> entries and append replacement density-map references safely;
- expand densityMap_fruits type-index capacity when required;
- convert GDM/GRLE density data through the GIANTS GRLE converter where required;
- synchronise densityMapHeightTypes with terrainDetailHeight;
- repack terrain-detail-height type/height bits when the type-index width expands;
- explicitly migrate an established save's densityMap_height.gdm when the converted map layout requires it;
- create backups and JSON/Markdown diagnostic reports;
- run internal regression/self-tests.

Experimental plantation/vine inspection and apply commands are still present, but the current validated v0.3 workflow is focused on standard field-style crops.

## Portability rule

CropPorter must solve **map structures**, not map names.

The engine should not contain target-specific branches such as:

~~~python
if map_name == "FS25_HobosHollow":
    ...
~~~

or assumptions tied to a particular I3D file ID.

Instead it detects characteristics such as:

- inline vs separate registries;
- PNG vs GDM/GRLE density references;
- current fruit-density channel capacity;
- presence or absence of a local height-type registry;
- terrain-detail-height bit layout;
- shared foliage family layouts;
- local runtime asset dependencies.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Current validation baseline

### Source/package baseline

The current 12-package validation set was exported from BallySpring:

1. barley (including winter barley family assets)
2. wheat (including winter wheat family assets)
3. triticale
4. rye
5. clover
6. fieldgrass
7. flowering catch crop
8. green rye
9. humus active
10. mustard
11. silage maize
12. vetch rye

### Target architecture A — Castlereagh

Validated behaviours include:

- existing local crop/height registries;
- shared barley/wheat foliage-family reconciliation;
- crop asset dependency resolution;
- fruit-density expansion;
- terrain-detail-height expansion;
- fresh-save load/save/reload;
- explicit established-save height-map migration.

The established Castlereagh save used for the final migration test had an all-zero densityMap_height.gdm, so real-world preservation of a pre-existing non-zero loose-material heap remains **not field-tested**. Synthetic non-zero migration tests pass.

### Target architecture B — Hobo's Hollow

Validated behaviours include:

- inline <fruitTypes> registry in map.xml;
- direct GDM references in the I3D;
- no original local densityMapHeightTypes registry;
- creation/activation of a supplemental height registry;
- append-only I3D file-table handling;
- terrainDetailHeight expansion from 12 to 14 channels;
- fresh-save load/save/reload.

Both targets were validated with the same generic crop-porting engine.

## Requirements

- Windows is the primary development/test platform.
- Python 3.10 or newer is recommended.
- Farming Simulator 25.
- Source/target map mods as ZIP files or extracted folders.
- GIANTS GRLE Converter when a workflow needs to decode/convert GDM/GRLE data.

The script currently contains a development-machine default path for the GRLE converter. On another system, pass the correct location explicitly:

~~~powershell
--grle-converter 'C:\Path\To\grleConverter\convert.cmd'
~~~

No third-party Python packages are required by the current baseline.

## Quick start

Run the internal checks first:

~~~powershell
py .\fs_25_crop_porter_v_0_3.py selftest
~~~

Scan a source map:

~~~powershell
py .\fs_25_crop_porter_v_0_3.py scan-source '.\mods\FS25_SourceMap.zip' --include-basegame
~~~

Probe a crop:

~~~powershell
py .\fs_25_crop_porter_v_0_3.py probe-crop '.\mods\FS25_SourceMap.zip' rye
~~~

Export a reusable crop package:

~~~powershell
py .\fs_25_crop_porter_v_0_3.py export-crop --source '.\mods\FS25_SourceMap.zip' --crop rye --library '.\CropPorterLibrary' --source-name 'Source Map'
~~~

Apply a package:

~~~powershell
py .\fs_25_crop_porter_v_0_3.py apply-package --package '.\CropPorterLibrary\rye_source-map.cropporter.zip' --target '.\mods\FS25_TargetMap.zip' --output '.\mods - TESTING\FS25_TargetMap_Rye'
~~~

For a multi-package build, repeat --package once for each package:

~~~powershell
py .\fs_25_crop_porter_v_0_3.py apply-packages --package '.\CropPorterLibrary\barley.cropporter.zip' --package '.\CropPorterLibrary\wheat.cropporter.zip' --package '.\CropPorterLibrary\rye.cropporter.zip' --target '.\mods - TESTING\FS25_TargetMap.zip' --output '.\mods - TESTING\FS25_TargetMap_Custom' --zip-output '.\mods - TESTING\FS25_TargetMap_Custom.zip'
~~~

See [docs/COMMANDS.md](docs/COMMANDS.md) for the complete command summary.

## Existing-save migration

Normal apply operations do **not** modify savegames.

If an established save was created against an older terrainDetailHeight layout and the converted map increases the type-index width, first inspect the migration:

~~~powershell
py .\fs_25_crop_porter_v_0_3.py migrate-save --savegame 'C:\Path\To\savegame' --map '.\FS25_TargetMap_Custom.zip' --dry-run
~~~

Only after reviewing the dry-run should the migration be executed without --dry-run.

The migration creates a timestamped backup and validates the generated GDM with the GIANTS converter before replacement.

## Safety model

Always:

1. keep an untouched source map;
2. keep an untouched pristine target map;
3. generate into a new output folder;
4. review CropPorter reports;
5. ensure only one copy/version of a target map is active in the FS25 mods folder;
6. test a brand-new disposable save first;
7. save, quit FS25 completely, restart and reload;
8. back up established saves before any explicit save migration.

CropPorter intentionally refuses transformations that violate known safety invariants.

## Crop packages and redistribution

A .cropporter.zip can contain assets originating from a source map. Package creation does **not** grant redistribution rights.

The package manifest records a redistribution status, but the user remains responsible for ensuring that source-map assets may legally be redistributed.

Do not publish third-party map assets or CropPorter packages without appropriate permission.

## Documentation

- [Commands](docs/COMMANDS.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Testing](docs/TESTING.md)
- [Limitations](docs/LIMITATIONS.md)
- [Changelog](CHANGELOG.md)
- [v0.3-alpha v17 release notes](RELEASE_NOTES_v0.3-alpha-v17.md)

## Reporting issues

Useful reports include:

- CropPorter version/build marker;
- exact command;
- source map and target map;
- crop/package list;
- generated CropPorter_Apply*.json/.md or CropPorter_MultiApply.*;
- save-migration report if applicable;
- relevant FS25 log.txt;
- whether the failure occurred on first load or after save/reload.

## Licence and permissions

Copyright © 2026 SimGamerJen. All rights reserved.

You may download and use this software for personal use. You may not modify, redistribute, re-upload, or publish this software, in whole or in part, or any derivative version without prior written permission from SimGamerJen.

## Disclaimer

FS25 CropPorter is an unofficial tool. It is not affiliated with GIANTS Software.

Use at your own risk. Always back up maps and saves.
