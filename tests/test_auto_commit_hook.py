"""The Stop hook that commits and pushes finished work.

It runs real git against throwaway repositories with a local bare repo as
"origin", so nothing here can reach GitHub. What is asserted is the safety
contract: it must refuse the unsafe cases, never loop, never rewrite
history, and leave the working tree alone when it refuses.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / ".claude" / "hooks" / "auto-commit-push.sh"
IDENTITY = ("ROOKIEEE12", "168962806+ROOKIEEE12@users.noreply.github.com")


def _find_bash() -> str | None:
    candidates = [
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files (x86)\Git\bin\bash.exe",
        shutil.which("bash"),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            probe = subprocess.run([candidate, "-c", "echo $BASH_VERSION"],
                                   capture_output=True, text=True)
            if probe.returncode == 0 and probe.stdout.strip():
                return candidate
    return None


BASH = _find_bash()
# Each test spawns bash and several git processes, which is slow on Windows.
# They are part of the full suite, not of the fast subset the hook itself
# runs before every commit (that would make every stop cost minutes).
pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(BASH is None, reason="bash is not available"),
]


def git(cwd: Path, *args: str) -> str:
    done = subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                          text=True, encoding="utf-8")
    assert done.returncode == 0, f"git {' '.join(args)}: {done.stderr}"
    return done.stdout.strip()


def commits(cwd: Path) -> int:
    return int(git(cwd, "rev-list", "--count", "HEAD"))


@pytest.fixture
def repo(tmp_path: Path):
    """A working repo on a feature branch, with a local bare 'origin'."""
    remote = tmp_path / "origin.git"
    work = tmp_path / "work"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    work.mkdir()
    git(work, "init", "-q", "-b", "main")
    git(work, "config", "user.name", IDENTITY[0])
    git(work, "config", "user.email", IDENTITY[1])
    (work / ".claude" / "hooks").mkdir(parents=True)
    shutil.copy(HOOK, work / ".claude" / "hooks" / "auto-commit-push.sh")
    (work / ".gitignore").write_text(
        ".claude/commit-message.txt\n.claude/hooks/auto-commit.log\n"
        ".claude/auto-commit.disabled\n", encoding="utf-8")
    (work / "app.py").write_text("x = 1\n", encoding="utf-8")
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", "chore: initial")
    git(work, "remote", "add", "origin", str(remote))
    git(work, "push", "-q", "origin", "main")
    git(work, "switch", "-q", "-c", "feat/x")
    return work, remote


def run_hook(work: Path, stdin: str = "{}", *, checks: str = "true",
             lint: str = "true", **env):
    environment = {k: v for k, v in os.environ.items()
                   if k != "CLAUDE_PROJECT_DIR"}
    environment.update({"AUTO_COMMIT_LINT_CMD": lint,
                        "AUTO_COMMIT_TEST_CMD": checks})
    environment.update({k: str(v) for k, v in env.items()})
    done = subprocess.run(
        [BASH, ".claude/hooks/auto-commit-push.sh"], cwd=work, input=stdin,
        capture_output=True, text=True, encoding="utf-8", env=environment,
        timeout=120)
    return done


def edit(work: Path, text: str = "x = 2\n", name: str = "app.py") -> None:
    (work / name).write_text(text, encoding="utf-8")


def system_message(done) -> str:
    lines = [ln for ln in done.stdout.splitlines() if ln.strip()]
    assert lines, f"no stdout; stderr was: {done.stderr}"
    return json.loads(lines[-1])["systemMessage"]


# -- never loops, never empty ---------------------------------------------------

def test_a_continuation_stop_does_nothing(repo):
    work, _ = repo
    edit(work)
    before = commits(work)
    done = run_hook(work, '{"stop_hook_active": true}')
    assert done.returncode == 0
    assert commits(work) == before
    assert (work / "app.py").read_text() == "x = 2\n", "changes left alone"


def test_a_clean_tree_makes_no_commit(repo):
    work, _ = repo
    before = commits(work)
    done = run_hook(work)
    assert done.returncode == 0 and commits(work) == before


def test_running_twice_never_creates_an_empty_commit(repo):
    work, _ = repo
    edit(work)
    assert run_hook(work).returncode == 0
    after_first = commits(work)
    assert run_hook(work).returncode == 0
    assert commits(work) == after_first


# -- branch policy --------------------------------------------------------------

def test_it_refuses_to_commit_on_main(repo):
    work, _ = repo
    git(work, "switch", "-q", "main")
    edit(work)
    before = commits(work)
    done = run_hook(work)
    assert done.returncode == 2
    assert "feature branch" in done.stderr
    assert commits(work) == before
    assert (work / "app.py").read_text() == "x = 2\n"


def test_main_can_be_allowed_explicitly(repo):
    work, _ = repo
    git(work, "switch", "-q", "main")
    edit(work)
    before = commits(work)
    done = run_hook(work, AUTO_COMMIT_ALLOW_MAIN=1)
    assert done.returncode == 0
    assert commits(work) == before + 1


# -- checks gate the commit -----------------------------------------------------

def test_failing_tests_block_the_commit_and_are_reported(repo):
    work, _ = repo
    edit(work)
    before = commits(work)
    done = run_hook(work, checks="echo 'AssertionError: boom'; false")
    assert done.returncode == 2
    assert "tests FAILED" in done.stderr and "boom" in done.stderr
    assert commits(work) == before
    assert git(work, "diff", "--cached", "--name-only") == "", "nothing staged"
    assert (work / "app.py").read_text() == "x = 2\n", "the work is kept"


def test_failing_lint_blocks_the_commit(repo):
    work, _ = repo
    edit(work)
    before = commits(work)
    done = run_hook(work, lint="echo 'F821 undefined name'; false")
    assert done.returncode == 2 and "lint FAILED" in done.stderr
    assert commits(work) == before


def test_a_web_change_without_its_dependencies_is_not_committed(repo):
    work, _ = repo
    (work / "web").mkdir()
    edit(work, "export const x = 1;\n", "web/page.tsx")
    before = commits(work)
    done = run_hook(work)
    assert done.returncode == 2 and "npm ci" in done.stderr
    assert commits(work) == before


def test_failing_web_checks_block_the_commit(repo):
    work, _ = repo
    (work / "web").mkdir()
    edit(work, "export const x: number = 'a';\n", "web/page.tsx")
    before = commits(work)
    done = run_hook(work, AUTO_COMMIT_WEB_CMD="echo 'TS2322'; false")
    assert done.returncode == 2 and "web FAILED" in done.stderr
    assert commits(work) == before


def test_web_checks_only_run_when_web_changed(repo):
    work, _ = repo
    edit(work)  # a Python-only change; no web/node_modules exists
    done = run_hook(work, AUTO_COMMIT_WEB_CMD="false")
    assert done.returncode == 0, done.stderr


# -- secrets and generated files -------------------------------------------------

@pytest.mark.parametrize("name", [
    ".env", ".env.local", "credentials.json", "server.pem", "id_rsa",
    "secrets.yaml", "app.key",
])
def test_secret_files_are_refused(repo, name):
    work, _ = repo
    edit(work, "safe\n", name)
    before = commits(work)
    done = run_hook(work)
    assert done.returncode == 2
    assert name in done.stderr
    assert commits(work) == before
    assert git(work, "diff", "--cached", "--name-only") == ""


def test_an_env_example_template_is_allowed(repo):
    work, _ = repo
    edit(work, "API_KEY=\n", ".env.example")
    assert run_hook(work).returncode == 0


@pytest.mark.parametrize("secret", [
    "gh" + "p_" + "A" * 36,
    "AK" + "IA" + "B" * 16,
    "gs" + "k_" + "c" * 24,
    "-----BEGIN " + "RSA PRIVATE" + " KEY-----",
])
def test_a_key_pasted_into_source_is_refused_without_echoing_it(repo, secret):
    work, _ = repo
    edit(work, f'TOKEN = "{secret}"\n', "config.py")
    before = commits(work)
    done = run_hook(work)
    assert done.returncode == 2
    assert "config.py" in done.stderr
    assert secret not in done.stderr, "the secret itself must not be printed"
    assert commits(work) == before


def test_generated_and_dependency_directories_are_refused(repo):
    work, _ = repo
    (work / "build").mkdir()
    edit(work, "artifact\n", "build/out.txt")
    done = run_hook(work)
    assert done.returncode == 2 and "build/out.txt" in done.stderr


# -- commit and push ------------------------------------------------------------

def test_a_clean_change_is_committed_and_pushed(repo):
    work, remote = repo
    edit(work)
    before = commits(work)
    done = run_hook(work)

    assert done.returncode == 0, done.stderr
    assert commits(work) == before + 1
    sha = git(work, "rev-parse", "HEAD")
    assert git(remote, "rev-parse", "feat/x") == sha, "pushed to the branch"
    assert "pushed" in system_message(done)


def test_commits_carry_the_owner_identity_and_no_ai_trailer(repo):
    work, _ = repo
    edit(work)
    run_hook(work)
    assert git(work, "log", "-1", "--format=%an <%ae>") == \
        f"{IDENTITY[0]} <{IDENTITY[1]}>"
    assert git(work, "log", "-1", "--format=%cn <%ce>") == \
        f"{IDENTITY[0]} <{IDENTITY[1]}>"
    assert "co-authored-by" not in git(work, "log", "-1", "--format=%B").lower()


def test_the_generic_message_is_conventional_and_truthful(repo):
    work, _ = repo
    edit(work, "# notes\n", "NOTES.md")
    run_hook(work)
    subject = git(work, "log", "-1", "--format=%s")
    assert subject.startswith("docs: update 1 file(s)")
    body = git(work, "log", "-1", "--format=%b")
    assert "NOTES.md" in body


def test_an_agent_written_message_is_used_and_consumed(repo):
    work, _ = repo
    edit(work)
    (work / ".claude" / "commit-message.txt").write_text(
        "fix(gates): stop blocking on unused imports\n\n"
        "A style rule was hiding the test signal.\n", encoding="utf-8")
    run_hook(work)
    assert git(work, "log", "-1", "--format=%s") == \
        "fix(gates): stop blocking on unused imports"
    assert not (work / ".claude" / "commit-message.txt").exists()


def test_an_ai_coauthor_trailer_in_the_message_file_is_stripped(repo):
    work, _ = repo
    edit(work)
    (work / ".claude" / "commit-message.txt").write_text(
        "feat: add a thing\n\nWhy.\n\n"
        "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>\n",
        encoding="utf-8")
    run_hook(work)
    assert "co-authored" not in git(work, "log", "-1", "--format=%B").lower()
    assert git(work, "log", "-1", "--format=%s") == "feat: add a thing"


def test_a_non_conventional_message_is_rejected_before_committing(repo):
    work, _ = repo
    edit(work)
    (work / ".claude" / "commit-message.txt").write_text(
        "updated some stuff\n", encoding="utf-8")
    before = commits(work)
    done = run_hook(work)
    assert done.returncode == 2
    assert "conventional" in done.stderr
    assert commits(work) == before
    assert git(work, "diff", "--cached", "--name-only") == ""


def test_a_push_failure_keeps_the_commit_and_does_not_block(repo):
    work, remote = repo
    git(work, "remote", "set-url", "origin", str(remote.parent / "missing.git"))
    edit(work)
    before = commits(work)
    done = run_hook(work)
    assert done.returncode == 0, "credentials are not Claude's to fix"
    assert commits(work) == before + 1
    message = system_message(done)
    assert "FAILED" in message and "only local" in message


def test_without_an_origin_it_commits_and_says_nothing_was_pushed(repo):
    work, _ = repo
    git(work, "remote", "remove", "origin")
    edit(work)
    done = run_hook(work)
    assert done.returncode == 0
    assert "No 'origin' remote" in system_message(done)


# -- switches and static guarantees ---------------------------------------------

def test_the_disable_file_turns_the_hook_off(repo):
    work, _ = repo
    edit(work)
    (work / ".claude" / "auto-commit.disabled").write_text("", encoding="utf-8")
    before = commits(work)
    assert run_hook(work).returncode == 0
    assert commits(work) == before


def test_the_disable_environment_variable_turns_it_off(repo):
    work, _ = repo
    edit(work)
    before = commits(work)
    assert run_hook(work, AUTO_COMMIT_DISABLED=1).returncode == 0
    assert commits(work) == before


def test_the_script_contains_no_history_rewriting_or_force_flags():
    text = "\n".join(
        line for line in HOOK.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#"))
    for forbidden in ("--force", "--amend", "--no-verify", "--date",
                      "push -f", "push +", "reset --hard", "filter-branch",
                      "rebase", "commit --allow-empty"):
        assert forbidden not in text, forbidden


def test_the_stop_hook_is_registered_with_the_requested_command():
    settings = json.loads(
        (ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
    (entry,) = settings["hooks"]["Stop"]
    (hook,) = entry["hooks"]
    assert hook["type"] == "command"
    assert hook["command"] == "bash .claude/hooks/auto-commit-push.sh"
