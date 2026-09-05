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

# Which git plumbing to use: "github" (PRs via gh) or "local" (a bind-mounted
# bare repo, no credential, no PR object). Defaults to github so an older
# orchestrator that does not set it behaves exactly as before.
GIT_BACKEND="${GIT_BACKEND:-github}"

# Single source of truth for "this run uses the local git backend". Derived
# once here and tested everywhere else, so the two spots (credential helper
# below, PR block further down) can never drift apart again -- exactly the
# REUSING_BRANCH precedent below, for exactly the same reason. They used to
# test opposite sides of DIFFERENT values (= "github" here, = "local" there),
# so any third value -- a typo, a future backend, an agent image lagging the
# orchestrator -- got NO credential helper AND the full gh path, the worst
# possible combination. One boolean makes anything that is not exactly "local"
# behave as github, which is the safe default.
IS_LOCAL_BACKEND=0
if [ "${GIT_BACKEND}" = "local" ]; then
    IS_LOCAL_BACKEND=1
fi

WORKSPACE="/home/agent/workspace"
STATUS="completed"
PR_URL=""
QUESTION=""
CAPTURED_SESSION_ID=""

# Single source of truth for "this run reuses the existing remote branch
# instead of rebuilding it from base": single-branch mode and a resume turn
# both apply. Computed once here and tested everywhere else so the two spots
# (branch checkout below, push guard further down) can never drift apart
# again -- that drift is exactly what caused the force-push defect this
# variable replaces.
REUSING_BRANCH=0
if [ "${SINGLE_BRANCH:-0}" = "1" ] || [ -n "${WORKER_SESSION_ID:-}" ]; then
    REUSING_BRANCH=1
fi

# Guard: in two-tier mode workers must NEVER target a protected base branch
# (main/master/release*). Doing so collapses two-tier branching and (worst case)
# points a PR at main. Single-branch mode (SINGLE_BRANCH=1, auto-delegate) is the
# exception by design: the plan branch IS the single feature branch, cut from the
# default branch and opening its PR back at it, so base=main is correct there.
# Match case-insensitively and hard-exit before any clone/branch/push/PR.
if [ "${SINGLE_BRANCH:-0}" != "1" ]; then
    base_branch_lower=$(printf '%s' "${BASE_BRANCH}" | tr '[:upper:]' '[:lower:]')
    case "${base_branch_lower}" in
        main | master | release*)
            printf 'PRAXIS_FATAL_PROTECTED_BASE: base branch %s is protected; workers must never target it\n' \
                "'${BASE_BRANCH}'"
            # NO callback here, deliberately, and no STATUS assignment
            # either. This guard runs BEFORE send_callback and the EXIT trap
            # are defined, so the `STATUS="failed"` that used to sit here was
            # dead code that read as "we reported this". Nothing is reported,
            # and that silence is load-bearing: `orchestrator_reconcile`
            # scrapes the PRAXIS_FATAL_PROTECTED_BASE sentinel above out of
            # the container log and marks the run terminal WITHOUT burning a
            # retry, whereas a `failed` callback routes to the retry branch
            # and would spend one on a misconfiguration no retry can fix.
            exit 1
            ;;
    esac
fi

json_escape() {
    python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))'
}

# Percent-encode a branch name so it survives the praxis-local:// query string.
# The orchestrator parses that URL with
#   ^praxis-local://pr\?branch=([^&]+)&base=([^&]+)$
# and percent-decodes each group, so the two characters that MUST be encoded
# are '&' (it would terminate the group early and break the whole match) and
# '%' (an unencoded one would be mis-decoded on the way back). '/' is encoded
# because every Praxis branch has one; ' ' because a caller-named branch may.
# Order matters: '%' first, or the escapes introduced below get re-escaped.
url_encode() {
    printf '%s' "$1" | sed -e 's|%|%25|g' -e 's|/|%2F|g' -e 's| |%20|g' -e 's|&|%26|g'
}

