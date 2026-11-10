"""Opt-in end-to-end coverage against an isolated r-nacos v0.8.8 binary."""

import json
import os
import shutil
import socket
import subprocess
import sys
import time
import zipfile
from contextlib import ExitStack
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import ProxyHandler, build_opener

import pytest

TEST_PASSWORD = "local-integration-password"
RAFT_STARTUP_PANIC = "panicked at src/raft/filestore/raftapply.rs"


@pytest.fixture
def server(tmp_path):
    configured = os.environ.get("RNACOSCTL_TEST_SERVER_BINARY")
    if not configured:
        pytest.skip("Set RNACOSCTL_TEST_SERVER_BINARY to an r-nacos v0.8.8 binary.")
    binary = Path(configured)
    assert binary.is_absolute() and binary.is_file(), (
        "RNACOSCTL_TEST_SERVER_BINARY must point to an absolute executable path."
    )
    opener = build_opener(ProxyHandler({}))

    with ExitStack() as cleanup:

        def start(name, *, captcha=False, session_ttl=86400):
            for attempt in range(3):
                workdir = tmp_path / f"{name}-{attempt}"
                workdir.mkdir()
                with ExitStack() as ports:
                    listeners = [ports.enter_context(socket.socket()) for _ in range(3)]
                    for listener in listeners:
                        listener.bind(("127.0.0.1", 0))
                    http, grpc, console = [listener.getsockname()[1] for listener in listeners]
                env = {
                    key: value for key, value in os.environ.items() if not key.startswith("RNACOS_")
                }
                env.update(
                    RNACOS_SDK_HOST="127.0.0.1",
                    RNACOS_CONSOLE_HOST="127.0.0.1",
                    RNACOS_HTTP_PORT=str(http),
                    RNACOS_HTTP_CONSOLE_PORT=str(console),
                    RNACOS_GRPC_PORT=str(grpc),
                    RNACOS_DATA_DIR=str(workdir / "data"),
                    RNACOS_INIT_ADMIN_PASSWORD=TEST_PASSWORD,
                    RNACOS_CONSOLE_ENABLE_CAPTCHA=str(captcha).lower(),
                    RNACOS_CONSOLE_LOGIN_TIMEOUT=str(session_ttl),
                    RNACOS_API_LOGIN_ONE_MINUTE_LIMIT="100",
                    RNACOS_HTTP_WORKERS="1",
                    RUST_LOG="warn",
                )
                log_path = workdir / "server.log"
                with log_path.open("wb") as log:
                    process = subprocess.Popen(
                        [str(binary)], cwd=workdir, env=env, stdout=log, stderr=subprocess.STDOUT
                    )
                cleanup.callback(stop, process)
                if wait_for_admin(process, opener, http, log_path):
                    return f"http://127.0.0.1:{console}"
                stop(process)
                log = log_path.read_text(errors="replace")
                # Upstream v0.8.8 can start Raft before its actor dependencies are injected.
                if RAFT_STARTUP_PANIC not in log:
                    break
            pytest.fail(f"r-nacos console did not start: {log}")

        yield start


def wait_for_admin(process, opener, http, log_path):
    url = f"http://127.0.0.1:{http}/nacos/v1/auth/login"
    credentials = urlencode({"username": "admin", "password": TEST_PASSWORD}).encode()
    deadline = time.monotonic() + 15
    while process.poll() is None and time.monotonic() < deadline:
        if RAFT_STARTUP_PANIC in log_path.read_text(errors="replace"):
            return False
        try:
            with opener.open(url, data=credentials, timeout=1) as response:
                if json.load(response).get("username") == "admin":
                    return True
        except (URLError, TimeoutError):
            pass
        time.sleep(0.1)
    return False


