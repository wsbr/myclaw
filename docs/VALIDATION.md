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

### Continuous-task context acceptance

`test/e2e/fullstack/test_context_long_task_real.py` exercises the shipped
`context.standard` + `history.summary_recent` preset using a private test Agent
and real Provider calls. Configure the normal full-stack API, database and
real-LLM environment variables before running it. The dedicated test ModelVersion
requires an artificial context window of `40096`, `max_input_tokens=36000`,
`max_output_tokens=4096`, and model parameters `max_tokens=4096`. These values
exercise the `28800` soft target; they do not describe the Provider's actual
capacity or change existing Agents. Set other capabilities and Provider-specific
parameters according to the model's conformance results.

```bash
test/.venv/bin/python -m pytest -c test/pytest.ini \
  test/e2e/fullstack/test_context_long_task_real.py \
  --override-ini addopts='' --junitxml=/path/to/private-evidence/context.xml
```

The nine-turn scenario must demonstrate actual soft-budget history removal,
two asynchronous rolling summary batches with verified parent and source hashes,
unchanged original Messages, and recall of an early task identifier that is no
longer present in selected raw history. It also requires the latest correction
to remain in the model input, intact tool continuations, and a real downloadable
artifact with exact expected content. A completed Run or a ready summary alone
does not pass. Recent-history protection can leave a request above the soft
target, but every request must remain within the hard input limit.

Retain failed attempts separately when calibrating or diagnosing this test.
This bounded synthetic scenario is a regression check, not evidence that all
long tasks preserve every fact or succeed indefinitely.

### Hard-limit context acceptance

`test/e2e/fullstack/test_context_hard_compaction_real.py` uses four real chat
commands and the same shipped continuous-task preset. Its dedicated ModelVersion
requires `context_window=34096`, `max_input_tokens=30000`, and
`max_output_tokens=4096`, with model parameters `max_tokens=4096`. Supply these
through `MYCLAW_REAL_LLM_CAPABILITIES` and `MYCLAW_REAL_LLM_PARAMETERS`, preserving
the other conformance-verified capabilities and Provider-specific parameters.

The corpus crosses the artificial hard limit while the recent-history floor
prevents prior soft trimming and before asynchronous History summarization
starts. Acceptance requires a real synchronous summary, measured Provider usage
and ledger entries, an over-limit pre-count and fitting post-count, unchanged
source messages, exact early identifier recall, latest corrections, durable cache
reuse without another paid summary, and a verified downloadable artifact.

Run this test with the same command as above, replacing the test filename and
using a separate private evidence directory. Summary failure and protected-input
overflow are deterministic negative tests; a successful real UAT does not claim
that an arbitrary oversized current request can be made to fit.

## Interpretation

The implementation status and non-production boundaries are maintained in the [README maturity table](../README.md#project-maturity). Production reference documents describe a target deployment contract, not a claim that the local Compose profile already satisfies it.