# Escape a value for JSON, or render the bare word `null` if it cannot be.
#
# `json_escape` shells out to python3, and the four call sites below were plain
# assignments, so under `set -e` one failing interpreter aborted send_callback
# MIDWAY: no callback was posted at all, while the last two lines of the run's
# own log still read "PR created: <url>" and "agent completed". The task then
# hung until the reconcile sweep with its log asserting success. A callback
# missing one field is recoverable; a callback that is never sent is the exact
# failure this function exists to prevent, so an escaping failure degrades the
# FIELD and never the delivery. The warning goes to stderr because stdout here
# IS the return value.
escape_or_null() {
    local value="$1"
    local escaped
    if [ -z "${value}" ]; then
        printf 'null'
        return 0
    fi
    if escaped=$(printf '%s' "${value}" | json_escape); then
        printf '%s' "${escaped}"
    else
        echo "WARNING: json escaping failed; reporting this field as null" >&2
        printf 'null'
    fi
}

send_callback() {
    local pr_json run_json question_json session_json
    pr_json=$(escape_or_null "${PR_URL:-}")
    run_json=$(escape_or_null "${RUN_ID:-}")
    question_json=$(escape_or_null "${QUESTION:-}")
    session_json=$(escape_or_null "${CAPTURED_SESSION_ID:-}")

    local payload="{\"task_id\":\"${TASK_ID}\",\"run_id\":${run_json},\"status\":\"${STATUS}\",\"pr_url\":${pr_json},\"question\":${question_json},\"session_id\":${session_json}}"
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
        # `|| code="000"` OUTSIDE the substitution, never `|| echo "000"`
        # inside it: on a connection failure curl writes its own "000" to
        # stdout AND exits non-zero, so BOTH landed in `code` and the operator
        # read "failed (HTTP 000000)", a status code that does not exist.
        code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 \
            -X POST "${CALLBACK_URL}" \
            -H "Content-Type: application/json" \
            "${token_header[@]}" \
            -d "${payload}") || code="000"
        if [ "${code}" = "200" ]; then
            echo "Callback delivered on attempt ${attempt}"
            return 0
        fi
        echo "WARNING: callback attempt ${attempt}/${max_attempts} failed (HTTP ${code})"
        attempt=$((attempt + 1))
        # Only back off if another attempt is actually coming. The old sleep
        # ran after the FINAL attempt too, adding dead time to a run that had
        # already given up.
        if [ "${attempt}" -le "${max_attempts}" ]; then
            sleep $((attempt * 2))
        fi
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

# A STOP REQUEST. `docker stop` (and `praxis stop`, which goes through it) sends
# SIGTERM to this script, PID 1 of the container. Bash defers a trap while a
# FOREGROUND command runs, and as PID 1 an unhandled TERM is ignored outright,
# so the agent ran on until Docker's 30 s SIGKILL: `praxis stop` took 33 s
# measured (round 12, probe 2), and a bare `docker stop` reported `completed`
# with no PR because the harness exited 0. The agent is therefore run in the
# BACKGROUND and waited on (`run_agent`), the one shape in which a trap fires
# promptly; the handler kills it, says so, and `run_agent` returns 143, which
# the EXIT trap above turns into a `failed` callback.
STOP_REQUESTED=0
AGENT_PID=""
on_term() {
    STOP_REQUESTED=1
    echo "Received SIGTERM: stopping the agent and reporting failed"
    if [ -n "${AGENT_PID}" ]; then
        # The whole process GROUP (job control below gives the agent its own),
        # so the agent binary and tee die with the wrapping subshell rather
        # than running on until PID 1 exits.
        kill -TERM -- "-${AGENT_PID}" 2>/dev/null || kill -TERM "${AGENT_PID}" 2>/dev/null || true
    fi
}
trap on_term TERM INT

# Run "$@" (the agent command) piped through tee into ${AGENT_LOG}, in the
# background, and return the AGENT's exit status (never tee's), or 143 after a
# stop request. `wait` returns as soon as a trapped signal arrives, then the
# trap runs; the second wait lets the killed subshell go before returning.
run_agent() {
    # Job control ON for the start alone: a background job then gets its own
    # process group, which is what lets on_term kill agent + tee together.
    set -m
    ( "$@" 2>&1 | tee "${AGENT_LOG}"; exit "${PIPESTATUS[0]}" ) &
    AGENT_PID=$!
    set +m
    local rc
    wait "${AGENT_PID}"
    rc=$?
    if [ "${STOP_REQUESTED}" = "1" ]; then
        wait "${AGENT_PID}" 2>/dev/null || true
        AGENT_PID=""
        return 143
    fi
    AGENT_PID=""
    return "${rc}"
}

