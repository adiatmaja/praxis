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
TOKENS_USED=""

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
            STATUS="failed"
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

    local session_json="null"
    if [ -n "${CAPTURED_SESSION_ID:-}" ]; then
        session_json=$(printf "%s" "${CAPTURED_SESSION_ID}" | json_escape)
    fi

    # tokens_used is NUMERIC in the payload, never quoted, and must be the bare
    # word null (not "") when absent -- the callback schema treats a missing
    # count as "this harness cannot report", not as zero. Guarded against a
    # non-numeric capture so a malformed envelope can never emit invalid JSON.
    local tokens_json="null"
    if [ -n "${TOKENS_USED:-}" ]; then
        case "${TOKENS_USED}" in
            ''|*[!0-9]*) tokens_json="null" ;;
            *) tokens_json="${TOKENS_USED}" ;;
        esac
    fi

    local payload="{\"task_id\":\"${TASK_ID}\",\"run_id\":${run_json},\"status\":\"${STATUS}\",\"pr_url\":${pr_json},\"question\":${question_json},\"session_id\":${session_json},\"tokens_used\":${tokens_json}}"
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
    # Reuse the existing remote branch. Required when resuming a conversation:
    # the restored context refers to edits checkpointed on this branch.
    echo "--- Reusing existing origin/${BRANCH} ---"
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
#
# UNVERIFIED: --output-format json and --conversation are not confirmed against a
# real agy build (no image/creds available in this environment). extract_session.py
# tries several plausible envelope keys and fails closed (exit 1, no stdout) on any
# shape it does not recognize, so the fallback branch below always keeps the worker
# usable even if these flags are wrong or the JSON envelope has a different shape.
# There is also a known upstream bug (antigravity-cli#76) where --print emits nothing
# on stdout when stdout is not a TTY; if that also affects --output-format json, the
# fallback path (RAW_LOG copied straight to OUTPUT_LOG) is what keeps this working.

AGY_BASE_ARGS=(--dangerously-skip-permissions --mode accept-edits --print-timeout 30m
               --output-format json --model "${MODEL}")
AGY_ARGS=("${AGY_BASE_ARGS[@]}")
if [ -n "${WORKER_SESSION_ID:-}" ]; then
    echo "--- Resuming agy conversation ${WORKER_SESSION_ID} ---"
    AGY_ARGS+=(--conversation "${WORKER_SESSION_ID}")
fi

# NOTE: piped through `tee` (not a plain `>` redirect) so container log streaming
# behaves exactly as it did before this feature: the orchestrator tails container
# logs live, and buffering the whole run into a file before printing it would delay
# visibility for the entire task duration. RAW_LOG still captures everything for the
# JSON-envelope split below.
RAW_LOG="$(mktemp)"
OUTPUT_LOG="$(mktemp)"
set +e
agy "${AGY_ARGS[@]}" -p "${EFFECTIVE_PROMPT}" 2>&1 | tee "${RAW_LOG}"
agy_rc="${PIPESTATUS[0]}"
set -e
if [ "${agy_rc}" -ne 0 ] && [ -n "${WORKER_SESSION_ID:-}" ]; then
    # A stale or pruned conversation id must not fail the task. Retry once cold.
    echo "WARNING: resume with conversation ${WORKER_SESSION_ID} failed; retrying cold"
    AGY_ARGS=("${AGY_BASE_ARGS[@]}")
    set +e
    agy "${AGY_ARGS[@]}" -p "${EFFECTIVE_PROMPT}" 2>&1 | tee "${RAW_LOG}"
    agy_rc="${PIPESTATUS[0]}"
    set -e
fi
if [ "${agy_rc}" -ne 0 ]; then
    exit "${agy_rc}"
fi

echo "--- Splitting agy JSON envelope (best effort) ---"
if SPLIT=$(python3 /usr/local/bin/extract_session.py < "${RAW_LOG}" 2>/dev/null); then
    CAPTURED_SESSION_ID=$(printf '%s' "${SPLIT}" | head -n1)
    printf '%s' "${SPLIT}" | tail -n +2 > "${OUTPUT_LOG}"
    echo "Conversation id: ${CAPTURED_SESSION_ID:-<none>}"
else
    # Envelope unparseable: fall back to treating raw output as the transcript,
    # exactly as before this feature existed.
    CAPTURED_SESSION_ID=""
    cp "${RAW_LOG}" "${OUTPUT_LOG}"
    echo "Envelope unparseable; continuing without conversation id"
fi

