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

# Guard: workers must NEVER target a protected base branch (main/master/release*).
# Doing so collapses two-tier branching and (worst case) points a PR at main.
# Match case-insensitively and hard-exit before any clone/branch/push/PR.
base_branch_lower=$(printf '%s' "${BASE_BRANCH}" | tr '[:upper:]' '[:lower:]')
case "${base_branch_lower}" in
    main | master | release*)
        printf 'PRAXIS_FATAL_PROTECTED_BASE: base branch %s is protected; workers must never target it\n' \
            "'${BASE_BRANCH}'"
        STATUS="failed"
        exit 1
        ;;
esac

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

echo "=== OpenHands agent starting ==="
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

echo "--- Writing environment manifest + Static Bible (never committed) ---"
{
    printf '%s\n' "# ENVIRONMENT (this container -- use what is already here; do NOT install your own)"
    printf '%s\n\n' "- Non-root user. NO sudo. NO apt (permission denied). Network may be restricted."
    if command -v python3 >/dev/null 2>&1; then
        printf '%s\n' "- python3: $(python3 --version 2>&1)"
    fi
    if command -v uv >/dev/null 2>&1; then
        printf '%s\n' "- uv: $(uv --version 2>&1) at $(command -v uv)"
        printf '%s\n' '  Run Python tools via `uv run <tool>` (e.g. `uv run pytest`, `uv run ruff check .`, `uv run mypy src`).'
        printf '%s\n' '  uv installs project deps on demand -- do NOT use pip/apt/get-pip.'
    else
        printf '%s\n' "- uv: NOT available on PATH."
    fi
    if command -v git >/dev/null 2>&1; then
        printf '%s\n' "- git: $(git --version 2>&1)"
    fi
    if command -v gh >/dev/null 2>&1; then
        printf '%s\n' "- gh: present"
    fi
    if command -v node >/dev/null 2>&1; then
        printf '%s\n' "- node: $(node --version 2>&1)"
    fi
} > "${WORKSPACE}/.praxis-bible.md"
if [ -n "${BIBLE_TEXT:-}" ]; then
    printf "\n%s\n" "${BIBLE_TEXT}" >> "${WORKSPACE}/.praxis-bible.md"
fi
echo ".praxis-bible.md" >> "${WORKSPACE}/.git/info/exclude"

echo "--- Running OpenHands (headless, local runtime) ---"
# LiteLLM openai-compatible config via env; --override-with-envs applies them.
export LLM_MODEL="openai/${MODEL}"
export LLM_BASE_URL="${OPENAI_API_BASE}"
export LLM_API_KEY="${OPENAI_API_KEY:-not-needed}"
export RUNTIME="local"

# OpenHands has no persistent instructions/--read mechanism, so fold the
# environment manifest + Static Bible directly into the prompt it consumes.
BIBLE_PREAMBLE=""
if [ -s "${WORKSPACE}/.praxis-bible.md" ]; then
    BIBLE_PREAMBLE="$(cat "${WORKSPACE}/.praxis-bible.md")

"
fi

# Prepend plan context to the prompt when supplied.
EFFECTIVE_PROMPT="${BIBLE_PREAMBLE}${TASK_PROMPT}"
if [ -n "${PLAN_PATH:-}" ]; then
    EFFECTIVE_PROMPT="${BIBLE_PREAMBLE}Plan reference: ${PLAN_PATH}

${TASK_PROMPT}"
elif [ -n "${PLAN_TEXT:-}" ]; then
    EFFECTIVE_PROMPT="${BIBLE_PREAMBLE}${PLAN_TEXT}

${TASK_PROMPT}"
fi

OUTPUT_LOG="$(mktemp)"
set +e
python3 -m openhands.core.main -t "${EFFECTIVE_PROMPT}" --override-with-envs 2>&1 | tee "${OUTPUT_LOG}"
openhands_rc="${PIPESTATUS[0]}"
set -e
if [ "${openhands_rc}" -ne 0 ]; then
    exit "${openhands_rc}"
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

echo "--- Committing changes (OpenHands may not commit) ---"
git add -A
if git diff --cached --quiet; then
    echo "No changes produced by OpenHands"
    STATUS="failed"
    exit 1
fi
git commit -m "agent: ${BRANCH}" || echo "Nothing to commit (already committed)"

echo "--- Pushing branch ---"
git push -u origin "${BRANCH}"

echo "--- Creating PR ---"
PR_URL=$(gh pr create \
    --title "agent: ${BRANCH}" \
    --body "Task: ${TASK_SUMMARY:-${BRANCH}}

---
Implemented by \`${MODEL}\` (harness: openhands)" \
    --base "${BASE_BRANCH}" \
    --head "${BRANCH}")

echo "PR created: ${PR_URL}"
echo "=== OpenHands agent completed ==="
