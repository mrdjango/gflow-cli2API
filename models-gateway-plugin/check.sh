#!/usr/bin/env bash
# Lints the plugin and replays the fixture with the gateway's own plugin CLI.
# Needs a models-gateway checkout; defaults to the sibling of this repo.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
gateway="${GATEWAY_DIR:-$here/../../models-gateway}"

cd "$gateway"
CGO_ENABLED=0 go run . plugin lint "$here/plugin.js"
CGO_ENABLED=0 go run . plugin test "$here/plugin.js" --fixture "$here/gflow.fixture.json"
