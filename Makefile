# Convenience targets. The Python CLI and relay-worker each have their own tooling
# (pytest / npm); this just wires up the cross-language crypto fixtures.

.PHONY: fixtures
fixtures: ## Regenerate nomnom-web's crypto interop fixtures from nomnom.py
	uv run python tools/gen_crypto_fixtures.py --out nomnom-web/test/fixtures/crypto-vectors.json
