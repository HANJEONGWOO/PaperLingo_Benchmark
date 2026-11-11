"""Deploy a tested commit to the existing user systemd service (Python 3.12+).

Releases have separate virtualenvs. The existing .env and runtime stay external.
Only the service override is switched; a failed start restores its previous value.
"""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
from urllib.parse import urlsplit
from urllib.request import urlopen

RELEASE_FILES = (
    "requirements.txt", "paperlingo", "prompts", "data/abstract_data.jsonl",
    "data/abstract_labels_v2.jsonl", "data/labeling_prompt_v2.md",
)


def run(*args, **kwargs):
    return subprocess.run(args, check=True, text=True, **kwargs)


def output(*args, **kwargs):
    return run(*args, stdout=subprocess.PIPE, **kwargs).stdout.strip()


def load_config(path):
    config = json.loads(path.read_text())
    for key in ("release_dir", "env_file", "runtime_dir"):
        config[key] = Path(config[key]).expanduser().resolve()
        if any(c in str(config[key]) for c in "\n\r\0"):
            raise ValueError(f"Invalid path: {key}")
    if not re.fullmatch(r"[\w@.-]+\.service", config["service"]):
        raise ValueError("service must be a systemd .service name")
    url = urlsplit(config["health_url"])
    if url.scheme != "http" or url.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("health_url must point to the local HTTP service")
    if not config["env_file"].is_file() or not config["runtime_dir"].is_dir():
        raise ValueError("Existing server .env and runtime directory are required")
    for key in ("runtime_dir", "env_file"):
        if config[key].is_relative_to(config["release_dir"]):
            raise ValueError(f"{key} must be outside release_dir")
    return config


def atomic_write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as file:
        temp = Path(file.name)
        file.write(content)
    try:
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


@contextmanager
def deployment_lock(root):
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root / "deploy.lock").open("a") as file:
        try:
            fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another deployment is running") from None
        yield


def validate_revision(source, revision):
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("Provide the full 40-character tested commit SHA")
    if output("git", "-C", str(source), "rev-parse", "HEAD") != revision:
        raise ValueError("The checkout is not the tested commit")
    if output("git", "-C", str(source), "status", "--porcelain", "--untracked-files=no"):
        raise ValueError("The checkout contains modified tracked files")


def check_version(current_version, next_version):
    versions = (current_version, next_version)
    if any(not re.fullmatch(r"\d+\.\d+\.\d+", version) for version in versions):
        raise ValueError("Deployment requires numeric major.minor.patch versions")
    if tuple(map(int, next_version.split("."))) < tuple(map(int, current_version.split("."))):
        raise ValueError(f"Refusing to downgrade the running server: {current_version} -> {next_version}")


def prepare_release(source, revision, config):
    root = config["release_dir"]
    releases = root / "releases"
    releases.mkdir(exist_ok=True)
    release = Path(tempfile.mkdtemp(prefix=revision[:12] + "-", dir=releases))
    with tempfile.TemporaryFile() as archive:
        subprocess.run(
            ["git", "-C", str(source), "archive", revision, "--", *RELEASE_FILES],
            stdout=archive, check=True,
        )
        archive.seek(0)
        with tarfile.open(fileobj=archive) as tar:
            tar.extractall(release, filter="data")
    (release / ".env").symlink_to(config["env_file"])
    (release / "runtime").symlink_to(config["runtime_dir"], target_is_directory=True)
    (release / "REVISION").write_text(revision + "\n")
    run(sys.executable, "-m", "venv", str(release / ".venv"))
    python = str(release / ".venv/bin/python")
    run(python, "-m", "pip", "install", "--disable-pip-version-check", "-r", str(release / "requirements.txt"))
    with urlopen(config["health_url"], timeout=5) as response:
        current_version = json.load(response)["version"]
    next_version = output(python, "-c", "from paperlingo.app import VERSION; print(VERSION)", cwd=release)
    check_version(current_version, next_version)
    # Load the app and reference data using a temporary database; no live jobs run.
    run(python, "-c", """
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from paperlingo.app import create_app
from paperlingo.config import Settings
with TemporaryDirectory() as runtime:
    cfg = replace(Settings.from_env(), runtime_dir=Path(runtime),
                  judge_base_url='', judge_model='', review_mode='code')
    create_app(cfg)
""", cwd=release, env=os.environ | {"PAPERLINGO_DATA_DIR": str(release / "data")})
    return release


