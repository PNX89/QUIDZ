# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- `quidz.model`: an `adjust_auth` whose sequence is older than the newest event already folded
  into the aggregate is now a no-op rather than the value that wins. `PaymentState.last_sequence`
  was maintained and never read, so the last adjustment to be processed set the hold, and
  `authorized_minor` is the ceiling the over-capture guard compares against. One stream of three
  events reached two different terminal aggregates depending on arrival order.
- `quidz.reconcile`: `gate` evaluated its materiality threshold by value against a sum of
  `delta_minor` across currencies, which is the addition `quidz.money` exists to refuse. Exposure
  is now totalled per currency and the rationale states a figure for each, so unrelated drift in
  unrelated currencies no longer pools into a total that blocks payments none of which crossed
  the threshold alone. The count threshold is a count of findings and is unchanged.

### Added

- Tests for the guards, branches and constants the suite could not previously fail on: the
  Stripe tolerance value itself rather than only the comparison that uses it, the retry budget
  read back and written through `drain`, both routes to `quidz reconcile` exit 1 separately, the
  `--assert-terminal` mismatch, `quidz demo --http` against the in-process transport, the
  aggregate's currency and negative-amount guards, the over-capture boundary, and the gate's
  currency and batch scopes.

## [0.1.0] - 2026-08-18

### Added

- `quidz.money`: integer minor units with a currency, ISO 4217 exponents for zero-decimal and
  three-decimal currencies, and arithmetic that raises across currencies.
- `quidz.clock`: the single wall-clock boundary, with a `FakeClock` that records what was slept
  so no test needs a real sleep.
- `quidz.verify`: Stripe signature verification over the raw request bytes, with multiple active
  `v1` secrets for rotation, every non-`v1` scheme discarded, and a 300 second default tolerance
  checked at receipt time. Adyen HMAC verification over the colon-joined signing string, with
  `\` and `:` escaped inside field values and the hexadecimal key decoded before use.
- `quidz.events`: Stripe and Adyen payloads to one `CanonicalEvent`, with delivery identity taken
  from stable identity fields and the Adyen `(eventCode, success)` pair mapped as a pair.
- `quidz.model`: the payment aggregate, amount conservation invariants, a monotonic state rank,
  and a status derived from the amounts rather than stored.
- `quidz.store`: SQLite schema with two composite unique constraints, WAL plus `busy_timeout`
  plus `foreign_keys` plus `synchronous` pragmas, and `write_tx` as `BEGIN IMMEDIATE` that
  refuses to nest.
- `quidz.inbox`: claim with a lease, park, drain, and a global retry budget; the claim row and
  the ledger effect commit in one transaction.
- `quidz.ledger`: transactional application of one canonical event, with the duplicate path
  decided by the unique index rather than by a prior SELECT.
- `quidz.reconcile`: the thirteen-member drift taxonomy, the two-source classifier, a fail-closed
  gate scoped to the affected payments with materiality thresholds and an audited break-glass,
  and stable JSON and text report renderers.
- `quidz.dlq`: typed reason codes, full-jitter bounded backoff from an unpredictable generator
  that only a test seeds, and an `unknown` default that is retryable with a cap.
- `quidz.sim`: a synthetic provider with seven break modes, a payment list and a settlement
  report, generating no card-like data.
- `quidz.metrics`: twelve operational counters as an allowlist.
- `quidz.cli`: `quidz demo`, `quidz replay` and `quidz reconcile`, with the exit-code contract
  documented in the README.
- `quidz.app`: an optional FastAPI adapter behind the `server` extra, the only module permitted
  to import a web framework.
- 140 tests, deterministic and network-free, including an import-boundary scan, a forced INSERT
  race across eight threads, and a 20 permutation seeded replay of one event stream.
- `mypy --strict` over the package and the tests on every interpreter leg, so the `py.typed`
  marker the wheel ships is a checked promise rather than an asserted one.