echo "=== OpenCode agent starting ==="
echo "Repo: ${REPO_URL}"
echo "Branch: ${BRANCH}  Base: ${BASE_BRANCH}  Model: ${MODEL}"

echo "--- Configuring git auth ---"
# Local mode clones from a bind-mounted bare repo; there is no credential to
# configure (GH_TOKEN is a placeholder there, satisfying the guard above).
if [ "${IS_LOCAL_BACKEND}" = "1" ]; then
    echo "Local backend: skipping credential helper"
else
    git config --global credential.helper '!f() { echo "username=x-access-token"; echo "password=${GH_TOKEN}"; }; f'
fi

# The local bare repo reaches us as a BIND MOUNT, so its owner uid is the host's
# and not this container's agent user. Git's dubious-ownership check then refuses
# to read it and the clone dies with "fatal: detected dubious ownership in
# repository at '/srv/praxis-repo.git'" before a single file is touched. The
# mount is placed by the orchestrator, not by anything the worker controls, so
# trusting it is safe. Measured live 2026-08-08: without this every local run
# exits 128. Kept HERE rather than in the credential-helper branch above so the
# only REPO_URL reference stays beside the clone that needs it.
if [ "${IS_LOCAL_BACKEND}" = "1" ]; then
    git config --global --add safe.directory "${REPO_URL}"
    git config --global --add safe.directory "${WORKSPACE}"
fi

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
    # -B (create-or-reset): in single-branch mode BASE_BRANCH is the default
    # branch (e.g. main), which already exists locally after clone, so a plain
    # -b would fatal "a branch named 'main' already exists".
    git checkout -B "${BASE_BRANCH}" "origin/${BASE_BRANCH}"
else
    echo "Base branch not on remote; creating from default"
    git checkout -b "${BASE_BRANCH}"
    if ! git push -u origin "${BASE_BRANCH}" 2>/dev/null; then
        echo "Push failed (branch may exist), fetching"
        git fetch origin "${BASE_BRANCH}"
        git reset --hard "origin/${BASE_BRANCH}"
    fi
fi
if [ "${REUSING_BRANCH}" = "1" ] \
    && git rev-parse --verify "origin/${BRANCH}" >/dev/null 2>&1; then
    # Reuse the existing remote branch. Required when resuming a session: the
    # restored conversation refers to edits checkpointed on this branch, so a
    # fresh branch cut from base would contradict the worker's memory.
    echo "--- Reusing existing origin/${BRANCH} ---"
    git checkout -b "${BRANCH}" "origin/${BRANCH}"
else
    echo "--- Creating branch ${BRANCH} from ${BASE_BRANCH} ---"
    git checkout -b "${BRANCH}"
fi

echo "--- Writing Static Bible to a separate instructions file (never committed) ---"
# The Bible is a compaction-proof context slot: goal + handover + conventions
# re-sent to the model every turn. We write it to its OWN file and load it via
# OpenCode's `instructions` config (combined with the repo's AGENTS.md into
# context) instead of injecting it into AGENTS.md. This means:
#   - we never touch/dereference the repo's AGENTS.md (which may be a symlink),
#   - the Bible can never leak into a PR, so no strip/restore logic is needed.
# `.git/info/exclude` is per-clone and uncommitted, so `git add -A` skips it.

