from contextlib import contextmanager
from io import BytesIO
from zipfile import ZipFile

import click
import pytest
from click.testing import CliRunner

from rnacosctl.commands import register


class Console:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.requests = []
        self.output = []

    @contextmanager
    def client(self):
        yield self

    def request(self, method, path, **kwargs):
        self.requests.append((method, path, kwargs))
        return next(self.responses)

    def emit(self, data):
        self.output.append(data)


@pytest.fixture
def invoke():
    cli = click.Group("rnacosctl")
    register(cli)

    def run(console, args, stdin=None):
        return CliRunner().invoke(cli, args.split(), input=stdin, obj=console)

    return run


def test_config_set_preserves_line_endings(invoke):
    console = Console(True)
    result = invoke(console, "config set --namespace demo --data-id app.json --file -", b"{}\r\n")
    assert result.exit_code == 0, result.output
    assert console.requests == [
        (
            "POST",
            "/rnacos/api/console/v2/config/update",
            {
                "json": {
                    "dataId": "app.json",
                    "group": "DEFAULT_GROUP",
                    "tenant": "demo",
                    "content": "{}\r\n",
                }
            },
        )
    ]


def test_config_zip_import_stdin_sends_namespace_header(invoke):
    archive = BytesIO()
    with ZipFile(archive, "w") as handle:
        handle.writestr("DEFAULT_GROUP/app.json", "{}")
    content = archive.getvalue()
    console = Console(None)
    result = invoke(console, "config import --namespace demo --file - --yes", content)
    assert result.exit_code == 0, result.output
    assert console.requests == [
        (
            "POST",
            "/rnacos/api/console/v2/config/import",
            {
                "headers": {"tenant": "demo"},
                "files": {"file": ("config.zip", content, "application/zip")},
            },
        )
    ]


def test_config_selected_export_uses_v1_post_and_binary_stdout(invoke):
    console = Console(b"\x00binary\x01")
    result = invoke(console, "config export --keys-file - --output -", b'[{"dataId":"app.json"}]')
    assert result.exit_code == 0, result.output
    assert result.stdout_bytes == b"\x00binary\x01"
    assert console.requests == [
        (
            "POST",
            "/rnacos/api/console/config/download",
            {"json": [{"dataId": "app.json"}], "raw": True},
        )
    ]


def test_user_remove_checks_server_result(invoke):
    console = Console(True, {"list": [{"username": "admin"}]})
    unconfirmed = invoke(console, "user remove --username admin")
    assert unconfirmed.exit_code == 2
    assert console.requests == []
    failed = invoke(console, "user remove --username admin --yes")
    assert failed.exit_code == 1
    assert "did not remove" in failed.output
    assert console.output == []


def test_namespace_create_checks_explicit_id_before_upsert(invoke):
    console = Console([{"namespaceId": "dev", "namespaceName": "Development"}])
    result = invoke(console, "namespace add --id dev --name Different")
    assert result.exit_code == 1
    assert "already exists" in result.output
    assert console.requests == [("GET", "/rnacos/api/console/v2/namespaces/list", {})]


def test_mcp_update_requires_fields_that_server_would_reset(invoke):
    console = Console()
    result = invoke(console, "mcp-server update --file -", b'{"id":1,"description":"Changed"}')
    assert result.exit_code == 2
    assert "Specify namespace and tools explicitly" in result.output
    assert console.requests == []
