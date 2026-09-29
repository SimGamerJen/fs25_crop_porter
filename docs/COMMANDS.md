# FS25 CropPorter Commands

Current script:

~~~text
fs_25_crop_porter_v_0_3.py
~~~

Run help at any level with -h.

## selftest

Runs internal regression checks.

~~~powershell
py .\fs_25_crop_porter_v_0_3.py selftest
~~~

## probe-map

Inspects a map for likely crop-system XML roles and integration points.

~~~powershell
py .\fs_25_crop_porter_v_0_3.py probe-map '.\mods\FS25_SourceMap.zip' --verbose
~~~

Optional: --keywords, --max-hits.

## scan-source

Scans a source map and lists detected crops.

~~~powershell
py .\fs_25_crop_porter_v_0_3.py scan-source '.\mods\FS25_SourceMap.zip' --include-basegame
~~~

Optional: --output.

## probe-crop

Inspects one crop and reports detected dependencies.

~~~powershell
py .\fs_25_crop_porter_v_0_3.py probe-crop '.\mods\FS25_SourceMap.zip' rye
~~~

## preflight

Compares selected source crops against a target map.

~~~powershell
py .\fs_25_crop_porter_v_0_3.py preflight --source '.\mods\FS25_SourceMap.zip' --target '.\mods\FS25_TargetMap.zip' --crops rye triticale --output '.\cropporter_reports\source_to_target'
~~~

## apply

Ports selected crops directly from a source map into a copied target tree.

~~~powershell
py .\fs_25_crop_porter_v_0_3.py apply --source '.\mods\FS25_SourceMap.zip' --target '.\mods\FS25_TargetMap.zip' --crops rye triticale --output '.\mods - TESTING\FS25_TargetMap_Custom'
~~~

Optional: --force, --grle-converter.

## export-crop

Exports a reusable CropPorter package.

~~~powershell
py .\fs_25_crop_porter_v_0_3.py export-crop --source '.\mods\FS25_SourceMap.zip' --crop rye --library '.\CropPorterLibrary' --source-name 'Source Map' --redistribution private
~~~

Optional:

- --package-name
- --plantation
- --include-vehicles
- --redistribution unknown|private|permitted|open

## probe-package

Shows a package manifest.

~~~powershell
py .\fs_25_crop_porter_v_0_3.py probe-package '.\CropPorterLibrary\rye_source-map.cropporter.zip' --assets
~~~

## list-library

Lists CropPorter packages in a library folder.

~~~powershell
py .\fs_25_crop_porter_v_0_3.py list-library '.\CropPorterLibrary'
~~~

## apply-package

Applies one CropPorter package without needing the original source map.

~~~powershell
py .\fs_25_crop_porter_v_0_3.py apply-package --package '.\CropPorterLibrary\rye_source-map.cropporter.zip' --target '.\mods - TESTING\FS25_TargetMap.zip' --output '.\mods - TESTING\FS25_TargetMap_Rye'
~~~

Optional: --grle-converter.

## apply-packages

Applies multiple field-crop packages into a single working tree. Repeat --package in the required apply order.

~~~powershell
py .\fs_25_crop_porter_v_0_3.py apply-packages --package '.\CropPorterLibrary\barley.cropporter.zip' --package '.\CropPorterLibrary\wheat.cropporter.zip' --package '.\CropPorterLibrary\triticale.cropporter.zip' --package '.\CropPorterLibrary\rye.cropporter.zip' --target '.\mods - TESTING\FS25_TargetMap.zip' --output '.\mods - TESTING\FS25_TargetMap_Custom' --zip-output '.\mods - TESTING\FS25_TargetMap_Custom.zip'
~~~

The target is copied once. CropPorter accumulates package changes in one working tree and writes multi-apply reports.

Optional: --grle-converter.

## migrate-save

Explicitly migrates an established savegame densityMap_height.gdm to match a converted map's terrainDetailHeight layout.

Always start with dry-run:

~~~powershell
py .\fs_25_crop_porter_v_0_3.py migrate-save --savegame 'C:\Path\To\savegame' --map '.\mods\FS25_TargetMap_Custom.zip' --dry-run
~~~

Then, only after reviewing the inferred layout:

~~~powershell
py .\fs_25_crop_porter_v_0_3.py migrate-save --savegame 'C:\Path\To\savegame' --map '.\mods\FS25_TargetMap_Custom.zip'
~~~

Optional: --grle-converter.

Normal apply commands do not modify saves.

## Diagnostic / repair commands

The script also retains lower-level commands used during development:

- probe-fruit-registry
- probe-density
- patch-density-config
- patch-i3d-foliage-layer
- fix-fruit-registry
- fix-crop-filltype-case
- fix-filltype-registry-name
- patch-l10n

Use -h on the command before running one; several repair commands intentionally refuse ZIP targets and require an extracted map folder.

## Experimental plantation commands

- probe-plantation-crop
- apply-plantation

These remain experimental and are not part of the current field-crop portability baseline.

## GRLE converter

Commands that expand or validate GDM/GRLE data may require the GIANTS GRLE converter.

If it is not installed at the development default path, specify it explicitly:

~~~powershell
--grle-converter 'C:\Path\To\grleConverter\convert.cmd'
~~~
