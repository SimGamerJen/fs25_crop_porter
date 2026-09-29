# Limitations

FS25 CropPorter v0.3-alpha is substantially more capable than the original v0.1 release, but it is still an alpha compatibility tool.

## Portability status

The current Python engine has been validated against two materially different target architectures:

- Castlereagh;
- Hobo's Hollow.

This is strong regression coverage, but it is **not proof that every FS25 map layout is supported**.

New target failures should be reduced to a generic structural case rather than fixed by map name.

## Best-supported crop style

The current production path is strongest for field-style crops that use conventional combinations of:

- fruitType XML;
- fillType data;
- foliage I3D/shapes/textures;
- map I3D foliage layers;
- fill/fruit categories;
- density-map height types;
- l10n data.

Shared foliage families can be handled, but remain an important regression area.

## Plantation / vine support

Plantation/vine probing and apply commands exist but are experimental.

They are not currently covered by the same portability validation as the field-crop package workflow.

## Vehicle, economy and production integration

CropPorter does not comprehensively migrate:

- vehicle/tool compatibility;
- harvesting equipment;
- production chains;
- pallets;
- selling points;
- contracts;
- economy/pricing behaviour.

Some crop XML references and converter/category data are preserved where detected, but this should not be treated as complete ecosystem migration.

## Density maps

CropPorter can:

- promote fruit density type-index capacity;
- convert GDM/GRLE to PNG through the GIANTS converter when needed;
- synchronise densityMapHeightTypes and terrainDetailHeight;
- repack height-map logical bits when the type-index width changes;
- retire stale compiled height GDMs after PNG expansion.

However:

- unusual density-map encodings may still expose unsupported cases;
- runtime testing remains mandatory;
- the tool does not claim to replace GIANTS' own converter/engine validation.

## Established saves

The explicit migrate-save command handles the known terrain-detail-height channel-expansion case and creates a backup before replacement.

Current evidence:

- GIANTS converter round-trip validation: passed;
- synthetic non-zero type/height preservation: passed;
- real established-save migration/runtime: passed on Castlereagh;
- real pre-existing non-zero loose-material heap preservation: **not yet field-tested** because the tested real save's height map contained only zero logical values.

Do not infer a guarantee beyond that evidence.

## GRLE converter path

The reference script currently contains a development-machine default converter path.

Users on other systems should pass --grle-converter explicitly.

The GIANTS converter itself is not included in this repository.

## Crop package redistribution

A CropPorter package may contain source-map assets.

Creating a package does not confer redistribution rights. Do not publish packages or third-party assets without permission.

## Generated map identity

FS25 savegames are tied to map/mod identity.

During development:

- use a stable final map identity for long-running saves;
- avoid leaving multiple copies of the same map active;
- test generated map changes in disposable saves first.

## C# application

The future C#/.NET/WPF application is not in this repository yet.

This repository remains the Python reference implementation against which the new application can be checked.
