#!/bin/bash
set -euo pipefail

: "${REPO_URL:?REPO_URL is required}"
: "${BRANCH:?BRANCH is required}"
: "${BASE_BRANCH:?BASE_BRANCH is required}"
: "${TASK_PROMPT:?TASK_PROMPT is required}"
: "${OPENAI_API_BASE:?OPENAI_API_BASE is required}"
: "${MODEL:?MODEL is required}"
: "${GH_TOKEN:?GH_TOKEN is required}"
: "${CALLBACK_URL:?CALLBACK_URL is required}"
: "${TASK_ID:?TASK_ID is required}"

WORKSPACE="/home/agent/workspace"
STATUS="completed"
PR_URL=""

json_escape() {
    python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))'
}

send_callback() {
    local pr_json="null"
    local run_json="null"
    if [ -n "${PR_URL}" ]; then
        pr_json=$(printf "%s" "${PR_URL}" | json_escape)
    fi
    if [ -n "${RUN_ID:-}" ]; then
        run_json=$(printf "%s" "${RUN_ID}" | json_escape)
    fi
    local payload="{\"task_id\":\"${TASK_ID}\",\"run_id\":${run_json},\"status\":\"${STATUS}\",\"pr_url\":${pr_json}}"
    local max_attempts="${CALLBACK_MAX_ATTEMPTS:-5}"
    local attempt=1
    while [ "${attempt}" -le "${max_attempts}" ]; do
        local code
        # CALLBACK_TOKEN is the shared secret set by the orchestrator.
        # The header is omitted when unset so local dev without a token still works.
        local token_header=()
        if [ -n "${CALLBACK_TOKEN:-}" ]; then
            token_header=(-H "X-Praxis-Callback-Token: ${CALLBACK_TOKEN}")
        fi
        code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 \
            -X POST "${CALLBACK_URL}" \
            -H "Content-Type: application/json" \
            "${token_header[@]}" \
            -d "${payload}" || echo "000")
        if [ "${code}" = "200" ]; then
            echo "Callback delivered on attempt ${attempt}"
            return 0
        fi
        echo "WARNING: callback attempt ${attempt}/${max_attempts} failed (HTTP ${code})"
        attempt=$((attempt + 1))
        sleep $((attempt * 2))
    done
    echo "ERROR: callback failed after ${max_attempts} attempts; orchestrator will reconcile"
}

cleanup() {
    local exit_status=$?
    if [ "${exit_status}" -ne 0 ]; then
        STATUS="failed"
    fi
    send_callback
    exit "${exit_status}"
}
trap cleanup EXIT

echo "=== OpenCode agent starting ==="
echo "Repo: ${REPO_URL}"
echo "Branch: ${BRANCH}  Base: ${BASE_BRANCH}  Model: ${MODEL}"

echo "--- Configuring git auth ---"
git config --global credential.helper '!f() { echo "username=x-access-token"; echo "password=${GH_TOKEN}"; }; f'

echo "--- Cloning repository ---"
git clone "${REPO_URL}" "${WORKSPACE}"
cd "${WORKSPACE}"
git config user.email "agent@orchestrator.local"
git config user.name "AI Agent"

echo "--- Creating branch ${BRANCH} from ${BASE_BRANCH} ---"
if git rev-parse --verify "origin/${BASE_BRANCH}" >/dev/null 2>&1; then
    git checkout -b "${BASE_BRANCH}" "origin/${BASE_BRANCH}"
else
    echo "Base branch not on remote; creating from default"
    git checkout -b "${BASE_BRANCH}"
    if ! git push -u origin "${BASE_BRANCH}" 2>/dev/null; then
        echo "Push failed (branch may exist), fetching"
        git fetch origin "${BASE_BRANCH}"
        git reset --hard "origin/${BASE_BRANCH}"
    fi
fi
git checkout -b "${BRANCH}"

