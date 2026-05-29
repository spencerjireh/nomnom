#!/usr/bin/env bash
# Generate a fresh 32-byte (256-bit) random HMAC secret for the nomnom relay.
# Output is base64-encoded, no trailing newline, suitable for piping into:
#   ./scripts/generate-secret.sh | npx wrangler secret put NOMNOM_HMAC_SECRET
set -euo pipefail
openssl rand -base64 32 | tr -d '\n'
