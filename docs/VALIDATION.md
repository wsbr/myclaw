# Validation and release evidence

MyClaw separates deterministic software checks from model-dependent behavior. A passing build proves the repository is reproducible; it does not prove that every Provider or prompt will produce the same Agent behavior.

## Continuous integration

Every push and pull request runs three public jobs:

- repository safety: tracked-file and Git-history credential checks, patch hygiene, and Compose validation;
- backend: dependency installation, source compilation, and deterministic unit tests;
- frontend: frozen dependency installation and production build.

The workflow is defined in [`.github/workflows/ci.yml`](../.github/workflows/ci.yml). Its result is the authoritative public signal for the tested commit.

## Local release gate

Create the test environment once, then run the same release-oriented checks locally:

```bash
make test-env
make release-check
```

The gate checks repository hygiene, Python compilation, backend tests, the frontend production build, and all supported Compose configurations.

## Real-model acceptance

Chat, Plan, Tool use, and emergent Group behavior depend on the configured model. Changes to those paths require an opt-in acceptance run against the intended Provider in a controlled environment.

Raw acceptance material is not source code and is not committed to this repository. Keep screenshots, session transcripts, traces, test reports, database exports, workspaces, artifacts, model identifiers, and environment metadata in access-controlled CI artifacts or local evidence directories.

A public release may include a concise sanitized summary containing only:

- the release tag or commit;
- the scenarios exercised;
- aggregate pass/fail results;
- material limitations or nondeterministic observations.

Do not publish credentials, account data, local paths or hostnames, internal resource identifiers, raw prompts, private model output, or backup filenames.

## Interpretation

The implementation status and non-production boundaries are maintained in the [README maturity table](../README.md#project-maturity). Production reference documents describe a target deployment contract, not a claim that the local Compose profile already satisfies it.
