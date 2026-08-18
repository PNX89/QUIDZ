# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
- `quidz.dlq`: typed reason codes, full-jitter bounded backoff from a seeded generator, and an
  `unknown` default that is retryable with a cap.
- `quidz.sim`: a synthetic provider with six break modes, a payment list and a settlement report,
  generating no card-like data.
- `quidz.metrics`: twelve operational counters as an allowlist.
- `quidz.cli`: `quidz demo`, `quidz replay` and `quidz reconcile`, with the exit-code contract
  documented in the README.
- `quidz.app`: an optional FastAPI adapter behind the `server` extra, the only module permitted
  to import a web framework.
- 116 tests, deterministic and network-free, including an import-boundary scan, a forced INSERT
  race across eight threads, and a 20 permutation seeded replay of one event stream.

[0.1.0]: https://github.com/PNX89/QUIDZ/releases/tag/v0.1.0
