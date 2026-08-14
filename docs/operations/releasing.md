# Release process

This process applies to public source releases. It does not replace Provider-specific acceptance or enterprise production-readiness review.

## 1. Prepare the release

- update `VERSION`, the frontend and API versions, and `CHANGELOG.md` together;
- confirm the README quick start from a fresh clone;
- document current limitations without presenting reference architecture as implemented production capability;
- keep raw acceptance evidence and deployment data outside Git.

## 2. Run deterministic gates

```bash
make test-env
make release-check
```

The release check includes the repository safety scan, backend tests, frontend build, and Compose validation. Database migrations must have one Alembic head.

## 3. Validate model-dependent behavior

Run the relevant Provider conformance and real-model acceptance suites when Agent behavior changes. Publish only the sanitized summary described in [Validation and release evidence](../VALIDATION.md).

## 4. Seal and publish

- review a clean working tree;
- create a release commit;
- create an annotated `vX.Y.Z` tag at that exact commit;
- push the branch and tag without force;
- wait for GitHub CI to pass;
- smoke-test the published archive before announcing the release.

Published tags are immutable. Corrections after publication use a new patch release rather than moving an existing tag.
