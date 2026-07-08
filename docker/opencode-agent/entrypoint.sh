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
QUESTION=""

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

    local question_json="null"
    if [ -n "${QUESTION:-}" ]; then
        question_json=$(printf "%s" "${QUESTION}" | json_escape)
    fi

    local payload="{\"task_id\":\"${TASK_ID}\",\"run_id\":${run_json},\"status\":\"${STATUS}\",\"pr_url\":${pr_json},\"question\":${question_json}}"
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
# Commit author identity is provided by the orchestrator (GIT_AUTHOR_*); fall
# back to a neutral GitHub noreply identity when unset. Kept free of any Praxis
# footprint in the commit profile.
git config user.name "${GIT_AUTHOR_NAME:-dev-bot}"
git config user.email "${GIT_AUTHOR_EMAIL:-dev-bot@users.noreply.github.com}"

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

echo "--- Writing Static Bible to a separate instructions file (never committed) ---"
# The Bible is a compaction-proof context slot: goal + handover + conventions
# re-sent to the model every turn. We write it to its OWN file and load it via
# OpenCode's `instructions` config (combined with the repo's AGENTS.md into
# context) instead of injecting it into AGENTS.md. This means:
#   - we never touch/dereference the repo's AGENTS.md (which may be a symlink),
#   - the Bible can never leak into a PR, so no strip/restore logic is needed.
# `.git/info/exclude` is per-clone and uncommitted, so `git add -A` skips it.
BIBLE_INSTRUCTIONS=""
if [ -n "${BIBLE_TEXT:-}" ]; then
    printf "%s\n" "${BIBLE_TEXT}" > "${WORKSPACE}/.praxis-bible.md"
    echo ".praxis-bible.md" >> "${WORKSPACE}/.git/info/exclude"
    BIBLE_INSTRUCTIONS='  "instructions": [".praxis-bible.md"],'
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
${BIBLE_INSTRUCTIONS}
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

OUTPUT_LOG="$(mktemp)"
set +e
opencode run --model "lmstudio/${MODEL}" "${EFFECTIVE_PROMPT}" 2>&1 | tee "${OUTPUT_LOG}"
opencode_rc="${PIPESTATUS[0]}"
set -e
if [ "${opencode_rc}" -ne 0 ]; then
    exit "${opencode_rc}"
fi

report_status=$(grep -oE '^Status:[[:space:]]*[A-Z_]+' "${OUTPUT_LOG}" \
    | tail -n1 | sed -E 's/^Status:[[:space:]]*//' ) || true

if [ "${report_status}" = "BLOCKED" ] || [ "${report_status}" = "NEEDS_CONTEXT" ]; then
    echo "--- Worker reported ${report_status}; sending clarification request (no PR) ---"
    QUESTION=$(awk '/^Concerns/{flag=1;next}/^====/{flag=0}flag' "${OUTPUT_LOG}" \
        | sed '/^[[:space:]]*$/d')
    [ -z "${QUESTION}" ] && QUESTION="Worker reported ${report_status} without details."
    STATUS="needs_clarification"
    send_callback
    trap - EXIT
    exit 0
fi

echo "--- Committing changes (OpenCode does not auto-commit) ---"
# No Bible cleanup needed: it lives in .praxis-bible.md, excluded via
# .git/info/exclude, so it never enters the index and never touches AGENTS.md.
git add -A
if git diff --cached --quiet; then
    # The worker may have committed its own work; a clean tree is only a
    # failure when the branch also has no commits beyond the base.
    ahead=$(git rev-list --count "${BASE_BRANCH}..HEAD")
    if [ "${ahead}" -gt 0 ]; then
        echo "Worker committed its own work (${ahead} commit(s) ahead of ${BASE_BRANCH})"
    else
        echo "No changes produced by OpenCode"
        STATUS="failed"
        exit 1
    fi
else
    git commit -m "agent: ${BRANCH}"
fi

echo "--- Pushing branch ---"
# Retries rebuild the agent branch from base while the remote may still hold a
# previous attempt's commits; each attempt is a full re-implementation, so the
# fresh branch is authoritative and a force push is correct here.
git push -u --force origin "${BRANCH}"

echo "--- Creating PR ---"
# A previous attempt may already have opened a PR for this branch; reuse it.
if PR_URL=$(gh pr view "${BRANCH}" --json url --jq .url 2>/dev/null) && [ -n "${PR_URL}" ]; then
    echo "Reusing existing PR: ${PR_URL}"
else
PR_URL=$(gh pr create \
    --title "agent: ${BRANCH}" \
    --body "Task: ${TASK_SUMMARY:-${BRANCH}}

---
Implemented by \`${MODEL}\` (harness: opencode)" \
    --base "${BASE_BRANCH}" \
    --head "${BRANCH}")
fi

echo "PR created: ${PR_URL}"
echo "=== OpenCode agent completed ==="
