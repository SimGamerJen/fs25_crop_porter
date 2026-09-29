# Contributing to FS25 CropPorter

FS25 CropPorter is currently an alpha Python reference implementation.

The most useful contributions are:

- testing additional source and target map architectures;
- reporting exact FS25 runtime errors;
- sharing CropPorter apply/preflight reports;
- identifying unusual registry/density-map layouts;
- adding regression tests for a structural case;
- improving documentation.

## Design rule: structural fixes only

A compatibility fix must describe a reusable map-layout behaviour.

Good examples:

- target uses an inline <fruitTypes> registry;
- target references a GDM directly from the I3D;
- target has no local density-map-height-type registry;
- target uses a different terrain-detail-height channel layout.

Avoid fixes such as:

~~~python
if map_name == "SomeMap":
    ...
~~~

or code tied to a particular file ID.

Castlereagh and Hobo's Hollow are regression architectures, not special-case targets.

## Before reporting a bug

Please confirm:

1. You used the current v0.3-alpha baseline.
2. You ran selftest.
3. You started from an untouched target map.
4. You generated into a fresh output path.
5. Only one copy/version of the target map was active in the FS25 mods folder.
6. You tested a brand-new disposable save first.
7. You saved, quit FS25 completely, restarted and reloaded.
8. You checked the generated CropPorter report(s).
9. You checked the full FS25 log.txt.

For an existing-save issue, also state whether migrate-save --dry-run was used and include the migration report if a migration was performed.

## Pull requests

Pull requests are welcome for review.

Please:

- keep the Python reference behaviour conservative;
- add or update a self-test for compatibility logic;
- avoid committing source/target map ZIPs or extracted third-party map assets;
- avoid committing CropPorter package archives containing third-party map assets;
- do not commit the GIANTS GRLE converter;
- document any new target-layout capability in docs/ARCHITECTURE.md;
- update CHANGELOG.md when behaviour changes.

The separate C#/.NET/WPF desktop application will be developed independently; this repository remains the Python reference/prototype.
