# Open-source release checklist

This checklist governs public source releases. It does not replace the real-model UAT or enterprise production readiness gates.

## Content gate

- [ ] `VERSION`, frontend version, API version, and changelog agree.
- [ ] README quick start works from a fresh clone.
- [ ] Product maturity and non-production limitations are explicit.
- [ ] New behavior is reflected in authoritative module documentation.
- [ ] License, notice, contribution guide, security policy, and code of conduct are present.

## Security gate

- [ ] `./scripts/check_repository_secrets.sh --history` passes.
- [ ] No deployment `.env`, database backup, UAT evidence, Workspace, Artifact, or private key is tracked.
- [ ] Any credential that ever entered Git has been revoked and rotated.
- [ ] GitHub private vulnerability reporting is enabled.
- [ ] Repository visibility is changed only after reviewing the final remote tree.

## Deterministic gate

- [ ] `make release-check` passes.
- [ ] Backend unit tests pass.
- [ ] Frontend production build passes.
- [ ] Main, UAT, and test Compose files parse.
- [ ] Database migrations have a single Alembic head.

## Behavioral evidence

- [ ] Relevant real Provider conformance passes.
- [ ] Chat, Plan, and emergent Group acceptance evidence is bound to the release commit SHA.
- [ ] Group evidence proves directed request/delivery semantics and no duplicate delivery.
- [ ] Known failures and nondeterministic model limitations are documented.

## Git seal

- [ ] Working tree is clean.
- [ ] Release commit is reviewed and signed when signing is available.
- [ ] Annotated tag `vX.Y.Z` points at that exact commit.
- [ ] Tag and branch are pushed without force.
- [ ] GitHub Release notes use the matching changelog section.
- [ ] Published archive is downloaded once and its quick start is smoke-tested.