# Build a runtime environment manifest so the worker knows what is available
# without guessing. Probe only tools actually on PATH.
{
    printf '%s\n' "# ENVIRONMENT (this container -- use what is already here; do NOT install your own)"
    printf '%s\n\n' "- Non-root user. NO sudo. NO apt (permission denied). Network may be restricted."
    if command -v python3 >/dev/null 2>&1; then
        printf '%s\n' "- python3: $(python3 --version 2>&1)"
    fi
    if command -v uv >/dev/null 2>&1; then
        printf '%s\n' "- uv: $(uv --version 2>&1) at $(command -v uv)"
        printf '%s\n' '  Run this repo tests with `python -m pytest` (pytest is installed system-wide).'
        printf '%s\n' '  Do NOT use `uv run pytest`: it needs a project venv a freshly cloned repo does not have.'
        printf '%s\n' '  uv is here for `uv run <tool>` on tools this image does not already provide, and installs project deps on demand -- do NOT use pip/apt/get-pip.'
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

# Append the orchestrator-supplied Bible (goal + handover + conventions) after the manifest.
if [ -n "${BIBLE_TEXT:-}" ]; then
    printf "\n%s\n" "${BIBLE_TEXT}" >> "${WORKSPACE}/.praxis-bible.md"
fi

echo ".praxis-bible.md" >> "${WORKSPACE}/.git/info/exclude"
BIBLE_INSTRUCTIONS='  "instructions": [".praxis-bible.md"],'

echo "--- Writing OpenCode config (OpenAI-compatible local provider) ---"
mkdir -p "${HOME}/.config/opencode"
# WORKER_REASONING_EFFORT is resolved by the orchestrator from the harness's
# declared effort channel (src/orchestrator/core/worker_effort.py). It is ALWAYS
# set for this harness. The reason is NOT that the omitted default is any
# particular value: it is that the omitted default is not a stable API. It
# meant maximum effort on 2026-08-15 and zero on 2026-08-21 on the same
# endpoint, so silence here is a real behaviour change that reports no error
# either way. State it explicitly and the question never arises.
#
# Key name: OpenCode's own config schema reads a per-model request option as
# camelCase "options": { "reasoningEffort": ... }, NOT snake_case
# "reasoning_effort" -- verified against https://opencode.ai/docs/models/ and
# https://github.com/anomalyco/opencode/issues/23622 (same npm package,
# @ai-sdk/openai-compatible, and the docs' own "lmstudio" worked example).
# OpenCode's transform layer converts this camelCase config key into the
# wire-level snake_case "reasoning_effort" field of the actual LM Studio HTTP
# request; that snake_case form belongs to the wire payload, not this file.
effort="${WORKER_REASONING_EFFORT:-none}"
model_opts='"options": { "reasoningEffort": "'"${effort}"'" }'
echo "Using reasoning effort: ${effort}"

# MODEL_CONTEXT_LIMIT is detected per-model from LM Studio by the orchestrator
# (never hardcoded). When present, advertise it as the model's context limit so
# OpenCode's auto-compaction triggers at the real window instead of overflowing
# into silent server-side truncation. Omitted -> OpenCode uses its own default.
model_cfg='{ "name": "'"${MODEL}"'", '"${model_opts}"' }'
if [ -n "${MODEL_CONTEXT_LIMIT:-}" ]; then
    # OpenCode's schema requires BOTH context and output in a limit block;
    # omitting output fails validation ("Missing key ...limit.output").
    model_cfg='{ "name": "'"${MODEL}"'", '"${model_opts}"', "limit": { "context": '"${MODEL_CONTEXT_LIMIT}"', "output": 8192 } }'
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

OPENCODE_BASE_ARGS=(run --model "lmstudio/${MODEL}")
OPENCODE_ARGS=("${OPENCODE_BASE_ARGS[@]}")
if [ -n "${WORKER_SESSION_ID:-}" ]; then
    echo "--- Resuming OpenCode session ${WORKER_SESSION_ID} ---"
    OPENCODE_ARGS+=(--session "${WORKER_SESSION_ID}")
fi

OUTPUT_LOG="$(mktemp)"
AGENT_LOG="${OUTPUT_LOG}"
set +e
run_agent opencode "${OPENCODE_ARGS[@]}" "${EFFECTIVE_PROMPT}"
opencode_rc=$?
set -e
if [ "${opencode_rc}" -ne 0 ] && [ -n "${WORKER_SESSION_ID:-}" ]; then
    # A stale or pruned session id must not fail the task. Retry once cold,
    # reusing the same base args (never a second hand-copied literal) so a
    # future flag added to OPENCODE_BASE_ARGS can't be forgotten here.
    echo "WARNING: resume with session ${WORKER_SESSION_ID} failed; retrying cold"
    set +e
    run_agent opencode "${OPENCODE_BASE_ARGS[@]}" "${EFFECTIVE_PROMPT}"
    opencode_rc=$?
    set -e
fi
if [ "${opencode_rc}" -ne 0 ]; then
    exit "${opencode_rc}"
fi

echo "--- Capturing OpenCode session id (best effort) ---"
if CAPTURED_SESSION_ID=$(opencode session list --format json 2>/dev/null \
    | python3 /usr/local/bin/extract_session.py 2>/dev/null); then
    echo "Session id: ${CAPTURED_SESSION_ID:-<none>}"
else
    CAPTURED_SESSION_ID=""
    echo "No session id captured; next dispatch will start cold"
fi

report_status=$(grep -oE '^Status:[[:space:]]*[A-Z_]+' "${OUTPUT_LOG}" \
    | tail -n1 | sed -E 's/^Status:[[:space:]]*//' ) || true

if [ "${report_status}" = "BLOCKED" ] || [ "${report_status}" = "NEEDS_CONTEXT" ]; then
    echo "--- Worker reported ${report_status}; checkpointing WIP (no PR) ---"
    QUESTION=$(awk '/^Concerns/{flag=1;next}/^====/{flag=0}flag' "${OUTPUT_LOG}" \
        | sed '/^[[:space:]]*$/d')
    [ -z "${QUESTION}" ] && QUESTION="Worker reported ${report_status} without details."

    # Checkpoint so the resumed worker's tree matches its restored memory.
    # .praxis-bible.md is in .git/info/exclude, so `git add -A` cannot stage it.
    # Every git step below is guarded as an `if` condition (never a bare
    # statement) so a failure here cannot trip `set -e` and skip send_callback;
    # it only flips checkpoint_ok, which blanks the session id instead.
    checkpoint_ok=1
    if ! git add -A; then
        echo "WARNING: checkpoint git add failed; suppressing session resume"
        checkpoint_ok=0
    fi
    if [ "${checkpoint_ok}" -eq 1 ] && ! git diff --cached --quiet; then
        if ! git commit -m "wip: checkpoint before clarification (${BRANCH})"; then
            echo "WARNING: checkpoint commit failed; suppressing session resume"
            checkpoint_ok=0
        fi
    fi
    ahead=0
    if [ "${checkpoint_ok}" -eq 1 ]; then
        # The same numeric validation the commit block below already applies,
        # and for the same reason. `git rev-list --count` can exit 0 having
        # printed nothing or something non-numeric, and `[ "" -gt 0 ]` is not
        # false, it is an ERROR (`integer expression expected`) that bash then
        # reads as false. That skipped the push while leaving checkpoint_ok at
        # 1, so a session id was reported for a checkpoint that never reached
        # the remote: the next turn resumes the model's memory of edits that
        # only ever existed in a destroyed container, against a branch rebuilt
        # from base. That divergence is exactly what the invariant below is
        # written to prevent, and this was the hole it was falling through.
        if checkpoint_ahead=$(git rev-list --count "${BASE_BRANCH}..HEAD" 2>&1); then
            case "${checkpoint_ahead}" in
                ""|*[!0-9]*)
                    echo "WARNING: checkpoint rev-list returned non-numeric output; suppressing session resume"
                    checkpoint_ok=0
                    ;;
                *)
                    ahead="${checkpoint_ahead}"
                    ;;
            esac
        else
            echo "WARNING: checkpoint rev-list failed; suppressing session resume"
            checkpoint_ok=0
            ahead=0
        fi
    fi
    if [ "${checkpoint_ok}" -eq 1 ] && [ "${ahead}" -gt 0 ]; then
        if ! git push -u origin "${BRANCH}"; then
            echo "WARNING: checkpoint push failed; suppressing session resume"
            checkpoint_ok=0
        fi
    fi
    # The invariant: only report a session id once its checkpoint is on the
    # remote. Otherwise the next turn must start cold and rebuild from base.
    if [ "${checkpoint_ok}" -ne 1 ]; then
        CAPTURED_SESSION_ID=""
    fi

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
    # failure when the branch also has no commits beyond the base. Guarded
    # (never a bare assignment) so a `rev-list` failure cannot trip `set -e`
    # and abort the script before a single diagnostic line prints -- proved
    # by execution: the bare form exits 128 here with nothing printed at all.
    # An undeterminable count is treated as "no commits ahead" so the run
    # still falls into the diagnostic path below instead of vanishing, and
    # the rev-list failure itself (rc + stderr) is folded into that diagnostic.
    #
    # `2>&1` merges stderr into the captured value, so exit 0 alone does not
    # prove `rev_list_out` is a clean count: a stray advisory line on stderr
    # would land right here too. Trusting that blindly with `-gt` would not
    # merely misreport -- `[ "3\nwarning: ..." -gt 0 ]` is not a numeric
    # comparison at all, bash exits 2 ("integer expression expected"), the
    # `if` reads that as FALSE, and a worker's real commits get reported as
    # no changes: the exact misclassification this diagnostic exists to
    # prevent, reintroduced by the guard meant to fix it. So the captured
    # value is validated as PURELY numeric before it is trusted as a count;
    # anything else (empty, or containing so much as one non-digit anywhere,
    # newline included) is treated exactly like a rev-list failure rather
    # than parsed or stripped, on the same "explain, don't guess" principle
    # as the rest of this block.
    ahead=0
    rev_list_error=""
    rev_list_rc=""
    if rev_list_out=$(git rev-list --count "${BASE_BRANCH}..HEAD" 2>&1); then
        case "${rev_list_out}" in
            ""|*[!0-9]*)
                rev_list_error="rev-list exited 0 but returned non-numeric output: ${rev_list_out}"
                echo "WARNING: git rev-list returned non-numeric output; treating as 0 commits ahead of ${BASE_BRANCH}"
                ;;
            *)
                ahead="${rev_list_out}"
                ;;
        esac
    else
        rev_list_rc=$?
        rev_list_error="${rev_list_out}"
        echo "WARNING: git rev-list failed (rc=${rev_list_rc}) counting commits ahead of ${BASE_BRANCH}; treating as 0"
    fi
    if [ "${ahead}" -gt 0 ]; then
        echo "Worker committed its own work (${ahead} commit(s) ahead of ${BASE_BRANCH})"
    else
        # Explain the run instead of merely asserting it. This container is
        # destroyed seconds from now, so anything not printed here is gone
        # for good, including whether the harness said something, said
        # nothing, or refused.
        #
        # harness_rc is ALWAYS 0 here: a non-zero harness rc exits far above,
        # long before the commit block. Printing it is still correct and
        # deliberate. It is what separates "exited cleanly and produced
        # nothing" from every other shape, and it stops a future reader from
        # assuming the code was swallowed by the tee. Do NOT delete it as dead
        # output.
        output_bytes="$(wc -c < "${OUTPUT_LOG}" 2>/dev/null | tr -d '[:space:]')" || output_bytes=""
        [ -n "${output_bytes}" ] || output_bytes=0
        echo "No changes produced by OpenCode"
        echo "  harness_rc=${opencode_rc}"
        echo "  output_log_bytes=${output_bytes}"
        echo "  report_status=${report_status:-none}"
        if [ -n "${rev_list_error}" ]; then
            echo "  rev_list_failed=true rc=${rev_list_rc:-n/a}"
            echo "  rev_list_stderr: ${rev_list_error}"
        fi
        echo "--- last 30 lines of harness output ---"
        tail -n 30 "${OUTPUT_LOG}" 2>/dev/null || echo "  (no harness output captured)"
        echo "--- end of harness output ---"

        # An empty diff is a FACT, not a verdict. Two different runs produce
        # it and they are not the same event:
        #
        #  - The harness ran, worked, and found the tree already satisfied
        #    the task. That is a no-op. Reporting it as `failed` re-dispatched
        #    the task up to three times to the identical correct answer and
        #    then failed the whole plan, with the repo already in the state
        #    the spec asked for. Measured in 4 of 4 plans across BOTH
        #    harnesses; the survivors got lucky, not correct.
        #  - The harness produced literally no output at all. Nothing ran.
        #    That is a broken run and must stay a failure, or a dead worker
        #    silently closes every leaf it touches.
        #
        # The orchestrator makes the governance call (it runs the project's
        # verify command against the base branch before accepting a no-op);
        # this only reports which of the two shapes happened.
        # rev_list_error is part of the condition, not just the diagnostic.
        # When the commit count could not be trusted we do NOT know that the
        # worker produced nothing; it may have committed work this run cannot
        # see. Closing the leaf then would discard real, unpushed work. An
        # undeterminable count stays a failure, which is the conservative
        # direction and the behaviour that shipped before.
        if [ "${output_bytes}" -gt 0 ] && [ -z "${rev_list_error}" ]; then
            echo "Reporting no_changes: the harness ran and the tree already satisfied this task."
            echo "The orchestrator decides whether that is a no-op; it verifies ${BASE_BRANCH} first."
            STATUS="no_changes"
            send_callback
            trap - EXIT
            exit 0
        fi
        if [ -n "${rev_list_error}" ]; then
            echo "Commit count was untrustworthy, so no-op cannot be established; failing."
        else
            echo "Harness produced no output at all; this is a failed run, not a no-op."
        fi
        STATUS="failed"
        exit 1
    fi
