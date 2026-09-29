# Testing Guide

This guide describes the current regression procedure for the v0.3-alpha Python baseline.

## 1. Python self-test

Before map testing:

~~~powershell
py .\fs_25_crop_porter_v_0_3.py selftest
~~~

The current v17 baseline covers:

- RYE token matching without GREENRYE/VETCHRYE collisions;
- foliage entry matching;
- asset-graph scope;
- append-only densityMap_fruits GDM-to-PNG replacement;
- inline fruit registry detection;
- supplemental density-map-height registry creation;
- in-place multi-package height-registry activation;
- append-only densityMap_height replacement;
- RGB8 terrain-height logical-bit migration;
- stale height-GDM retirement;
- 14-channel GDM encode/decode round trip.

## 2. Build from a pristine target

- [ ] Source/package files are unchanged.
- [ ] Target is an untouched map ZIP/folder.
- [ ] Output path does not contain a previous build.
- [ ] Only one active copy/version of the map will be left in the FS25 mods folder.
- [ ] CropPorter reports are retained for review.

For multi-package tests, use apply-packages so the target is copied only once and package changes accumulate in one working tree.

## 3. Static output checks

Review:

- [ ] active fruit registry points to imported foliage XML;
- [ ] imported fill types are present;
- [ ] imported height types are present where required;
- [ ] I3D <File> entries that existed in the target were not rewritten;
- [ ] replacement density PNGs use appended file IDs;
- [ ] densityMap_fruits channel/index values match the required capacity;
- [ ] densityMapHeightTypes type-index width matches terrainDetailHeight;
- [ ] physical height bit count remains correct;
- [ ] stale compiled densityMap_height.gdm has been retired when PNG expansion occurred;
- [ ] expected foliage assets and runtime dependencies exist.

## 4. Fresh-save runtime test

With only the generated target map active:

- [ ] start a brand-new disposable save;
- [ ] confirm the correct generated map path appears in log.txt;
- [ ] confirm imported fruit types register;
- [ ] confirm imported foliage I3Ds load;
- [ ] confirm the map reaches gameplay;
- [ ] inspect the crop calendar/fruit list;
- [ ] inspect representative crops visually where practical;
- [ ] save successfully;
- [ ] quit FS25 completely.

## 5. Save / restart / reload

Restart FS25 and reload the new save:

- [ ] generated map loads again;
- [ ] imported fruit types register again;
- [ ] savegame density data loads;
- [ ] map reaches gameplay;
- [ ] save again successfully.

This restart/reload phase is part of the acceptance test, not an optional extra.

## 6. Established-save migration

Only when the converted map changes terrainDetailHeight layout and an established save must be retained:

1. restore/back up the untouched save;
2. run migrate-save --dry-run;
3. review source channels, inferred old bit layout and target layout;
4. run the real migration;
5. retain the timestamped CropPorter backup;
6. load the established save;
7. save, quit, restart and reload;
8. review the full log.

## 7. Log signatures

Search the complete FS25 log for:

~~~text
GDM file has wrong number of channels
DensityMap failed to load GDM file
Failed to load terrain detail layer
Can't load resource
Missing png file
Failed to load distance texture
maximum number (63)
Loaded fruit type
Entered Gameplay
Game saved successfully
~~~

An informational fruit-density conversion such as:

~~~text
GDM file has wrong number of type index channels (5 should be 6). Converting format.
~~~

can be expected on the first load after fruit type-index expansion if the engine converts the old save/map fruit density successfully. It should not be confused with a rejected total-channel count.

## Current regression targets

### Castlereagh

Covers:

- existing local registries;
- shared crop-family layout;
- terrain-height channel expansion;
- fresh save/reload;
- established-save migration.

### Hobo's Hollow

Covers:

- inline fruit registry;
- direct GDM references;
- missing local height registry;
- supplemental height registry;
- append-only density references;
- 12 -> 14 channel height layout;
- fresh save/reload.

## Reporting a failure

Include:

- exact CropPorter version/build marker;
- exact command;
- source map and target map;
- crop/package order;
- generated CropPorter reports;
- complete relevant FS25 log;
- whether it failed during fresh load, save, restart/reload or established-save migration.
