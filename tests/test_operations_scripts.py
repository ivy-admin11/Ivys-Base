"""Focused, non-delivering tests for production operations safeguards."""

from __future__ import annotations

import io
import hashlib
import os
from pathlib import Path
import plistlib
import shutil
import stat
import sqlite3
import subprocess
import sys
import tarfile
import textwrap

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_NAMES = (
    "backup_state.sh",
    "deploy_production.sh",
    "monitor_ivy.sh",
    "production_smoke_test.sh",
    "restart_ivy.sh",
    "restore_state.sh",
    "rollback_release.sh",
)


def run_script(path: Path, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(path), *args],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_operations_scripts_are_executable_and_parse_as_bash() -> None:
    paths = [REPO_ROOT / "scripts" / name for name in SCRIPT_NAMES]
    paths.append(REPO_ROOT / "deploy" / "install_launchd.sh")

    for path in paths:
        assert path.stat().st_mode & stat.S_IXUSR, path
        result = subprocess.run(
            ["bash", "-n", str(path)],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert result.returncode == 0, result.stderr


def test_default_backup_and_restart_are_no_write_dry_runs(tmp_path: Path) -> None:
    backup_destination = tmp_path / "must-not-be-created"
    backup = run_script(
        REPO_ROOT / "scripts" / "backup_state.sh",
        "--destination",
        str(backup_destination),
    )
    assert backup.returncode == 0, backup.stderr
    assert "DRY-RUN" in backup.stdout
    assert not backup_destination.exists()

    restart = run_script(REPO_ROOT / "scripts" / "restart_ivy.sh")
    assert restart.returncode == 0, restart.stderr
    assert "DRY-RUN" in restart.stdout
    assert "No process was restarted" in restart.stdout


def test_backup_archive_is_private_secret_excluding_and_restore_compatible(tmp_path: Path) -> None:
    project = tmp_path / "fixture-project"
    fixture_script = project / "scripts" / "backup_state.sh"
    fixture_script.parent.mkdir(parents=True)
    shutil.copy2(REPO_ROOT / "scripts" / "backup_state.sh", fixture_script)

    data_dir = project / "data"
    logs_dir = project / "logs"
    outbox_dir = data_dir / "outbox"
    outbox_dir.mkdir(parents=True)
    logs_dir.mkdir(parents=True)
    for database in (
        data_dir / "picks.db",
        logs_dir / "executions.db",
        logs_dir / "imessage_worker.db",
    ):
        with sqlite3.connect(database) as connection:
            connection.execute("CREATE TABLE sample (value TEXT)")
            connection.execute("INSERT INTO sample VALUES ('safe')")
    (outbox_dir / "SP-test.json").write_text('{"status": "pending"}\n', encoding="utf-8")
    (outbox_dir / "SP-test.pdf").write_bytes(b"%PDF-1.4\nfixture")
    admin_key = "ADMIN" + "_SECRET"
    (project / ".env").write_text(f"{admin_key}=must-not-be-archived\n", encoding="utf-8")
    (project / "favorites.json").write_text('["+15555550100"]\n', encoding="utf-8")

    launch_agents = tmp_path / "launch-agents"
    launch_agents.mkdir()
    with (launch_agents / "com.ivy.gateway.plist").open("wb") as handle:
        plistlib.dump({"Label": "com.ivy.gateway"}, handle)

    destination = tmp_path / "backups"
    destination.mkdir(mode=0o700)
    env = os.environ.copy()
    env["IVY_LAUNCH_AGENTS_DIR"] = str(launch_agents)
    result = subprocess.run(
        [str(fixture_script), "--destination", str(destination), "--apply"],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    archive_line = next(line for line in result.stdout.splitlines() if line.startswith("BACKUP_ARCHIVE="))
    archive_path = Path(archive_line.split("=", 1)[1])
    assert archive_path.is_file()
    assert destination.stat().st_mode & 0o077 == 0
    assert archive_path.stat().st_mode & 0o077 == 0

    with tarfile.open(archive_path, "r:gz") as archive:
        names = archive.getnames()
    assert any(name.endswith("/state/logs/imessage_worker.db") for name in names)
    assert not any(name.endswith("/.env") for name in names)
    assert not any(name.endswith("/favorites.json") for name in names)
    assert not any(name.endswith("/com.lexi.ivy.plist") for name in names)

    restore = run_script(
        REPO_ROOT / "scripts" / "restore_state.sh",
        "--backup",
        str(archive_path),
    )
    assert restore.returncode == 0, restore.stderr
    assert "passed integrity checks" in restore.stdout

    embedded_marker = "must-not-appear-in-output"
    with (launch_agents / "com.ivy.gateway.plist").open("wb") as handle:
        plistlib.dump(
            {
                "Label": "com.ivy.gateway",
                "EnvironmentVariables": {admin_key: embedded_marker},
            },
            handle,
        )
    rejected_destination = tmp_path / "rejected-backups"
    rejected_destination.mkdir(mode=0o700)
    rejected = subprocess.run(
        [
            str(fixture_script),
            "--destination",
            str(rejected_destination),
            "--apply",
        ],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert rejected.returncode != 0
    assert "embedded credential-like value" in rejected.stderr
    assert embedded_marker not in rejected.stdout + rejected.stderr


@pytest.mark.parametrize("ready_fails", [False, True])
def test_monitor_uses_loopback_auth_without_printing_secret(tmp_path: Path, ready_fails: bool) -> None:
    fake_curl = tmp_path / "curl"
    fake_curl.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import json
            import os
            from pathlib import Path
            import sys

            args = sys.argv[1:]
            output_path = Path(args[args.index("--output") + 1])
            endpoint = args[-1].rsplit("/", 1)[-1]
            if endpoint == "health":
                payload, status = {"status": "ok"}, "200"
            elif endpoint == "ready":
                failed = os.environ.get("FAKE_READY_FAIL") == "1"
                payload, status = {"ready": not failed}, "503" if failed else "200"
            elif endpoint == "runtime":
                payload, status = {
                    "imessage": {"ready": True, "oldest_queued_age_seconds": 0},
                }, "200"
            elif endpoint.startswith("executions"):
                payload, status = {"executions": []}, "200"
            else:
                payload, status = {
                    "git_sha": "0123456789abcdef",
                    "dirty_working_tree": False,
                }, "200"
            output_path.write_text(json.dumps(payload), encoding="utf-8")
            print(status, end="")
            """
        ),
        encoding="utf-8",
    )
    fake_curl.chmod(0o700)

    output_marker = "monitor-test-value-must-not-print"
    admin_key = "ADMIN" + "_SECRET"
    env = os.environ.copy()
    env["PATH"] = f"{tmp_path}{os.pathsep}{env['PATH']}"
    env[admin_key] = output_marker
    env["FAKE_READY_FAIL"] = "1" if ready_fails else "0"
    result = run_script(REPO_ROOT / "scripts" / "monitor_ivy.sh", env=env)
    combined = result.stdout + result.stderr

    assert output_marker not in combined
    assert (result.returncode != 0) is ready_fails
    if ready_fails:
        assert "FAIL /ready: HTTP 503" in combined
    else:
        assert "OK   /health" in result.stdout
        assert "OK   /ready" in result.stdout
        assert "OK   /version" in result.stdout


@pytest.mark.parametrize(
    "unsafe_url",
    ["https://example.com", "http://localhost:8000@example.com"],
)
def test_monitor_rejects_non_loopback_before_request(unsafe_url: str) -> None:
    env = os.environ.copy()
    env["ADMIN" + "_SECRET"] = "local-placeholder"  # pragma: allowlist secret
    result = run_script(
        REPO_ROOT / "scripts" / "monitor_ivy.sh",
        "--base-url",
        unsafe_url,
        env=env,
    )
    assert result.returncode == 2
    assert "non-loopback" in result.stderr


def test_restore_rejects_archive_path_traversal(tmp_path: Path) -> None:
    archive_path = tmp_path / "malicious.tar.gz"
    with tarfile.open(archive_path, "w:gz") as archive:
        member = tarfile.TarInfo("../escape")
        member.size = 1
        archive.addfile(member, io.BytesIO(b"x"))

    result = run_script(
        REPO_ROOT / "scripts" / "restore_state.sh",
        "--backup",
        str(archive_path),
    )
    assert result.returncode != 0
    assert "unsafe archive path" in result.stderr
    assert not (tmp_path / "escape").exists()


def test_restore_accepts_a_checksummed_integrity_checked_archive(tmp_path: Path) -> None:
    payload = tmp_path / "ivy-state-20260803T000000Z-test"
    database = payload / "state" / "data" / "picks.db"
    state_json = payload / "state" / "data" / "meal_plan_state.json"
    database.parent.mkdir(parents=True)
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE sample (value TEXT)")
        connection.execute("INSERT INTO sample VALUES ('safe')")
    state_json.write_text('{"state": "valid"}\n', encoding="utf-8")
    (payload / "manifest.txt").write_text(
        "\n".join(
            (
                "BACKUP_FORMAT_VERSION=1",
                "CREATED_UTC=20260803T000000Z",
                "SOURCE_GIT_SHA=0123456789abcdef",  # pragma: allowlist secret
                "SOURCE_WORKTREE_DIRTY=false",
                "SENSITIVE_FILES_INCLUDED=false",
                "STATE_LAYOUT=allowlisted-runtime-state",
                "",
            )
        ),
        encoding="utf-8",
    )

    inventory = []
    for path in sorted(candidate for candidate in payload.rglob("*") if candidate.is_file()):
        relative = path.relative_to(payload).as_posix()
        inventory.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  ./{relative}")
    (payload / "checksums.sha256").write_text("\n".join(inventory) + "\n", encoding="utf-8")

    archive_path = tmp_path / "valid.tar.gz"
    with tarfile.open(archive_path, "w:gz") as archive:
        archive.add(payload, arcname=payload.name)

    result = run_script(
        REPO_ROOT / "scripts" / "restore_state.sh",
        "--backup",
        str(archive_path),
    )
    assert result.returncode == 0, result.stderr
    assert "passed integrity checks" in result.stdout
    assert "DRY-RUN" in result.stdout


def test_ci_has_safe_real_macos_job_and_live_smoke_has_no_bypass() -> None:
    workflow = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "runs-on: macos-14" in workflow
    assert "actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd" in workflow
    assert "actions/setup-python@a309ff8b426b58ec0e2a45f0f869d46889d02405" in workflow
    assert 'PYTEST_MACOS_INTEGRATION: "1"' in workflow
    assert "test_argv_round_trip_with_tricky_characters_real_osascript" in workflow
    assert "plutil -lint deploy/launchd/*.plist.template" in workflow
    assert "tracked secret fingerprints in this PR." in workflow
    assert "Baseline cleanup-only — existing findings removed or moved" in workflow
    assert "ADDED {filename} | {secret_type} | {hashed_secret}" in workflow
    assert "WARNING: unable to attribute all net-new filenames" in workflow

    smoke = (REPO_ROOT / "scripts" / "production_smoke_test.sh").read_text(encoding="utf-8")
    assert "--live-delivery" in smoke
    assert "--live-delivery requires --check-running" in smoke
    assert "recipient not in contacts" in smoke
    assert 'confirm_phrase="SEND IVY SMOKE MESSAGE TO $masked_recipient"' in smoke
    assert "read -r confirmation < /dev/tty" in smoke
    assert "--confirm)" not in smoke


def test_readiness_waits_are_deadline_based_and_at_least_ninety_seconds() -> None:
    for name in ("deploy_production.sh", "restart_ivy.sh", "rollback_release.sh"):
        script = (REPO_ROOT / "scripts" / name).read_text(encoding="utf-8")
        assert 'IVY_READINESS_TIMEOUT_SECONDS:-90' in script
        assert 'READINESS_TIMEOUT_SECONDS" -lt 90' in script
        if name != "rollback_release.sh":
            assert "deadline=$(" in script
        else:
            assert '--readiness-timeout-seconds "$READINESS_TIMEOUT_SECONDS"' in script

    rejected = run_script(
        REPO_ROOT / "scripts" / "restart_ivy.sh",
        "--readiness-timeout-seconds",
        "89",
    )
    assert rejected.returncode == 2
    assert "90 through 600" in rejected.stderr


def test_deploy_and_rollback_use_reviewed_production_lock() -> None:
    for name in ("deploy_production.sh", "rollback_release.sh"):
        script = (REPO_ROOT / "scripts" / name).read_text(encoding="utf-8")
        assert 'pip install -r "$PROJECT_ROOT/requirements.lock"' in script
        assert 'pip install -r "$PROJECT_ROOT/requirements.txt"' not in script


def test_backup_rejects_broad_or_nonprivate_custom_destinations_without_chmod(tmp_path: Path) -> None:
    nonprivate = tmp_path / "nonprivate"
    nonprivate.mkdir(mode=0o755)
    before = stat.S_IMODE(nonprivate.stat().st_mode)
    rejected = run_script(
        REPO_ROOT / "scripts" / "backup_state.sh",
        "--destination",
        str(nonprivate),
        "--apply",
    )
    assert rejected.returncode != 0
    assert "must already be private" in rejected.stderr
    assert stat.S_IMODE(nonprivate.stat().st_mode) == before

    broad = run_script(
        REPO_ROOT / "scripts" / "backup_state.sh",
        "--destination",
        "/private/tmp",
        "--apply",
    )
    assert broad.returncode == 2
    assert "broad custom backup destination" in broad.stderr

    mount_root = run_script(
        REPO_ROOT / "scripts" / "backup_state.sh",
        "--destination",
        "/Volumes/EncryptedDisk",
        "--apply",
    )
    assert mount_root.returncode == 2
    assert "broad custom backup destination" in mount_root.stderr


def test_restore_has_size_space_writer_type_and_transaction_guards(tmp_path: Path) -> None:
    restore_script = (REPO_ROOT / "scripts" / "restore_state.sh").read_text(encoding="utf-8")
    assert "2 GiB compressed safety limit" in restore_script
    assert "shutil.disk_usage(stage).free" in restore_script
    assert 'mktemp -d "${TMPDIR:-/tmp}/ivy-restore.XXXXXX"' in restore_script
    assert "trap rollback_restore_transaction ERR" in restore_script
    assert '"com.ivy.sharppicks"' in restore_script
    assert '"com.ivy.happy_hour_scout"' in restore_script
    assert '"com.ivy.familia_meal_planner"' in restore_script
    assert "com.lexi.ivy" not in restore_script
    assert "restore target has an unexpected type" in restore_script

    oversized = tmp_path / "oversized.tar.gz"
    with oversized.open("wb") as handle:
        handle.truncate(2 * 1024 * 1024 * 1024 + 1)
    result = run_script(
        REPO_ROOT / "scripts" / "restore_state.sh",
        "--backup",
        str(oversized),
    )
    assert result.returncode != 0
    assert "2 GiB compressed safety limit" in result.stderr


def _write_executable(path: Path, content: str = "#!/usr/bin/env bash\nexit 0\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(0o700)


def _init_deployment_fixture(
    tmp_path: Path,
    *,
    installer_content: str = "#!/usr/bin/env bash\nexit 0\n",
) -> tuple[Path, dict[str, str]]:
    project = tmp_path / "deployment-fixture"
    scripts = project / "scripts"
    deploy = scripts / "deploy_production.sh"
    scripts.mkdir(parents=True)
    shutil.copy2(REPO_ROOT / "scripts" / "deploy_production.sh", deploy)
    deploy.chmod(0o700)
    for name in ("backup_state.sh", "check_hygiene.sh", "monitor_ivy.sh", "restart_ivy.sh"):
        _write_executable(scripts / name)
    _write_executable(project / "deploy" / "install_launchd.sh", installer_content)
    (project / "requirements.txt").write_text("", encoding="utf-8")
    (project / "requirements.lock").write_text("", encoding="utf-8")
    (project / ".gitignore").write_text(".venv/\n.venv.next.*\n", encoding="utf-8")
    (project / ".ivy-operations-test-root").write_text("disposable\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    subprocess.run(["git", "-C", str(project), "config", "user.email", "ivy-test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(project), "config", "user.name", "Ivy Test"], check=True)
    subprocess.run(["git", "-C", str(project), "add", "."], check=True)
    subprocess.run(["git", "-C", str(project), "commit", "-qm", "fixture"], check=True)
    subprocess.run([sys.executable, "-m", "venv", str(project / ".venv")], check=True)
    (project / ".venv" / "original-marker").write_text("original\n", encoding="utf-8")

    fake_bin = tmp_path / "fake-bin"
    _write_executable(fake_bin / "uname", "#!/usr/bin/env bash\nprintf 'Darwin\\n'\n")
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
    env["HOME"] = str(fake_home)
    env["TMPDIR"] = str(tmp_path)
    env["IVY_PROJECT_ROOT_OVERRIDE"] = str(project)
    env["IVY_OPERATIONS_TEST_FAIL_AFTER_VENV_SWITCH"] = "1"
    return project, env


def test_applied_deployment_failure_restores_original_virtualenv(tmp_path: Path) -> None:
    project, env = _init_deployment_fixture(tmp_path)
    result = subprocess.run(
        [
            str(project / "scripts" / "deploy_production.sh"),
            "--ref",
            "HEAD",
            "--apply",
            "--yes-i-know-this-is-live",
        ],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode != 0
    assert "Injecting guarded test failure" in result.stderr
    assert "automatic rollback was incomplete" in result.stderr
    assert (project / ".venv").is_dir()
    assert not (project / ".venv").is_symlink()
    assert (project / ".venv" / "original-marker").read_text(encoding="utf-8") == "original\n"


def test_applied_deployment_failure_restores_prior_managed_plist(tmp_path: Path) -> None:
    installer = textwrap.dedent(
        """\
        #!/usr/bin/env bash
        set -euo pipefail
        for argument in "$@"; do
            if [ "$argument" = "--apply" ]; then
                mkdir -p "$HOME/Library/LaunchAgents"
                printf 'new plist\n' > "$HOME/Library/LaunchAgents/com.ivy.gateway.plist"
            fi
        done
        """
    )
    project, env = _init_deployment_fixture(tmp_path, installer_content=installer)
    launch_agents = Path(env["HOME"]) / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True)
    gateway_plist = launch_agents / "com.ivy.gateway.plist"
    gateway_plist.write_text("prior plist\n", encoding="utf-8")
    env.pop("IVY_OPERATIONS_TEST_FAIL_AFTER_VENV_SWITCH")
    env["IVY_OPERATIONS_TEST_FAIL_AFTER_PLIST_INSTALL"] = "1"

    result = subprocess.run(
        [
            str(project / "scripts" / "deploy_production.sh"),
            "--ref",
            "HEAD",
            "--apply",
            "--yes-i-know-this-is-live",
        ],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode != 0
    assert "after launchd plist installation" in result.stderr
    assert gateway_plist.read_text(encoding="utf-8") == "prior plist\n"
    assert (project / ".venv").is_dir()
    assert not (project / ".venv").is_symlink()


def _init_rollback_fixture(tmp_path: Path) -> tuple[Path, str, str, dict[str, str]]:
    project = tmp_path / "rollback-fixture"
    scripts = project / "scripts"
    scripts.mkdir(parents=True)
    rollback = scripts / "rollback_release.sh"
    shutil.copy2(REPO_ROOT / "scripts" / "rollback_release.sh", rollback)
    rollback.chmod(0o700)
    for name in (
        "backup_state.sh",
        "check_hygiene.sh",
        "deploy_production.sh",
        "monitor_ivy.sh",
        "production_smoke_test.sh",
        "restart_ivy.sh",
        "restore_state.sh",
    ):
        _write_executable(scripts / name)
    _write_executable(project / "deploy" / "install_launchd.sh")
    gateway_template = project / "deploy" / "launchd" / "com.ivy.gateway.plist.template"
    gateway_template.parent.mkdir(parents=True)
    gateway_template.write_text("fixture\n", encoding="utf-8")
    (project / "requirements.txt").write_text("", encoding="utf-8")
    (project / "requirements.lock").write_text("", encoding="utf-8")
    (project / ".gitignore").write_text(".venv/\n.venv.next.*\n", encoding="utf-8")
    (project / ".ivy-operations-test-root").write_text("disposable\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    subprocess.run(["git", "-C", str(project), "config", "user.email", "ivy-test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(project), "config", "user.name", "Ivy Test"], check=True)
    subprocess.run(["git", "-C", str(project), "add", "."], check=True)
    subprocess.run(["git", "-C", str(project), "commit", "-qm", "target"], check=True)
    target_sha = subprocess.check_output(
        ["git", "-C", str(project), "rev-parse", "HEAD"], text=True
    ).strip()
    (project / "release-marker.txt").write_text("current\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(project), "add", "release-marker.txt"], check=True)
    subprocess.run(["git", "-C", str(project), "commit", "-qm", "current"], check=True)
    original_sha = subprocess.check_output(
        ["git", "-C", str(project), "rev-parse", "HEAD"], text=True
    ).strip()
    subprocess.run([sys.executable, "-m", "venv", str(project / ".venv")], check=True)
    (project / ".venv" / "original-marker").write_text("original\n", encoding="utf-8")

    fake_bin = tmp_path / "rollback-fake-bin"
    _write_executable(fake_bin / "uname", "#!/usr/bin/env bash\nprintf 'Darwin\\n'\n")
    fake_home = tmp_path / "rollback-home"
    fake_home.mkdir()
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
    env["HOME"] = str(fake_home)
    env["TMPDIR"] = str(tmp_path)
    env["IVY_PROJECT_ROOT_OVERRIDE"] = str(project)
    env["IVY_OPERATIONS_TEST_FAIL_AFTER_VENV_SWITCH"] = "1"
    return project, target_sha, original_sha, env


def test_applied_rollback_failure_restores_original_code_and_virtualenv(tmp_path: Path) -> None:
    project, target_sha, original_sha, env = _init_rollback_fixture(tmp_path)
    result = subprocess.run(
        [
            str(project / "scripts" / "rollback_release.sh"),
            "--ref",
            target_sha,
            "--apply",
            "--yes-i-know-this-is-live",
        ],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode != 0
    assert "Injecting guarded test failure" in result.stderr
    assert "attempting to restore the original release" in result.stderr
    actual_sha = subprocess.check_output(
        ["git", "-C", str(project), "rev-parse", "HEAD"], text=True
    ).strip()
    assert actual_sha == original_sha
    assert (project / ".venv").is_dir()
    assert not (project / ".venv").is_symlink()
    assert (project / ".venv" / "original-marker").read_text(encoding="utf-8") == "original\n"


def test_installer_protects_familia_and_validates_before_install() -> None:
    installer = (REPO_ROOT / "deploy" / "install_launchd.sh").read_text(encoding="utf-8")
    assert '"com.ivy.familia_meal_planner"' in installer
    assert "plutil -lint \"$rendered_path\"" in installer
    assert "mkdir -p \"$TARGET_DIR\" \"$PROJECT_ROOT/logs\" \"$PROJECT_ROOT/data\"" in installer
    assert "IVY_VENV_PYTHON_OVERRIDE must be an absolute path" in installer
    assert 'VENV_PYTHON="$PROJECT_ROOT/.venv/bin/python"' in installer


def test_interpreter_overrides_reject_relative_unsafe_and_missing_paths() -> None:
    installer = REPO_ROOT / "deploy" / "install_launchd.sh"
    hygiene = REPO_ROOT / "scripts" / "check_hygiene.sh"

    for variable, script in (
        ("IVY_VENV_PYTHON_OVERRIDE", installer),
        ("IVY_HYGIENE_PYTHON", hygiene),
    ):
        relative_env = os.environ.copy()
        relative_env[variable] = "relative/python"
        relative = run_script(script, "--validate-only", env=relative_env) if script == installer else run_script(script, env=relative_env)
        assert relative.returncode == 2
        assert "must be an absolute path" in relative.stderr

        unsafe_env = os.environ.copy()
        unsafe_env[variable] = "/private/tmp/../bin/python"
        unsafe = run_script(script, "--validate-only", env=unsafe_env) if script == installer else run_script(script, env=unsafe_env)
        assert unsafe.returncode == 2
        assert "unsafe path component" in unsafe.stderr

        missing_env = os.environ.copy()
        missing_env[variable] = "/private/tmp/ivy-interpreter-does-not-exist"
        missing = run_script(script, "--validate-only", env=missing_env) if script == installer else run_script(script, env=missing_env)
        assert missing.returncode == 1
        assert "not an executable interpreter" in missing.stderr


def test_scheduled_plists_use_receipt_aware_worker_without_forcing_gates() -> None:
    expected = {
        "com.ivy.familia_meal_planner.plist.template": (
            "familia_meal_planner",
            "proactive_agents.Familia_meal_planner:run",
            {"Hour": 8, "Minute": 0, "Weekday": 0},
        ),
        "com.ivy.happy_hour_scout.plist.template": (
            "happy_hour",
            "proactive_agents.happy_hour_scout:run",
            {"Hour": 12, "Minute": 0, "Weekday": 0},
        ),
        "com.ivy.sharppicks.plist.template": (
            "sharp_picks",
            "proactive_agents.sports_bettor:run",
            [
                {"Hour": 9, "Minute": 0},
                {"Hour": 15, "Minute": 0},
                {"Hour": 21, "Minute": 0},
            ],
        ),
    }

    for filename, (job_name, entrypoint, schedule) in expected.items():
        raw = (REPO_ROOT / "deploy" / "launchd" / filename).read_text(encoding="utf-8")
        rendered = (
            raw.replace("__PROJECT_ROOT__", "/opt/ivy")
            .replace("__VENV_PYTHON__", "/opt/ivy/.venv/bin/python")
            .replace("__HOME__", "/Users" + "/ivy")
        )
        payload = plistlib.loads(rendered.encode("utf-8"))
        arguments = payload["ProgramArguments"]

        assert arguments[:3] == [
            "/opt/ivy/.venv/bin/python",
            "-m",
            "ivy_core.job_worker",
        ]
        assert arguments[arguments.index("--job-name") + 1] == job_name
        assert arguments[arguments.index("--entrypoint") + 1] == entrypoint
        assert "--send" in arguments
        assert "--force" not in arguments
        assert payload["StartCalendarInterval"] == schedule