echo "--- Writing Static Bible to AGENTS.md (persists across compaction) ---"
if [ -n "${BIBLE_TEXT:-}" ]; then
    bible_block=".praxis-bible-tmp.md"
    printf "%s\n" "${BIBLE_TEXT}" > "${bible_block}"
    if [ -f "${WORKSPACE}/AGENTS.md" ]; then
        # Preserve the repo's own AGENTS.md; prepend the Bible in a fenced block.
        {
            echo "<!-- praxis:bible:start -->"
            cat "${bible_block}"
            echo "<!-- praxis:bible:end -->"
            echo ""
            cat "${WORKSPACE}/AGENTS.md"
        } > "${WORKSPACE}/AGENTS.md.new"
        mv "${WORKSPACE}/AGENTS.md.new" "${WORKSPACE}/AGENTS.md"
    else
        {
            echo "<!-- praxis:bible:start -->"
            cat "${bible_block}"
            echo "<!-- praxis:bible:end -->"
        } > "${WORKSPACE}/AGENTS.md"
    fi
    rm -f "${bible_block}"
fi

echo "--- Writing OpenCode config (OpenAI-compatible local provider) ---"
mkdir -p "${HOME}/.config/opencode"
# MODEL_CONTEXT_LIMIT is detected per-model from LM Studio by the orchestrator
# (never hardcoded). When present, advertise it as the model's context limit so
# OpenCode's auto-compaction triggers at the real window instead of overflowing
# into silent server-side truncation. Omitted -> OpenCode uses its own default.
model_cfg='{ "name": "'"${MODEL}"'" }'
if [ -n "${MODEL_CONTEXT_LIMIT:-}" ]; then
    # OpenCode's schema requires BOTH context and output in a limit block;
    # omitting output fails validation ("Missing key ...limit.output").
    model_cfg='{ "name": "'"${MODEL}"'", "limit": { "context": '"${MODEL_CONTEXT_LIMIT}"', "output": 8192 } }'
    echo "Using detected context limit: ${MODEL_CONTEXT_LIMIT}"
fi
cat > "${HOME}/.config/opencode/opencode.json" <<EOF
{
  "\$schema": "https://opencode.ai/config.json",
  "provider": {
    "lmstudio": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "LM Studio (local)",
      "options": { "baseURL": "${OPENAI_API_BASE}", "apiKey": "not-needed" },
      "models": { "${MODEL}": ${model_cfg} }
    }
  }
}
EOF

echo "--- Running OpenCode (headless) ---"
export OPENAI_API_KEY="${OPENAI_API_KEY:-not-needed}"

# Prepend plan context to the prompt when supplied.
EFFECTIVE_PROMPT="${TASK_PROMPT}"
if [ -n "${PLAN_PATH:-}" ]; then
    EFFECTIVE_PROMPT="Plan reference: ${PLAN_PATH}

${TASK_PROMPT}"
elif [ -n "${PLAN_TEXT:-}" ]; then
    EFFECTIVE_PROMPT="${PLAN_TEXT}

${TASK_PROMPT}"
fi

opencode run --model "lmstudio/${MODEL}" "${EFFECTIVE_PROMPT}"

echo "--- Removing injected Static Bible before commit (keep it out of the PR) ---"
# The Bible is injected into AGENTS.md only as a compaction-proof context slot;
# it must never land in the PR. Strip the marked block; if we created AGENTS.md
# ourselves (untracked) and nothing else remains, delete it.
if [ -f "${WORKSPACE}/AGENTS.md" ] && grep -q "praxis:bible:start" "${WORKSPACE}/AGENTS.md"; then
    sed -i '/<!-- praxis:bible:start -->/,/<!-- praxis:bible:end -->/d' "${WORKSPACE}/AGENTS.md"
    sed -i '/./,$!d' "${WORKSPACE}/AGENTS.md"  # drop leading blank lines
    if ! git ls-files --error-unmatch AGENTS.md >/dev/null 2>&1 && [ ! -s "${WORKSPACE}/AGENTS.md" ]; then
        rm -f "${WORKSPACE}/AGENTS.md"
    fi
fi

echo "--- Committing changes (OpenCode does not auto-commit) ---"
git add -A
if git diff --cached --quiet; then
    echo "No changes produced by OpenCode"
    STATUS="failed"
    exit 1
fi
git commit -m "agent: ${BRANCH}"

echo "--- Pushing branch ---"
git push -u origin "${BRANCH}"

echo "--- Creating PR ---"
PR_URL=$(gh pr create \
    --title "agent: ${BRANCH}" \
    --body "Automated implementation by OpenCode agent.

Task: ${TASK_SUMMARY:-${BRANCH}}

---
Generated by Praxis (harness: opencode)" \
    --base "${BASE_BRANCH}" \
    --head "${BRANCH}")

echo "PR created: ${PR_URL}"
echo "=== OpenCode agent completed ==="
