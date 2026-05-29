#!/usr/bin/env bash
# Generate a 6-word diceware passphrase for the nomnom relay HMAC secret.
# Words come from the EFF short wordlist (1296 words, <=5 letters, CC0).
# 6 words ~= 62 bits of entropy. Pipe into:
#   ./scripts/generate-secret.sh | npx wrangler secret put NOMNOM_HMAC_SECRET
set -euo pipefail
DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
exec python3 -c '
import pathlib, secrets, sys
words = pathlib.Path(sys.argv[1]).read_text().splitlines()
print("-".join(secrets.choice(words) for _ in range(6)), end="")
' "$DIR/wordlist.txt"
