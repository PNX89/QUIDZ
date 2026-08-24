<!-- Generated for QUIDZ. The checks below are the ones this repository's CI runs. -->

## What this changes

<!-- One paragraph. What was true before, what is true now, and why the change was worth making. -->

## Checklist

- [ ] Every gate passes locally, not just the tests:
      `uv run ruff check .`
      `uv run ruff format --check .`
      `uv run mypy`
      `uv run pytest -q`
- [ ] No number in the README was edited by hand. Anything generated was regenerated.
- [ ] The prose still describes the code. No claim in the README has quietly stopped being true.
- [ ] Public behaviour that changed is in `CHANGELOG.md` under the unreleased heading.
- [ ] No em dash and no en dash anywhere in the diff, prose or code.
- [ ] Nothing added to the dependency tree without a stated reason and a look at its advisories.

## What I checked that a reviewer cannot see from the diff

<!-- Ran the demo? Reproduced the bug first? Watched a new test fail against the old code
     before believing it? Say so here. This section is the point of the template. -->
