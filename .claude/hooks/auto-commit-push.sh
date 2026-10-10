#!/usr/bin/env bash
# Claude Code "Stop" hook: commit and push finished work, safely.
#
# Contract (Claude Code Stop hook):
#   - the event JSON arrives on stdin;
#   - exit 0         -> nothing to do, or done; stdout may carry a
#                       {"systemMessage": "..."} line shown to the user;
#   - exit 2         -> blocking: stderr is handed back to Claude to fix.
#
# What it will NOT do, ever: force-push, amend, rewrite history, set commit
# dates, create empty commits, or commit secrets / generated files.
#
# Opt-outs (any one disables it):
#   touch .claude/auto-commit.disabled        (local, git-ignored)
#   export AUTO_COMMIT_DISABLED=1
#
# Tunables (environment):
#   AUTO_COMMIT_ALLOW_MAIN=1   allow committing on main/master (default: refuse)
#   AUTO_COMMIT_LINT_CMD       override the lint command
#   AUTO_COMMIT_TEST_CMD       override the test command
#   CODECREW_PYTHON            interpreter that has pytest and ruff
#   CODECREW_VENV              venv directory (default ~/.codecrew/venv)
#
# A good commit message comes from the agent: write a conventional-commit
# subject to .claude/commit-message.txt before stopping. Without it the hook
# falls back to a generic, truthful "update N files" message.

set -u

PROJECT_DIR="${CLAUDE_PROJECT_DIR:-}"
if [ -z "$PROJECT_DIR" ]; then
  PROJECT_DIR="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
fi
cd "$PROJECT_DIR" 2>/dev/null || exit 0

LOG_FILE=".claude/hooks/auto-commit.log"
MSG_FILE=".claude/commit-message.txt"
DISABLE_FILE=".claude/auto-commit.disabled"

log()  { mkdir -p "$(dirname "$LOG_FILE")" 2>/dev/null
         printf '%s  %s\n' "$(date '+%F %T')" "$*" >> "$LOG_FILE" 2>/dev/null || true; }
note() { printf '[auto-commit] %s\n' "$*" >&2; }
fail() { log "BLOCKED: $*"; note "$*"; exit 2; }
say()  { # a message for the user that does not block
  local m="$1"
  m="${m//\\/\\\\}"; m="${m//\"/\\\"}"; m="${m//$'\n'/\\n}"; m="${m//$'\r'/}"
  printf '{"systemMessage": "%s"}\n' "$m"
}

# -- 0. kill switches ---------------------------------------------------------
if [ -f "$DISABLE_FILE" ]; then log "disabled by $DISABLE_FILE"; exit 0; fi
if [ "${AUTO_COMMIT_DISABLED:-0}" = "1" ]; then log "disabled by env"; exit 0; fi

# -- 1. read the event; never re-enter our own continuation -------------------
INPUT=""
if [ ! -t 0 ]; then INPUT="$(cat 2>/dev/null || true)"; fi
if printf '%s' "$INPUT" | grep -Eq '"stop_hook_active"[[:space:]]*:[[:space:]]*true'; then
  log "stop_hook_active=true: skipping to avoid a loop"
  exit 0
fi

# -- 2. is there anything to do? ---------------------------------------------
git rev-parse --is-inside-work-tree >/dev/null 2>&1 || exit 0
if [ -z "$(git status --porcelain 2>/dev/null)" ]; then
  log "working tree clean"
  exit 0
fi

# -- 3. branch policy ---------------------------------------------------------
BRANCH="$(git branch --show-current 2>/dev/null)"
[ -n "$BRANCH" ] || fail "Detached HEAD. Switch to a branch before committing."
case "$BRANCH" in
  main|master)
    if [ "${AUTO_COMMIT_ALLOW_MAIN:-0}" != "1" ]; then
      fail "You are on '$BRANCH'. Create a feature branch first (git switch -c feat/<topic>), then stop again. (Set AUTO_COMMIT_ALLOW_MAIN=1 to allow committing on $BRANCH.)"
    fi
    ;;
esac

# -- 4. refuse secrets and generated files ------------------------------------
changed_files() {
  {
    git diff --name-only HEAD 2>/dev/null
    git diff --name-only --cached 2>/dev/null
    git ls-files --others --exclude-standard 2>/dev/null
  } | sort -u
}

