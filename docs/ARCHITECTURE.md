# Architecture

## Purpose

FS25 CropPorter is intended to become a **map-structure-driven crop portability engine**, not a collection of per-map patches.

The current Python implementation is a monolithic reference script, but its logical architecture is already separable into the following concerns:

~~~text
Source / Package
      |
      v
Inspection
      |
      v
Compatibility analysis
      |
      v
Patch plan
      |
      +----> Registry/XML mutation
      |
      +----> I3D / foliage mutation
      |
      +----> Asset dependency copy
      |
      +----> Density-map transformation
      |
      v
Validation / reports
      |
      v
Generated map
~~~

Established-save migration is deliberately separate and explicit.

## Core responsibilities

### Source inspection

Discover:

- fruit definitions;
- fill types;
- height types;
- growth/season data;
- crop foliage assets;
- categories/converters/l10n;
- local asset dependencies;
- shared/sibling crop-family assets.

### Target inspection

Determine the target's actual structure instead of assuming filenames:

- separate fruit registry vs inline <fruitTypes>;
- explicit local fill registry vs implicit/base-game behaviour;
- explicit local height registry vs none;
- target map I3D location;
- densityMap_fruits representation and channel layout;
- terrainDetailHeight representation and type/height bit layout;
- existing crop/fill/height names and conflicts.

### Compatibility analysis

Decide what must change before writing:

- registry activation/insertion;
- asset copying;
- shared family reconciliation;
- fruit-density capacity;
- terrain-detail-height type-index width;
- height-bit repack;
- runtime dependency additions;
- safe ID allocation.

### Apply

Apply into a new working tree.

The multi-package path copies the pristine target once and accumulates each package into that tree.

### Validation

Validate:

- XML/I3D structure;
- append-only target file-table invariants;
- expected channel relationships;
- generated reports;
- narrow internal density/GDM round trips.

Final acceptance still requires FS25 runtime testing.

## Safety invariants

### No map-name special cases

Compatibility logic must describe a reusable structural condition.

Do not add:

~~~python
if map_name == "FS25_HobosHollow":
    ...
~~~

or equivalent target-specific conditions.

### Existing I3D File entries are append-only

When a target I3D already has:

~~~xml
<File fileId="755" filename="data/densityMap_fruits.gdm" />
~~~

CropPorter must not rewrite that existing entry to a PNG.

Instead it:

1. preserves the old entry;
2. appends a new file entry;
3. points the active density layer to the new file ID.

This avoids unsafe mutation of pre-existing file-table semantics.

### Retire stale compiled height maps

When densityMap_height.gdm is converted and the active terrain-detail-height layer is repointed to a PNG, the stale same-stem compiled GDM must not remain active on disk. FS25 may otherwise prefer/reject the stale compiled data.

The original is backed up before retirement.

### Preserve physical height bits

Expanding the height-type index must not consume the physical heap-height bits.

For the currently validated 12 -> 14 channel case:

~~~text
before: type [0..5] + height [6..11]
after:  type [0..7] + height [8..13]
~~~

The physical height width remains six bits; the logical height values are repacked to their new position.

### Savegames are opt-in

Normal apply/package operations never modify savegames.

An established save is modified only through migrate-save, which:

- analyses the source GDM;
- supports dry-run;
- creates a timestamped backup;
- stages the transformation;
- validates the generated result;
- replaces the save file only after validation.

## Validated target architectures

### Architecture A — Castlereagh

Characteristics encountered:

- existing local registries;
- existing density-map-height type registry;
- terrain-detail-height channel expansion required;
- shared crop-family foliage layouts.

This target drove the height-layout and existing-save migration work.

### Architecture B — Hobo's Hollow

Characteristics encountered:

- active inline <fruitTypes> in map.xml;
- I3D directly referenced compiled GDM files;
- no local density-map-height type registry;
- supplemental registry creation required;
- append-only density reference replacement required.

This target drove inline registry and supplemental height-registry support.

The same generic engine must continue to pass both architecture classes.

## Crop packages

A CropPorter package separates source extraction from target application.

Conceptually a package contains:

~~~text
manifest
crop XML/config fragments
foliage assets
resolved runtime dependencies
source metadata
redistribution metadata
~~~

The package does not imply permission to redistribute its source assets.

## Python reference vs desktop application

This repository remains the Python behavioural reference.

The planned main application will be built separately with:

~~~text
C#
.NET
WPF
XAML
~~~

The C# application should reproduce the validated engine behaviour rather than embedding or extending map-specific assumptions.

A sensible application decomposition is:

~~~text
CropPorter.Core
  inspection
  package model
  compatibility analysis
  patch planning
  XML/I3D mutation
  density migration
  validation

CropPorter.App
  WPF UI
  workflow orchestration
  progress/logging
  settings
  user confirmation

CropPorter.Tests
  unit tests
  synthetic map-layout fixtures
  regression fixtures for architecture A/B
~~~

The Python v0.3-alpha v17 script should be retained unchanged as a known-good comparison point while the C# core is implemented.
