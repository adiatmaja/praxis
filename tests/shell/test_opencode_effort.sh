#!/usr/bin/env bash
# Asserts the generated OpenCode config states reasoning effort explicitly.
# This EXECUTES the config-writing block of the real entrypoint.sh; a syntax
# check alone has shipped a real bug here before (printf leading-dash), so
# never settle for `bash -n`.
#
# Key name note: the OpenCode config schema for a per-model request option
# uses camelCase ("options": { "reasoningEffort": ... }), NOT snake_case
# "reasoning_effort". Verified against https://opencode.ai/docs/models/ and
# https://github.com/anomalyco/opencode/issues/23622, the latter using the
# exact same npm package ("@ai-sdk/openai-compatible") and provider id
# ("lmstudio" is the docs' own worked example at
# https://opencode.ai/docs/providers/) as this entrypoint. OpenCode's own
# transform layer converts the config-time camelCase key into the wire-level
# snake_case "reasoning_effort" field before it reaches the LM Studio HTTP
# API; this test asserts the config-file side, which is what this task owns.
set -euo pipefail

WORK="$(mktemp -d)"
export HOME="${WORK}"
export MODEL="qwen3.8-27b"
export OPENAI_API_BASE="http://host.docker.internal:1234/v1"
export MODEL_CONTEXT_LIMIT="${MODEL_CONTEXT_LIMIT-32768}"
export WORKER_REASONING_EFFORT="medium"
BIBLE_INSTRUCTIONS=''

# Extract and run only the config-writing block from the real entrypoint.
sed -n '/^echo "--- Writing OpenCode config/,/^EOF$/p' \
    "$(dirname "$0")/../../docker/opencode-agent/entrypoint.sh" > "${WORK}/block.sh"
if [ ! -s "${WORK}/block.sh" ]; then
    echo "FAIL: sed extracted an empty block -- markers no longer match entrypoint.sh" >&2
    exit 1
fi
# shellcheck disable=SC1090
. "${WORK}/block.sh"

CFG="${HOME}/.config/opencode/opencode.json"
python3 - "$CFG" <<'PY'
import json, sys
cfg = json.load(open(sys.argv[1]))
model = cfg["provider"]["lmstudio"]["models"]["qwen3.8-27b"]
opts = model.get("options", {})
assert opts.get("reasoningEffort") == "medium", (
    f"expected reasoningEffort=medium, got {opts!r}"
)
print("OK: reasoningEffort stated explicitly")
PY
rm -rf "${WORK}"