forbidden_reason() {
  local p="$1" base lower
  base="${p##*/}"
  lower="$(printf '%s' "$base" | tr 'A-Z' 'a-z')"
  case "$lower" in
    .env.example|.env.sample|.env.template|.env.dist) ;;        # templates are fine
    .env|.env.*)                       echo "environment file"; return ;;
    *.pem|*.key|*.p12|*.pfx|*.jks|*.keystore|*.kdbx)
                                       echo "key or credential material"; return ;;
    id_rsa|id_rsa.*|id_dsa|id_ecdsa|id_ed25519|id_ed25519.*)
                                       echo "SSH private key"; return ;;
    credentials|credentials.*|secrets|secrets.*|secret.*|.netrc|.npmrc|.pypirc)
                                       echo "credentials file"; return ;;
  esac
  case "/$p" in
    */node_modules/*|*/.next/*|*/dist/*|*/build/*|*/__pycache__/*|*/.venv/*|*/venv/*|*/.codecrew/*|*/bench_runs/*|*/.hypothesis/*|*/.mypy_cache/*|*/.ruff_cache/*|*/.pytest_cache/*)
      echo "build output, dependency or generated directory"; return ;;
  esac
  case "$lower" in
    *.pyc|*.pyo|*.log) echo "generated or local-state file"; return ;;
  esac
}

# Well-known credential shapes. Matching on content catches a key pasted into
# a source file, which a filename check cannot.
SECRET_RE='gsk_[A-Za-z0-9]{20,}|sk_live_[A-Za-z0-9]{10,}|sk-ant-[A-Za-z0-9_-]{20,}|sk-proj-[A-Za-z0-9_-]{20,}|AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|AIza[0-9A-Za-z_-]{35}|xox[baprs]-[A-Za-z0-9-]{10,}|-----BEGIN [A-Z ]*PRIVATE KEY-----'

guard_files() {
  local f why size
  while IFS= read -r f; do
    [ -n "$f" ] || continue
    why="$(forbidden_reason "$f")"
    if [ -n "$why" ]; then
      fail "Refusing to commit '$f' ($why). Remove it, add it to .gitignore, or unstage it."
    fi
    [ -f "$f" ] || continue                      # deleted files have no content
    size="$(wc -c < "$f" 2>/dev/null || echo 0)"
    [ "${size:-0}" -gt 2097152 ] && continue     # skip very large files
    if grep -Iq -E "$SECRET_RE" "$f" 2>/dev/null; then
      fail "Refusing to commit '$f': it contains something shaped like an API key or private key. Remove the secret (and rotate it if it was real)."
    fi
  done
}

# Process substitution, not a pipe: a function on the right of a pipe runs in
# a subshell, where its `exit` would not stop this script and the guard
# would silently do nothing.
guard_files < <(changed_files)

# -- 5. find an interpreter and define the checks -----------------------------
find_python() {
  local venv="${CODECREW_VENV:-$HOME/.codecrew/venv}" c
  for c in "${CODECREW_PYTHON:-}" "$venv/Scripts/python.exe" "$venv/bin/python"; do
    if [ -n "$c" ] && [ -x "$c" ]; then printf '%s' "$c"; return; fi
  done
  command -v python3 2>/dev/null || command -v python 2>/dev/null || true
}
PY="$(find_python)"

if [ -n "${AUTO_COMMIT_LINT_CMD:-}" ]; then
  LINT_CMD="$AUTO_COMMIT_LINT_CMD"
else
  # The same definition of "a real defect" the project's own G1 gate uses:
  # syntax errors, undefined names, broken comparisons. Pure style rules are
  # reported by `ruff check` but must not stop a commit.
  LINT_CMD="\"$PY\" -m ruff check codecrew tests --select E9,F63,F7,F82"
fi
if [ -n "${AUTO_COMMIT_TEST_CMD:-}" ]; then
  TEST_CMD="$AUTO_COMMIT_TEST_CMD"
else
  # Fast subset (seconds). The full suite, including the slow end-to-end
  # gate tests, is `python -m pytest tests`.
  TEST_CMD="\"$PY\" -m pytest tests -q -x -m \"not slow\" -p no:cacheprovider --no-header"
fi
COMPILE_CMD="\"$PY\" -m compileall -q codecrew tests"

if [ -z "$PY" ] && [ -z "${AUTO_COMMIT_TEST_CMD:-}" ]; then
  fail "No Python interpreter found, so the checks cannot run and nothing will be committed. Set CODECREW_PYTHON."
fi

run_check() {  # run_check <name> <command string>
  local name="$1" cmd="$2" out
  out="$(mktemp 2>/dev/null || echo ".claude/hooks/.check.out")"
  note "running ${name} ..."
  if ! bash -c "$cmd" > "$out" 2>&1; then
    note "${name} FAILED:"
    tail -n 40 "$out" >&2
    rm -f "$out"
    log "BLOCKED: check failed: $name"
    exit 2
  fi
  rm -f "$out"
}

# -- 6. checks, before anything is committed ----------------------------------
if [ -z "${AUTO_COMMIT_LINT_CMD:-}" ] && [ -z "${AUTO_COMMIT_TEST_CMD:-}" ]; then
  run_check "compile" "$COMPILE_CMD"
fi
if [ -n "${AUTO_COMMIT_LINT_CMD:-}" ] || "$PY" -c "import ruff" >/dev/null 2>&1 \
   || "$PY" -m ruff --version >/dev/null 2>&1; then
  run_check "lint" "$LINT_CMD"
else
  note "ruff is not installed for $PY; lint skipped."
fi
run_check "tests" "$TEST_CMD"

# -- 7. stage, then re-check exactly what is staged ---------------------------
git add -A >/dev/null 2>&1 || fail "git add failed."
guard_files < <(git diff --cached --name-only)
if git diff --cached --quiet; then
  log "nothing staged after add; no commit"
  exit 0
fi

# -- 8. the commit message ----------------------------------------------------
CONVENTIONAL='^(feat|fix|docs|style|refactor|perf|test|build|ci|chore|revert)(\([A-Za-z0-9._/-]+\))?!?: .{3,}'
TMP_MSG="$(mktemp 2>/dev/null || echo ".claude/hooks/.msg.tmp")"
FROM_FILE=0

if [ -s "$MSG_FILE" ]; then
  # Never carry an AI co-author trailer into the history.
  grep -Eiv '^co-authored-by:.*(claude|anthropic)' "$MSG_FILE" > "$TMP_MSG" || true
  FIRST="$(head -n1 "$TMP_MSG")"
  if ! printf '%s' "$FIRST" | grep -Eq "$CONVENTIONAL"; then
    rm -f "$TMP_MSG"
    git reset -q >/dev/null 2>&1 || true
    fail "$MSG_FILE does not start with a conventional commit subject (type(scope): summary). Fix it and stop again. Got: $FIRST"
  fi
  if [ "${#FIRST}" -gt 100 ]; then
    rm -f "$TMP_MSG"
    git reset -q >/dev/null 2>&1 || true
    fail "The commit subject in $MSG_FILE is over 100 characters. Shorten it and stop again."
  fi
  FROM_FILE=1
else
  NAMES="$(git diff --cached --name-status)"
  COUNT="$(printf '%s\n' "$NAMES" | wc -l | tr -d ' ')"
  ALL_PATHS="$(git diff --cached --name-only)"
  TYPE="chore"
  if ! printf '%s\n' "$ALL_PATHS" | grep -Evq '(\.md$|^docs/)'; then
    TYPE="docs"
  elif ! printf '%s\n' "$ALL_PATHS" | grep -Evq '^tests/'; then
    TYPE="test"
  elif printf '%s\n' "$NAMES" | grep -Eq '^A[[:space:]]+codecrew/'; then
    TYPE="feat"
  fi
  DIRS="$(printf '%s\n' "$ALL_PATHS" | awk -F/ '{ if (NF>1) print $1"/"; else print $1 }' \
          | sort -u | head -n 3 | paste -sd, - | sed 's/,/, /g')"
  {
    printf '%s: update %s file(s) in %s\n\n' "$TYPE" "$COUNT" "$DIRS"
    printf 'Changed files:\n'
    printf '%s\n' "$NAMES" | head -n 15 | sed 's/^/  /'
    [ "$COUNT" -gt 15 ] && printf '  ... and %s more\n' "$((COUNT - 15))"
  } > "$TMP_MSG"
fi

# -- 9. commit (no --no-verify, no --amend, no date overrides) ----------------
if ! git config user.email >/dev/null 2>&1 || ! git config user.name >/dev/null 2>&1; then
  rm -f "$TMP_MSG"
  fail "git user.name / user.email are not configured; refusing to commit under an unknown identity."
fi
if ! COMMIT_OUT="$(git commit -q -F "$TMP_MSG" 2>&1)"; then
  rm -f "$TMP_MSG"
  fail "git commit failed: $COMMIT_OUT"
fi
rm -f "$TMP_MSG"
[ "$FROM_FILE" -eq 1 ] && rm -f "$MSG_FILE"
SHA="$(git rev-parse --short HEAD)"
SUBJECT="$(git log -1 --format=%s)"
log "committed $SHA on $BRANCH: $SUBJECT"

# -- 10. push to the current branch -------------------------------------------
if ! git remote get-url origin >/dev/null 2>&1; then
  say "Committed $SHA on $BRANCH ($SUBJECT). No 'origin' remote, so nothing was pushed."
  exit 0
fi
export GIT_TERMINAL_PROMPT=0
if PUSH_OUT="$(git push origin HEAD 2>&1)"; then
  log "pushed $SHA to origin/$BRANCH"
  say "Committed and pushed $SHA to origin/$BRANCH: $SUBJECT"
else
  log "push failed after commit $SHA: $PUSH_OUT"
  # The commit is safe locally. A push failure is almost always credentials,
  # which Claude cannot fix, so this does not block or loop.
  say "Committed $SHA on $BRANCH ($SUBJECT) but the push FAILED, so it is only local. Run 'git push origin HEAD' yourself. Reason: $(printf '%s' "$PUSH_OUT" | tail -n 2 | tr '\n' ' ')"
fi
exit 0
