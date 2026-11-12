#!/bin/sh
# Copies the Alpaca PAPER keys from Claude's MCP settings (~/.claude.json) into the repo's
# encrypted GitHub secrets, without printing them. Run on the Mac:  sh scripts/set_alpaca_secrets.sh
REPO=${1:-goatmoments894/paper-trader}
GH=$(command -v gh || echo ~/.local/bin/gh)
for k in ALPACA_API_KEY ALPACA_SECRET_KEY; do
  python3 -c "import json,os;print(json.load(open(os.path.expanduser('~/.claude.json')))['mcpServers']['alpaca']['env']['$k'],end='')" \
    | "$GH" secret set "$k" -R "$REPO" && echo "$k saved"
done