# agy's JSON envelope carries a sibling "usage" object with total_tokens.
# Observed live against a real agy build on 2026-08-14 (two dispatched tasks,
# concrete non-zero counts read back from this exact field); the entrypoint's
# own envelope-shape comment further down was added from that same run. NOTE:
# docs/gotchas.md still marks the envelope UNVERIFIED as of this commit --
# that entry predates the 2026-08-14 run and was never updated; it needs a
# doc fix, tracked separately, not a reason to distrust the observed shape.
# Read from RAW_LOG, not OUTPUT_LOG: the split above strips the envelope.
# Guarded twice, like the envelope_info read further below, so a
# missing/unparseable field can never trip `set -e`; TOKENS_USED just stays
# empty and the callback reports null.
tokens_raw="$(python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    d = None
if isinstance(d, dict):
    usage = d.get("usage")
    if isinstance(usage, dict):
        total = usage.get("total_tokens")
        if total is not None:
            print(total)
' < "${RAW_LOG}" 2>/dev/null)" || tokens_raw=""
case "${tokens_raw}" in
    ''|*[!0-9]*) TOKENS_USED="" ;;
    *) TOKENS_USED="${tokens_raw}" ;;
esac
echo "Token usage: ${TOKENS_USED:-<not reported>}"

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
    # statement) so a failure here cannot trip `set -e` and skip send_callback
    # with STATUS still needs_clarification; it only flips checkpoint_ok,
    # which blanks the session id instead.
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
    if [ "${checkpoint_ok}" -eq 1 ] \
        && ! ahead=$(git rev-list --count "${BASE_BRANCH}..HEAD"); then
        echo "WARNING: checkpoint rev-list failed; suppressing session resume"
        checkpoint_ok=0
        ahead=0
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

echo "--- Committing changes (agy does not auto-commit) ---"
# .praxis-bible.md is excluded via .git/info/exclude, so git add -A never
# stages it and it can never appear in the PR.
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
        # nothing, or refused. This evidence does NOT currently reach the
        # orchestrator's data model: a no-change run reports STATUS=failed to
        # /api/internal/agent-done, which routes a non-"completed" status
        # straight to the retry/fail branch (api/internal.py) without ever
        # moving the task to REVIEWING, so orchestrator_review.review_task --
        # the only place a FailureClass is assigned and the only caller of
        # record_outcome -- never runs for this run, and no task_outcomes row
        # or failure_class is ever produced for it. Printing this here is
        # still worth doing: it is the only place this evidence exists at
        # all, the container log that reaches agent_runs.logs. Nobody should
        # build on the claim that it reaches failure_taxonomy; it does not,
        # yet.
        #
        # harness_rc is ALWAYS 0 here: a non-zero harness rc exits far above,
        # long before the commit block. Printing it is still correct and
        # deliberate. It is what separates "exited cleanly and produced
        # nothing" from every other shape, and it stops a future reader from
        # assuming the code was swallowed by the tee. Do NOT delete it as dead
        # output.
        output_bytes="$(wc -c < "${OUTPUT_LOG}" 2>/dev/null | tr -d '[:space:]')" || output_bytes=""
        [ -n "${output_bytes}" ] || output_bytes=0
        # agy alone has a JSON envelope: {conversation_id, status, response,
        # duration_seconds, num_turns, usage}. status and num_turns are the two
        # fields that say whether the model actually took any turns, which is
        # the difference between "refused immediately" and "worked and produced
        # nothing". Read from RAW_LOG, not OUTPUT_LOG: the split above strips
        # the envelope. Read on STDIN so no path is ever passed as an argument.
        # The whole thing is guarded twice -- python never raises, and the
        # substitution falls back -- because the fallback path above copies
        # RAW_LOG over verbatim and there may be no JSON here at all; tripping
        # set -e would skip the tail below AND the exit 1.
        envelope_info="$(python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    d = None
if isinstance(d, dict):
    st = d.get("status") or "none"
    nt = d.get("num_turns")
    nt = "unknown" if nt is None else nt
else:
    st, nt = "unparseable", "unknown"
print("  envelope_status=%s" % st)
print("  envelope_num_turns=%s" % nt)
' < "${RAW_LOG}" 2>/dev/null)" || envelope_info=""
        if [ -z "${envelope_info}" ]; then
            envelope_info="  envelope_status=unavailable
  envelope_num_turns=unknown"
        fi
        echo "No changes produced by agy"
        echo "  harness_rc=${agy_rc}"
        echo "  output_log_bytes=${output_bytes}"
        echo "  report_status=${report_status:-none}"
        if [ -n "${rev_list_error}" ]; then
            echo "  rev_list_failed=true rc=${rev_list_rc:-n/a}"
            echo "  rev_list_stderr: ${rev_list_error}"
        fi
        echo "${envelope_info}"
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
        # Kept byte-for-byte equivalent to the opencode entrypoint's branch on
        # purpose: this defect was reported against agy and is not
        # agy-specific. Arm B escaped it in run #5 only because its worker
        # happened to add 8 lines to an already-complete test file. The
        # envelope fields are printed above as evidence but deliberately NOT
        # used as the condition; gating on envelope_status is tracked
        # separately and must not be smuggled in here.
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
fi
echo "=== agy agent completed ==="
