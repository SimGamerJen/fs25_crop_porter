# Testing Guide

Use this checklist when testing a generated CropPorter map.

## Before launching FS25

- [ ] Original target map moved out of active mods folder
- [ ] Only one version of the generated map is active
- [ ] Output folder has `CropPorter_Apply.md`
- [ ] Output folder has copied crop assets
- [ ] `patch-density-config` has been run if needed

## In FS25

- [ ] Start a new disposable save
- [ ] Check the log for missing file errors
- [ ] Confirm the map loads
- [ ] Confirm the crop appears in expected lists
- [ ] Confirm crop appears on PDA/map
- [ ] Confirm crop can be seeded/planted
- [ ] Confirm crop can be set/grown to harvest-ready
- [ ] Confirm crop renders in field
- [ ] Confirm crop can be harvested
- [ ] Confirm trailers accept the fillType
- [ ] Confirm silo/storage accepts the fillType
- [ ] Confirm selling station accepts the fillType

## Log checks

Search `log.txt` for:

```text
Error:
Warning:
No terrain data layer
FoliageTransformGroup
couldn't be loaded
Loaded fruit type
```

## Report results

If reporting a problem, include:

- source map
- target map
- crop
- exact commands
- CropPorter reports
- FS25 log excerpts
