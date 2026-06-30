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
    python -c 'import json,sys; print(json.dumps(sys.stdin.read()))'
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

echo "=== Agent starting ==="
echo "Repo: ${REPO_URL}"
echo "Branch: ${BRANCH}"
echo "Base: ${BASE_BRANCH}"
echo "Model: openai/${MODEL}"

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
    echo "Base branch ${BASE_BRANCH} not found on remote, creating from default branch"
    git checkout -b "${BASE_BRANCH}"
    if ! git push -u origin "${BASE_BRANCH}" 2>/dev/null; then
        echo "Push failed (branch may already exist), fetching from remote"
        git fetch origin "${BASE_BRANCH}"
        git reset --hard "origin/${BASE_BRANCH}"
    fi
fi
git checkout -b "${BRANCH}"

echo "--- Running Aider ---"
export OPENAI_API_KEY="${OPENAI_API_KEY:-not-needed}"

# Build --read args so Aider has reference context (read-only) while implementing.
read_args=()

# 1. The plan file (existing behavior).
if [ -n "${PLAN_PATH:-}" ]; then
    read_args+=(--read "${PLAN_PATH}")
elif [ -n "${PLAN_TEXT:-}" ]; then
    printf "%s" "${PLAN_TEXT}" > "${WORKSPACE}/.praxis-plan.md"
    read_args+=(--read ".praxis-plan.md")
fi

# 2. Caller-curated, secret-scrubbed context from the orchestrator.
if [ -n "${CONTEXT_TEXT:-}" ]; then
    printf "%s" "${CONTEXT_TEXT}" > "${WORKSPACE}/.praxis-context.md"
    read_args+=(--read ".praxis-context.md")
fi

# 2b. Static Bible: goal + handover + conventions, re-sent each message by Aider.
if [ -n "${BIBLE_TEXT:-}" ]; then
    printf "%s\n" "${BIBLE_TEXT}" > "${WORKSPACE}/.praxis-bible.md"
    read_args+=(--read ".praxis-bible.md")
fi

# 3. Repo-local project memory already committed in the clone (GitHub-only:
#    we never mount local or gitignored files). Best-effort; skip if absent.
for ctx in CLAUDE.md MEMORY.md AGENTS.md; do
    if [ -f "${WORKSPACE}/${ctx}" ]; then
        read_args+=(--read "${ctx}")
    fi
done
while IFS= read -r doc; do
    [ -n "${doc}" ] && read_args+=(--read "${doc}")
done < <(find "${WORKSPACE}/docs" -maxdepth 1 -name '*.md' 2>/dev/null | sed "s|${WORKSPACE}/||" || true)

aider \
    --message "${TASK_PROMPT}" \
    --model "openai/${MODEL}" \
    --auto-commits \
    --yes-always \
    --no-auto-lint \
    --no-suggest-shell-commands \
    --no-show-model-warnings \
    --no-browser \
    --no-detect-urls \
    "${read_args[@]}"

echo "--- Pushing branch ---"
git push -u origin "${BRANCH}"

echo "--- Creating PR ---"
PR_URL=$(gh pr create \
    --title "agent: ${BRANCH}" \
    --body "Automated implementation by AI Agent.

Task: ${TASK_SUMMARY:-${BRANCH}}

---
Generated by AI Agent Orchestrator" \
    --base "${BASE_BRANCH}" \
    --head "${BRANCH}")

echo "PR created: ${PR_URL}"
echo "=== Agent completed ==="