def stop(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


@pytest.fixture
def run(tmp_path):
    def invoke(*args, stdin=None, error=None, exit_status=None, home=None):
        env = {
            **os.environ,
            "RNACOSCTL_HOME": str(home or tmp_path / "profiles"),
            "RNACOSCTL_PROFILE": "",
            "NO_PROXY": "localhost,127.0.0.1",
            "no_proxy": "localhost,127.0.0.1",
        }
        result = subprocess.run(
            [sys.executable, "-m", "rnacosctl.cli", *map(str, args)],
            check=False,
            input=stdin,
            text=True,
            capture_output=True,
            env=env,
            timeout=30,
        )
        if error:
            assert not result.stdout
            assert result.returncode != 0, result
            if exit_status is not None:
                assert result.returncode == exit_status, result
            assert json.loads(result.stderr)["error"]["code"] == error, result
            return None
        assert result.returncode == 0 and not result.stderr, result
        response = json.loads(result.stdout)
        assert response["ok"] is True
        return response["data"]

    return invoke


def login(run, url, profile, *, username="admin", password=TEST_PASSWORD):
    return run(
        "--profile",
        profile,
        "auth",
        "login",
        "--url",
        url,
        "--username",
        username,
        "--password-stdin",
        stdin=password + "\n",
    )


def test_profile_switch_and_config_roundtrip(server, run, tmp_path):
    primary = server("first-server")
    secondary = server("second-server")
    assert login(run, primary, "primary")["admin"] is True
    assert login(run, secondary, "secondary")["admin"] is True
    assert run("profile", "list")["active"] == "primary"

    namespace = ["--namespace", "isolated"]
    config = [*namespace, "--data-id", "settings.json"]
    run("namespace", "add", "--id", "isolated", "--name", "Primary")
    run("--profile", "secondary", "namespace", "add", "--id", "isolated", "--name", "Secondary")
    assert any(item["namespaceName"] == "Primary" for item in run("namespace", "list"))
    assert any(
        item["namespaceName"] == "Secondary"
        for item in run("--profile", "secondary", "namespace", "list")
    )

    first, second, other = '{"value":1}\n', '{"value":2}\n', '{"value":3}\n'
    run("config", "set", *config, "--type", "json", "--file", "-", stdin=first)
    run("config", "set", *config, "--type", "json", "--file", "-", stdin=second)
    run(
        "--profile",
        "secondary",
        "config",
        "set",
        *config,
        "--type",
        "json",
        "--file",
        "-",
        stdin=other,
    )
    assert run("config", "get", *config)["value"] == second
    assert run("--profile", "secondary", "config", "get", *config)["value"] == other
    assert run("profile", "list")["active"] == "primary"
    assert [item["dataId"] for item in run("config", "list", *namespace)["list"]] == [
        "settings.json"
    ]
    revisions = run("config", "history", *config)["list"]
    assert first in [revision["content"] for revision in revisions]

    archive = tmp_path / "configuration.zip"
    run("config", "export", *namespace, "--output", archive)
    with zipfile.ZipFile(archive) as contents:
        assert contents.read("DEFAULT_GROUP/settings.json").decode() == second

    run("profile", "use", "secondary")
    assert run("config", "get", *config)["value"] == other
    run(
        "config",
        "import",
        *namespace,
        "--file",
        archive,
        error="CONFIRMATION_REQUIRED",
        exit_status=2,
    )
    assert run("config", "get", *config)["value"] == other
    run("config", "import", *namespace, "--file", archive, "--yes")
    assert run("config", "get", *config)["value"] == second

    duplicate = tmp_path / "duplicated-profiles"
    shutil.copytree(tmp_path / "profiles", duplicate)
    assert run("auth", "status", home=duplicate)["profile"] == "secondary"
    assert run("auth", "logout")["session_removed"] is True
    assert run("profile", "list")["active"] == "secondary"
    run("auth", "status", error="AUTH_REQUIRED", exit_status=3)
    run("auth", "status", home=duplicate, error="AUTH_REQUIRED", exit_status=3)
    assert run("--profile", "primary", "auth", "status")["admin"] is True


def test_service_registry_and_scoped_user(server, run):
    url = server("registry-server")
    login(run, url, "registry")
    run("namespace", "add", "--id", "registry", "--name", "Registry")
    service = ["--namespace", "registry", "--service", "demo"]
    instance = [*service, "--ip", "127.0.0.1", "--port", "9000"]
    run("service", "add", *service)
    run("instance", "add", *instance)
    assert run("instance", "get", *instance)["ip"] == "127.0.0.1"
    run("instance", "remove", *instance, "--yes")
    run("service", "remove", *service, "--yes")

    reader_password = "integration-reader-password"
    user = run(
        "user",
        "add",
        "--username",
        "reader",
        "--role",
        "visitor",
        "--allow-namespace",
        "registry",
        "--password-file",
        "-",
        stdin=reader_password + "\n",
    )
    assert user["username"] == "reader"
    assert login(run, url, "reader", username="reader", password=reader_password)["admin"] is False
    namespaces = run("--profile", "reader", "namespace", "list")
    assert [item["namespaceId"] for item in namespaces] == ["registry"]
    run("--profile", "reader", "user", "list", error="PERMISSION_DENIED")
    run("user", "remove", "--username", "reader", "--yes")


def test_captcha_and_session_expiration(server, run, tmp_path):
    protected = server("captcha-server", captcha=True)
    image = tmp_path / "challenge.png"
    challenge = run("auth", "challenge", "--url", protected, "--image", image)
    assert challenge["required"] is True
    assert challenge["challenge_token"]
    assert image.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    run(
        "auth",
        "login",
        "--url",
        protected,
        "--password-stdin",
        stdin=TEST_PASSWORD + "\n",
        error="CAPTCHA_REQUIRED",
        exit_status=3,
    )
    assert run("profile", "list")["profiles"] == []

    session_ttl = 3
    expiring = server("expiring-server", session_ttl=session_ttl)
    login(run, expiring, "expiring")
    # r-nacos checks expiration at whole-second resolution.
    time.sleep(session_ttl + 1)
    run("auth", "status", error="AUTH_REQUIRED", exit_status=3)
    assert run("auth", "logout")["session_removed"] is True
