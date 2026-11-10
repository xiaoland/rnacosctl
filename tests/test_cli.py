import json
import os
import subprocess
import sys


def run_cli(tmp_path, *args):
    return subprocess.run(
        [sys.executable, "-m", "rnacosctl.cli", *args],
        env={**os.environ, "RNACOSCTL_HOME": str(tmp_path / "unused-state")},
        capture_output=True,
        check=False,
        text=True,
        timeout=10,
    )


def test_schema_runs_offline_and_describes_commands(tmp_path):
    result = run_cli(tmp_path, "schema")
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)["data"]
    assert data["schema_version"] == 1
    groups = {command["name"]: command for command in data["command"]["commands"]}
    assert {"auth", "profile", "config", "user", "mcp-server"} <= groups.keys()
    assert not (tmp_path / "unused-state").exists()
    result = run_cli(tmp_path, "schema", "config", "set")
    assert result.returncode == 0, result.stderr
    parameters = json.loads(result.stdout)["data"]["command"]["parameters"]
    assert any(p["required"] and "--file" in p["flags"] for p in parameters)


def test_confirmation_is_structured_and_does_not_touch_profiles(tmp_path):
    result = run_cli(tmp_path, "namespace", "remove", "--id", "example")
    assert result.returncode == 2
    assert not result.stdout
    assert json.loads(result.stderr)["error"]["code"] == "CONFIRMATION_REQUIRED"
    assert not (tmp_path / "unused-state").exists()


def test_click_errors_are_json_on_stderr(tmp_path):
    result = run_cli(tmp_path, "namespace", "add", "--id", "example")
    assert result.returncode == 2
    assert not result.stdout
    assert json.loads(result.stderr)["error"]["code"] == "USAGE_ERROR"


def test_auth_rejects_unapproved_cleartext_http(tmp_path):
    result = run_cli(tmp_path, "auth", "login", "--url", "http://10.0.0.2")
    assert result.returncode == 2
    error = json.loads(result.stderr)["error"]
    assert error["code"] == "USAGE_ERROR"
    assert "--allow-http" in error["hint"]
