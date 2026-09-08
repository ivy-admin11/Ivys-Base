"""Unattended pushes, and the review that has to happen without a reviewer.

The alternative was a deploy key the cloud sandbox could use — which means a
write credential to the repo living in a folder that syncs off the machine and
is readable by every future session. That is the "credential files in the
project folder" finding from the September health check, recreated on purpose.
This runs on the Mac with the key already there instead.

The trade is that nobody looks at the diff before it leaves. So the scan that
would have happened by eye happens in the script, and these tests hold it to
that — including not crying wolf over a placeholder, because a guard that
fires on documentation gets switched off.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "autopush.sh"


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args],
                          capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path):
    """A clone with an upstream, and autopush.sh in place."""
    bare = tmp_path / "bare.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    work = tmp_path / "work"
    subprocess.run(["git", "clone", "-q", str(bare), str(work)],
                   check=True, capture_output=True)
    git(work, "config", "user.email", "t@example.com")
    git(work, "config", "user.name", "t")
    (work / "scripts").mkdir()
    (work / "scripts" / "autopush.sh").write_bytes(SCRIPT.read_bytes())
    (work / "a.txt").write_text("hi\n")
    git(work, "add", "-A")
    git(work, "commit", "-qm", "init")
    git(work, "push", "-q", "origin", "HEAD:master")
    git(work, "branch", "--set-upstream-to=origin/master")
    return work


def run(work):
    return subprocess.run(["bash", "scripts/autopush.sh"], cwd=work,
                          capture_output=True, text=True, timeout=60)


def unpushed(work):
    out = git(work, "rev-list", "--count", "@{u}..HEAD").stdout.strip()
    return int(out or 0)


def commit(work, name, body):
    (work / name).write_text(body)
    git(work, "add", "-A")
    git(work, "commit", "-qm", f"add {name}")


class TestTheHappyPath:
    def test_nothing_to_push_says_nothing(self, repo):
        """A job running every 15 minutes must not write a line every time."""
        r = run(repo)
        assert r.returncode == 0
        assert r.stdout.strip() == ""

    def test_a_normal_commit_is_pushed(self, repo):
        commit(repo, "b.txt", "more\n")
        assert run(repo).returncode == 0
        assert unpushed(repo) == 0

    def test_it_reports_what_it_pushed(self, repo):
        commit(repo, "b.txt", "more\n")
        assert "pushing 1 commit" in run(repo).stdout


class TestItRefusesToLeakCredentials:
    """Nobody reviews an unattended push, so refusing is the safe outcome —
    the commits stay on the machine, which is where they already were."""

    @pytest.mark.parametrize("name", [
        ".env", "id_rsa", "deploy.pem", "service-account-key.json",
        "discord_backup_codes.txt",
    ])
    def test_a_credential_shaped_file_is_refused(self, repo, name):
        commit(repo, name, "whatever\n")
        r = run(repo)
        assert r.returncode == 2
        assert "credential-shaped file" in r.stdout
        assert unpushed(repo) == 1, "the commit must stay local"

    def test_a_real_looking_secret_value_is_refused(self, repo):
        commit(repo, "cfg.py",
               'API_KEY = "abcdefghijklmnopqrstuvwxyz1234567890"\n')
        r = run(repo)
        assert r.returncode == 2
        assert "credential value" in r.stdout
        assert unpushed(repo) == 1

    def test_the_refusal_does_not_print_the_secret(self, repo):
        """A guard that logs the thing it caught defeats itself."""
        secret = "abcdefghijklmnopqrstuvwxyz1234567890"
        commit(repo, "cfg.py", f'API_KEY = "{secret}"\n')
        assert secret not in run(repo).stdout


class TestItDoesNotCryWolf:
    """A guard that fires on documentation is a guard someone switches off."""

    def test_a_placeholder_in_a_doc_still_pushes(self, repo):
        commit(repo, "README.md", 'API_KEY = "your_api_key_here"\n')
        assert run(repo).returncode == 0
        assert unpushed(repo) == 0

    def test_an_ordinary_code_change_still_pushes(self, repo):
        commit(repo, "mod.py", "def f():\n    return 1\n")
        assert run(repo).returncode == 0


class TestItIsConservative:
    def test_no_upstream_means_no_remote_branch_is_created(self, repo):
        """Creating a remote branch unattended is a bigger decision than
        pushing to one that is already tracked."""
        git(repo, "checkout", "-qb", "brand-new")
        commit(repo, "c.txt", "x\n")
        r = run(repo)
        assert r.returncode == 0
        assert "no upstream" in r.stdout
        assert git(repo, "ls-remote", "--heads", "origin", "brand-new").stdout.strip() == ""

    def test_a_detached_head_is_left_alone(self, repo):
        sha = git(repo, "rev-parse", "HEAD").stdout.strip()
        git(repo, "checkout", "-q", sha)
        r = run(repo)
        assert r.returncode == 0
        assert "detached" in r.stdout

    def test_it_never_force_pushes(self):
        source = SCRIPT.read_text()
        assert "--force" not in source and "-f " not in source

    def test_it_never_commits(self):
        """It moves commits; it does not make them."""
        source = SCRIPT.read_text()
        assert "git commit" not in source
        assert "git add" not in source