else
    git commit -m "agent: ${BRANCH}"
fi

echo "--- Pushing branch ---"
# Force is correct only when the branch was just rebuilt fresh from base this
# attempt, which makes it authoritative over whatever the remote holds. Both
# single-branch mode and a resume turn instead REUSE the existing remote
# branch (checked out above), so a force push there would silently discard
# commits this run does not know about -- its own earlier checkpoint, or
# someone else's. Non-force in both reuse cases; force only for a genuine
# from-base rebuild retry.
if [ "${REUSING_BRANCH}" = "1" ]; then
    git push -u origin "${BRANCH}"
else
    git push -u --force origin "${BRANCH}"
fi

if [ "${IS_LOCAL_BACKEND}" = "1" ]; then
    # No PR objects exist in local mode; the orchestrator reviews the branch
    # against its base directly. Report the same (branch, base) pair it will
    # parse back out of tasks.pr_url. Every gh call, the reuse lookup included,
    # is inside the else: gh has no credential and no remote in local mode.
    PR_URL="praxis-local://pr?branch=$(url_encode "${BRANCH}")&base=$(url_encode "${BASE_BRANCH}")"
    echo "Local backend: reporting ${PR_URL}"
else
# The block below is deliberately NOT re-indented: the --body value is a
# multi-line string literal, so indenting its continuation lines would change
# the PR body text. Left byte-identical so GitHub mode is provably unchanged.
echo "--- Creating PR ---"
# A previous attempt may already have opened a PR for this branch; reuse it.
# ONLY an OPEN one, and only for this exact (head, base) pair. The old lookup
# was `gh pr view "${BRANCH}"`, which resolves a branch to a PR REGARDLESS of
# state; two plans built from the same spec text share slugs, so it handed back
# the previous plan's already-MERGED PR. This run's real commit was then
# attached to a diff that had already landed: the review fetched the old merged
# diff and passed, `merge_pr` saw a merged PR and treated that as success, the
# task was marked MERGED, and the work never reached the base branch, with every
# layer reporting success.
#
# This is the same positive open-state lookup `_existing_integration_pr` makes
# in core/orchestrator_review.py, for the same reason stated there: only a
# POSITIVE open hit may skip creation. Any other answer, including a failure of
# the lookup itself, falls through to creation, because treating a failure as
# "already open" would hide a real gh error forever.
#
# The emptiness of the OUTPUT is the signal, never the exit status: `gh pr list`
# prints nothing and still exits 0 when nothing matches.
PR_URL=$(gh pr list --head "${BRANCH}" --base "${BASE_BRANCH}" --state open --json url --jq '.[0].url // empty' 2>/dev/null || true)
if [ -n "${PR_URL}" ]; then
    echo "Reusing existing open PR: ${PR_URL}"
else
PR_URL=$(gh pr create \
    --title "agent: ${BRANCH}" \
    --body "Task: ${TASK_SUMMARY:-${BRANCH}}

---
Implemented by \`${MODEL}\` (harness: opencode)" \
    --base "${BASE_BRANCH}" \
    --head "${BRANCH}")
# `gh pr create` can exit 0 having printed NOTHING. The old code echoed
# "PR created: " with an empty url and then ran to a normal exit, so the
# callback said `completed` with `pr_url: null` and the log an operator reads
# through `praxis logs` said the PR existed and the agent had finished. The
# orchestrator's `completed_without_pr` guard converts that to a failure, so
# the dashboard was right and the log was wrong about the same run. The
# entrypoint reports FACTS; a success it did not observe is not one of them.
# The EXIT trap turns this into a `failed` callback, so the task is not left
# hanging for the reconcile sweep either.
if [ -z "${PR_URL}" ]; then
    echo "ERROR: gh pr create exited 0 but printed no PR URL"
    exit 1
fi
echo "PR created: ${PR_URL}"
fi
fi
echo "=== OpenCode agent completed ==="
