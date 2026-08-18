# QUIDZ

Payment webhook reconciliation with a fail-closed gate on outbound money movement. QUIDZ
(quid, money, slang) verifies signed webhook deliveries from a synthetic provider, applies
them to an idempotent ledger, and reports the drift between the event stream, the local
ledger and a settlement report.

## Development

```bash
uv sync --all-extras --dev
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

## License

MIT. See [LICENSE](LICENSE).
