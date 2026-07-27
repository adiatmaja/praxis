#!/bin/bash
set -euo pipefail

: "${REPO_URL:?REPO_URL is required}"
: "${BRANCH:?BRANCH is required}"
: "${BASE_BRANCH:?BASE_BRANCH is required}"
: "${TASK_PROMPT:?TASK_PROMPT is required}"
# NOTE: MODEL carries the Gemini model string verbatim, e.g. "Gemini 3.5 Flash (High)".
# agy does NOT use OPENAI_API_BASE (it talks to Google via OAuth creds, not LM Studio),
# so that env var is tolerated if set but intentionally not required here.
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

echo "=== agy (Antigravity) agent starting ==="
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

echo "--- Setting up base branch ${BASE_BRANCH} ---"
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
if [ "${SINGLE_BRANCH:-0}" = "1" ] && git rev-parse --verify "origin/${BRANCH}" >/dev/null 2>&1; then
    echo "--- Single-branch mode: reusing existing origin/${BRANCH} ---"
    git checkout -b "${BRANCH}" "origin/${BRANCH}"
else
    echo "--- Creating branch ${BRANCH} from ${BASE_BRANCH} ---"
    git checkout -b "${BRANCH}"
fi

echo "--- Writing Static Bible (task context, never committed) ---"
# The Bible is the compaction-proof context slot: goal + handover + conventions.
# For agy we prepend the Bible into the effective prompt rather than using a
# separate instructions file, because agy's --add-dir mechanism ingests whole
# directories (not single markdown files) and its per-prompt context injection
# via -p is the most reliable headless path.
#
# We still write .praxis-bible.md to disk (for human inspection during debug)
# and register it in .git/info/exclude so `git add -A` never commits it.

# Build a runtime environment manifest so the worker knows what is available.
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
    if command -v agy >/dev/null 2>&1; then
        printf '%s\n' "- agy: $(agy --version 2>&1 || true)"
    fi
} > "${WORKSPACE}/.praxis-bible.md"

# Append the orchestrator-supplied Bible (goal + handover + conventions).
if [ -n "${BIBLE_TEXT:-}" ]; then
    printf "\n%s\n" "${BIBLE_TEXT}" >> "${WORKSPACE}/.praxis-bible.md"
fi

echo ".praxis-bible.md" >> "${WORKSPACE}/.git/info/exclude"

echo "--- Building effective prompt (Bible prepended to task prompt) ---"
# We prepend the Bible text into the -p prompt. This ensures the context is
# always present regardless of how agy handles additional context flags.
# UNCERTAINTY NOTE: agy's --add-dir flag ingests a directory of files as
# context, but its exact headless behaviour for single-file injection is
# undocumented. The safest approach for headless automation is prompt prepend.
BIBLE_CONTENT=""
if [ -f "${WORKSPACE}/.praxis-bible.md" ]; then
    BIBLE_CONTENT=$(cat "${WORKSPACE}/.praxis-bible.md")
fi

EFFECTIVE_PROMPT="${TASK_PROMPT}"
if [ -n "${PLAN_PATH:-}" ]; then
    EFFECTIVE_PROMPT="Plan reference: ${PLAN_PATH}

${TASK_PROMPT}"
elif [ -n "${PLAN_TEXT:-}" ]; then
    EFFECTIVE_PROMPT="${PLAN_TEXT}

${TASK_PROMPT}"
fi

# Prepend Bible if available (non-empty).
if [ -n "${BIBLE_CONTENT}" ]; then
    EFFECTIVE_PROMPT="${BIBLE_CONTENT}

---

${EFFECTIVE_PROMPT}"
fi

echo "--- Verifying OAuth creds are present ---"
# The orchestrator mounts the praxis-gemini-creds volume read-write at
# ~/.gemini. It is populated once by an interactive `agy login` (see
# docs/deployment.md). Read-write so agy can persist refreshed access tokens
# (they expire in ~1h). A fresh worker process reads these Linux-native creds
# back and authenticates without any browser flow.
if [ -f "/home/agent/.gemini/antigravity-cli/conversation_summaries.db" ] \
    || [ -d "/home/agent/.gemini/antigravity-cli" ]; then
    echo "OAuth creds volume present at ~/.gemini"
else
    echo "WARNING: ~/.gemini has no agy credentials; authentication will fail."
    echo "Run the one-time 'agy login' setup described in docs/deployment.md."
fi

echo "--- Running agy (headless) ---"
# Verified invocation (agy v1.1.2 Linux):
#   agy --dangerously-skip-permissions --mode accept-edits --model "$MODEL" -p "$PROMPT"
#
# Flag notes:
#   --dangerously-skip-permissions : auto-approve all tool permission requests
#   --mode accept-edits            : non-interactive edit mode
#   --print-timeout 30m            : generous timeout for long tasks (default 5m)
#   --model                        : Gemini model string, e.g. "Gemini 3.5 Flash (High)"
#   -p / --print                   : one-shot prompt, print output to stdout
#
# Note: --headless and --approve are NOT valid flags in v1.1.x; removed.
# agy reads OAuth creds from ~/.gemini (a named Docker volume mounted read-write
# by the orchestrator, name set via GEMINI_CREDS_VOLUME). No OPENAI_API_BASE needed.

OUTPUT_LOG="$(mktemp)"
set +e
agy --dangerously-skip-permissions --mode accept-edits --print-timeout 30m \
    --model "${MODEL}" -p "${EFFECTIVE_PROMPT}" \
    2>&1 | tee "${OUTPUT_LOG}"
agy_rc="${PIPESTATUS[0]}"
set -e
if [ "${agy_rc}" -ne 0 ]; then
    exit "${agy_rc}"
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

echo "--- Committing changes (agy does not auto-commit) ---"
# .praxis-bible.md is excluded via .git/info/exclude, so git add -A never
# stages it and it can never appear in the PR.
git add -A
if git diff --cached --quiet; then
    # The worker may have committed its own work; a clean tree is only a
    # failure when the branch also has no commits beyond the base.
    ahead=$(git rev-list --count "${BASE_BRANCH}..HEAD")
    if [ "${ahead}" -gt 0 ]; then
        echo "Worker committed its own work (${ahead} commit(s) ahead of ${BASE_BRANCH})"
    else
        echo "No changes produced by agy"
        STATUS="failed"
        exit 1
    fi
else
    git commit -m "agent: ${BRANCH}"
fi

echo "--- Pushing branch ---"
# Force-push is safe here: each attempt is a full re-implementation starting
# from the base branch, so the fresh branch is always authoritative.
if [ "${SINGLE_BRANCH:-0}" = "1" ]; then
    git push -u origin "${BRANCH}"
else
    git push -u --force origin "${BRANCH}"
fi

echo "--- Creating PR ---"
# A previous attempt may already have opened a PR for this branch; reuse it.
if PR_URL=$(gh pr view "${BRANCH}" --json url --jq .url 2>/dev/null) && [ -n "${PR_URL}" ]; then
    echo "Reusing existing PR: ${PR_URL}"
else
PR_URL=$(gh pr create \
    --title "agent: ${BRANCH}" \
    --body "Task: ${TASK_SUMMARY:-${BRANCH}}

---
Implemented by \`${MODEL}\` (harness: agy)" \
    --base "${BASE_BRANCH}" \
    --head "${BRANCH}")
fi

echo "PR created: ${PR_URL}"
echo "=== agy agent completed ==="
