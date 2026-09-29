---
name: Bug report
about: Report a problem with FS25 CropPorter
title: "[Bug]: "
labels: bug
assignees: ""
---

## CropPorter version / build marker

Example:

~~~text
0.3-alpha
supplemental-height-registry-pipeline-v17
~~~

## Operation

- [ ] scan/probe/preflight
- [ ] export-crop
- [ ] apply-package
- [ ] apply-packages
- [ ] migrate-save
- [ ] plantation/experimental
- [ ] other

## Source map / package(s)

Name/version/link if available. For multi-package apply, include the package order.

~~~text

~~~

## Target map

Name/version/link if available:

~~~text

~~~

## Exact command

~~~powershell

~~~

## What happened?

~~~text

~~~

## What did you expect?

~~~text

~~~

## Runtime stage

- [ ] Build/static validation
- [ ] First FS25 load
- [ ] First save
- [ ] Restart/reload
- [ ] Established-save migration

## Test hygiene

- [ ] Untouched target used
- [ ] Fresh output path used
- [ ] Only one copy/version of the target map active
- [ ] Disposable fresh save tested first
- [ ] FS25 fully restarted for reload test

## Attach CropPorter reports

Attach whichever apply:

- CropPorter_Preflight.*
- CropPorter_Apply*.json/.md
- CropPorter_MultiApply.*
- CropPorter_SaveMigration_*.json

## FS25 log

Attach the complete relevant log.txt where possible, or paste the relevant section.

## Additional notes

~~~text

~~~
