#!/usr/bin/env bash
# Add one host name to the Worker's HOSTS list in wrangler.toml.
#
# Usage: deploy/worker/add-host.sh <name>
#
# The name is the DNS label and the role passed to deploy/run.sh, for example
# `galaxy-tab-s2`. The script is idempotent and does not deploy anything.
set -euo pipefail

HOST="${1:?usage: add-host.sh <name>}"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"

python3 - "$ROOT/deploy/worker/wrangler.toml" "$HOST" <<'PY'
import re
import sys

path, host = sys.argv[1], sys.argv[2]
text = open(path, encoding="utf-8").read()
match = re.search(r'^HOSTS = "([^"]*)"', text, re.MULTILINE)
if match is None:
    raise SystemExit("HOSTS not found in deploy/worker/wrangler.toml")

hosts = [name.strip() for name in match.group(1).split(",") if name.strip()]
if host not in hosts:
    hosts.append(host)
    text = text[: match.start()] + f'HOSTS = "{",".join(hosts)}"' + text[match.end() :]
    open(path, "w", encoding="utf-8").write(text)
print("HOSTS = " + ",".join(hosts))
PY

echo "Redeploy the Worker after adding hosts: cd deploy/worker && npx wrangler deploy"