def unit_quote(value):
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


def unit_override(release, config):
    return (
        "[Service]\n"
        # WorkingDirectory takes a path, not an ExecStart-style quoted argument.
        f"WorkingDirectory={str(release).replace('%', '%%')}\n"
        "ExecStart=\n"
        f"ExecStart={unit_quote(release / '.venv/bin/python')} -m paperlingo\n"
        f"Environment={unit_quote('PAPERLINGO_RUNTIME_DIR=' + str(config['runtime_dir']))}\n"
        f"Environment={unit_quote('PAPERLINGO_DATA_DIR=' + str(release / 'data'))}\n"
    ).encode()


def service_python(service):
    pid = output("systemctl", "--user", "show", service, "--property=MainPID", "--value")
    if not pid.isdigit() or pid == "0":
        return None
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")[0].decode()
    except FileNotFoundError:
        return None


def wait_healthy(config, expected_python, timeout=45):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if service_python(config["service"]) == str(expected_python):
                with urlopen(config["health_url"], timeout=2) as response:
                    if json.load(response).get("status") == "ok":
                        return
        except (OSError, ValueError, subprocess.CalledProcessError):
            pass
        time.sleep(1)
    raise RuntimeError("The new service did not become healthy")


def backup_database(runtime, backup_dir):
    source = runtime / "paperlingo.sqlite3"
    if source.exists():
        with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as db:
            with sqlite3.connect(backup_dir / source.name) as backup:
                db.backup(backup)


def activate_release(release, config, unit_root=None):
    unit_root = unit_root or Path(os.getenv("XDG_CONFIG_HOME", Path.home() / ".config")) / "systemd/user"
    override = unit_root / (config["service"] + ".d") / "90-paperlingo-deploy.conf"
    previous = override.read_bytes() if override.exists() else None
    previous_python = service_python(config["service"])
    if not previous_python:
        raise RuntimeError("The existing service must be running before deployment")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    backup = config["release_dir"] / "backups" / stamp
    backup.mkdir(parents=True, mode=0o700)
    (backup / "previous-override.json").write_text(json.dumps({
        "content": previous.decode() if previous is not None else None,
        "path": str(override),
    }))
    try:
        run("systemctl", "--user", "stop", config["service"])
        backup_database(config["runtime_dir"], backup)
        atomic_write(override, unit_override(release, config))
        run("systemctl", "--user", "daemon-reload")
        run("systemctl", "--user", "start", config["service"])
        wait_healthy(config, release / ".venv/bin/python")
        atomic_write(config["release_dir"] / "current.json", json.dumps({
            "release": str(release), "revision": (release / "REVISION").read_text().strip(),
            "backup": str(backup), "deployed_at": stamp,
        }, indent=2).encode())
    except BaseException:
        print("Deployment failed; restoring the previous service configuration.", flush=True)
        # Stop the failed process before restoring the old one. Do not overwrite
        # the live DB: that could discard submissions made after the new start.
        run("systemctl", "--user", "stop", config["service"])
        if previous is None:
            override.unlink(missing_ok=True)
        else:
            atomic_write(override, previous)
        run("systemctl", "--user", "daemon-reload")
        run("systemctl", "--user", "start", config["service"])
        wait_healthy(config, previous_python)
        raise
    print(f"Deployed {release.name}; database backup: {backup}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path.home() / ".config/paperlingo/deploy.json")
    parser.add_argument("--source", type=Path, default=Path.cwd())
    parser.add_argument("--revision", required=True)
    parser.add_argument("--check", action="store_true", help="Validate configuration and checkout without changing the service")
    args = parser.parse_args()
    os.umask(0o077)
    config = load_config(args.config)
    validate_revision(args.source, args.revision)
    run("systemctl", "--user", "is-active", "--quiet", config["service"])
    if args.check:
        print("Deployment configuration and checkout are ready.")
        return

    def interrupted(signum, frame):
        raise RuntimeError(f"Deployment interrupted by signal {signum}")

    signal.signal(signal.SIGTERM, interrupted)
    with deployment_lock(config["release_dir"]):
        release = prepare_release(args.source, args.revision, config)
        activate_release(release, config)


if __name__ == "__main__":
    main()
