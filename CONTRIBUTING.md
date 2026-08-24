# Contributing to QUIDZ

Thank you for looking. This is a small, deliberately scoped repository, and the fastest way to
be useful is usually to disagree with something it claims.

## Run it first

```bash
git clone https://github.com/PNX89/QUIDZ.git && cd QUIDZ
uv sync --all-extras --dev
uv run quidz demo --scenario adversarial --db /tmp/quidz-demo/quidz.db
```

Under a minute from clone to output, offline, with nothing to configure and no key to supply.
If that is not true on your machine, that is a bug and worth an issue on its own.

## The checks that gate every push

These are read out of `.github/workflows/ci.yml` when this file is generated, so the list
cannot drift away from what CI actually runs. All of them must pass locally before a pull
request will go green:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest -q
```

Run every one of them. Running only the test suite is the most common way to be surprised by a
red badge: formatting and typing are gates here, not suggestions.

## Everything merges through a pull request

`main` is protected and takes no direct pushes. The honest reason, since this is one author's
portfolio: there is no second pair of eyes, so the value is not the approval. It is that every
change arrives as a diff with a checklist against it, and that the pipeline has to be green
before the merge rather than after it.

**Merge with rebase, never with squash.** GitHub's squash button rewrites the author to the
account's primary address and appends a co-author trailer to the message. The pre-push gate here
rejects both: one author identity across every commit, and no attribution trailers of any kind.
The first pull request in this toolset was squashed and failed the gate on exactly those two
counts, which is how the rule came to be written down rather than rediscovered later.

The trailer is described here rather than quoted, because the gate scans tracked files for it as
well as commit messages, and a document explaining a banned string should not be the thing that
introduces it.

## What a review looks for

The pull request template carries the checklist. Two items on it are unusual and are the ones
that matter most here:

- **Does any number in the README still hold?** Several are asserted by tests against a real
  run. If you changed behaviour, regenerate rather than edit by hand.
- **Does the prose still describe the code?** A claim that has quietly stopped being true is
  worse than no claim, and it is the specific failure this whole toolset is built around.

## Issues

Bug reports are welcome. So is the other template: **a claim in the README does not hold**. If
something here says it does a thing and it does not, that is the most valuable issue you can
open, and it will be treated as a defect rather than a disagreement.

## Licence

MIT. By contributing you agree your contribution is licensed under it.
