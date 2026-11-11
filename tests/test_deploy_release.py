"""Deployment failures must not discard records or strand the shared service."""
import json
from pathlib import Path
import sqlite3
import subprocess

import pytest

from scripts import deploy_release as deploy


@pytest.fixture
def server(tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    with sqlite3.connect(runtime / "paperlingo.sqlite3") as db:
        db.execute("CREATE TABLE records (name TEXT)")
        db.execute("INSERT INTO records VALUES ('existing evaluation')")
    env = tmp_path / "server.env"
    env.write_text("PAPERLINGO_PUBLIC_ACCESS=true\n")
    config = {
        "release_dir": tmp_path / "deploy",
        "env_file": env,
        "runtime_dir": runtime,
        "service": "test-web.service",
        "health_url": "http://127.0.0.1:4179/api/health",
    }
    config["release_dir"].mkdir()
    return config


def test_config_requires_separate_existing_server_data(server, tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(server, default=str))
    assert deploy.load_config(path) == server
    for changed in (
        {"runtime_dir": server["release_dir"]},
        {"env_file": tmp_path / "missing.env"},
        {"service": "../other.service"},
        {"health_url": "https://external.example/api/health"},
    ):
        path.write_text(json.dumps(server | changed, default=str))
        with pytest.raises(ValueError):
            deploy.load_config(path)


def test_only_one_deployment_may_run(tmp_path):
    with deploy.deployment_lock(tmp_path):
        with pytest.raises(RuntimeError, match="Another deployment"):
            with deploy.deployment_lock(tmp_path):
                pytest.fail("Concurrent deployment acquired the lock")
    with deploy.deployment_lock(tmp_path):
        pass


def test_older_checkout_cannot_downgrade_the_live_server():
    deploy.check_version("0.10.0", "0.11.0")
    deploy.check_version("0.11.0", "0.11.0")
    deploy.check_version("0.9.0", "0.10.0")
    with pytest.raises(ValueError, match="downgrade"):
        deploy.check_version("0.11.0", "0.10.0")
    with pytest.raises(ValueError, match="major.minor.patch"):
        deploy.check_version("unknown", "0.11.0")


def test_revision_validation_uses_real_commit_and_rejects_dirty_tree(tmp_path):
    def git(*args):
        return subprocess.check_output(["git", "-C", str(tmp_path), *args], text=True).strip()
    git("init", "-q")
    file = tmp_path / "source.py"
    file.write_text("original")
    git("add", "source.py")
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "initial")
    revision = git("rev-parse", "HEAD")
    deploy.validate_revision(tmp_path, revision)
    with pytest.raises(ValueError, match="40-character"):
        deploy.validate_revision(tmp_path, "master")
    with pytest.raises(ValueError, match="not the tested commit"):
        deploy.validate_revision(tmp_path, "0" * 40)
    file.write_text("uncommitted")
    with pytest.raises(ValueError, match="modified tracked files"):
        deploy.validate_revision(tmp_path, revision)


@pytest.mark.parametrize("previous", [None, b"[Service]\n# previous deployment\n"])
@pytest.mark.parametrize("failure", [None, "health", "backup", "start"])
def test_switch_preserves_db_and_restores_previous_config(server, tmp_path, monkeypatch, previous, failure):
    release = server["release_dir"] / "release"
    release.mkdir()
    (release / "REVISION").write_text("a" * 40)
    units = tmp_path / "units"
    override = units / "test-web.service.d/90-paperlingo-deploy.conf"
    if previous is not None:
        deploy.atomic_write(override, previous)
    calls = []
    start_calls = 0

    def fake_run(*args, **kwargs):
        nonlocal start_calls
        calls.append(args)
        if args[2] == "start":
            start_calls += 1
            if failure == "start" and start_calls == 1:
                raise RuntimeError("start failed")

    def healthy(config, python):
        if str(python) != "/old/venv/bin/python" and failure == "health":
            # A submission arriving during the failed release must survive rollback.
            with sqlite3.connect(server["runtime_dir"] / "paperlingo.sqlite3") as db:
                db.execute("INSERT INTO records VALUES ('new evaluation')")
            raise RuntimeError("health failed")

    monkeypatch.setattr(deploy, "run", fake_run)
    monkeypatch.setattr(deploy, "service_python", lambda service: "/old/venv/bin/python")
    monkeypatch.setattr(deploy, "wait_healthy", healthy)
    if failure == "backup":
        def fail_backup(*args):
            raise RuntimeError("backup failed")
        monkeypatch.setattr(deploy, "backup_database", fail_backup)

    if failure:
        with pytest.raises(RuntimeError, match=failure):
            deploy.activate_release(release, server, units)
        assert (override.read_bytes() if override.exists() else None) == previous
        assert not (server["release_dir"] / "current.json").exists()
    else:
        deploy.activate_release(release, server, units)
        assert str(release).encode() in override.read_bytes()
        current = json.loads((server["release_dir"] / "current.json").read_text())
        assert current["revision"] == "a" * 40
    assert calls[0] == ("systemctl", "--user", "stop", "test-web.service")
    assert calls[-1] == ("systemctl", "--user", "start", "test-web.service")
    with sqlite3.connect(server["runtime_dir"] / "paperlingo.sqlite3") as db:
        rows = db.execute("SELECT name FROM records").fetchall()
    assert rows[0] == ("existing evaluation",)
    if failure == "health":
        assert rows[1] == ("new evaluation",)
    if failure != "backup":
        backup = next((server["release_dir"] / "backups").glob("*/paperlingo.sqlite3"))
        with sqlite3.connect(backup) as db:
            assert db.execute("SELECT name FROM records").fetchall() == [("existing evaluation",)]


def test_stopped_service_is_not_modified(server, tmp_path, monkeypatch):
    monkeypatch.setattr(deploy, "service_python", lambda service: None)
    with pytest.raises(RuntimeError, match="must be running"):
        deploy.activate_release(tmp_path / "release", server, tmp_path / "units")
    assert not (tmp_path / "units").exists()


def test_health_check_does_not_accept_an_old_process(server, monkeypatch):
    ticks = iter([0, 0, 0, 10])
    monkeypatch.setattr(deploy.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(deploy.time, "sleep", lambda value: None)
    monkeypatch.setattr(deploy, "service_python", lambda service: "/old/python")
    monkeypatch.setattr(deploy, "urlopen", lambda *args, **kwargs: pytest.fail("Old service cannot prove new release health"))
    with pytest.raises(RuntimeError, match="healthy"):
        deploy.wait_healthy(server, "/new/python", timeout=5)


def test_systemd_paths_escape_specifiers_spaces_and_quotes(server):
    release = Path('/tmp/release %h "test"')
    override = deploy.unit_override(release, server).decode()
    assert 'WorkingDirectory=/tmp/release %%h "test"\n' in override
    assert 'ExecStart=\nExecStart="/tmp/release %%h \\"test\\"/.venv/bin/python" -m paperlingo' in override
