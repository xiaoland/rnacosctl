"""Resource commands for the r-nacos console API."""

from __future__ import annotations

import io
import json
import os
import zipfile
from pathlib import Path
from uuid import uuid4

import click

CONSOLE = "/rnacos/api/console"
V2 = f"{CONSOLE}/v2"
DEFAULT_GROUP = "DEFAULT_GROUP"
POSITIVE = click.IntRange(min=1)
NONNEGATIVE = click.IntRange(min=0)
PAGE_SIZE = click.IntRange(1, 1000)


class ConfirmationRequired(click.UsageError):
    """A command requires explicit authorization with --yes."""


def _option(flag, field, help_text, **kwargs):
    return click.Option([flag, field], help=help_text, **kwargs)


def _group(root, name, help_text):
    group = click.Group(name, help=help_text)
    root.add_command(group)
    return group


def _command(group, name, help_text, callback, *options, destructive=False):
    if destructive:
        options = (
            *options,
            _option("--yes", "yes", "Explicitly authorize this change.", is_flag=True),
        )

    def invoke(**values):
        if destructive and not values.pop("yes"):
            raise ConfirmationRequired("This operation requires --yes.")
        callback(click.get_current_context().obj, **values)

    group.add_command(click.Command(name, help=help_text, callback=invoke, params=list(options)))


def _present(values):
    return {key: value for key, value in values.items() if value is not None}


def _action(group, name, path, help_text, *options, method="GET", prepare=None, destructive=False):
    def invoke(state, **values):
        payload = _present(values)
        if prepare is not None:
            payload = prepare(payload)
        request = {"params" if method == "GET" else "json": payload}
        with state.client() as client:
            result = client.request(method, path, **request)
        state.emit(result)

    _command(group, name, help_text, invoke, *options, destructive=destructive)


def _namespace(field="namespaceId", *, default=None):
    return _option("--namespace", field, "Namespace ID.", default=default, show_default=True)


def _page():
    return (
        _option(
            "--page",
            "pageNo",
            "One-based page number.",
            type=POSITIVE,
            default=1,
            show_default=True,
        ),
        _option(
            "--page-size",
            "pageSize",
            "Maximum records per page.",
            type=PAGE_SIZE,
            default=20,
            show_default=True,
        ),
    )


def _input(help_text, *, flag="--file", field="source", required=True):
    return _option(flag, field, help_text, type=click.File("rb"), required=required)


def _output():
    return _option(
        "--output",
        "output",
        "New destination file or - for binary stdout.",
        type=click.Path(dir_okay=False, path_type=Path),
        required=True,
    )


def _text(source):
    try:
        return source.read().decode("utf-8")
    except UnicodeDecodeError as exc:
        raise click.UsageError("Input must use UTF-8 encoding.") from exc


def _json_file(source, expected=dict):
    try:
        value = json.loads(_text(source))
    except ValueError as exc:
        raise click.UsageError("Input must be valid JSON.") from exc
    if not isinstance(value, expected):
        kind = "object" if expected is dict else "array"
        raise click.UsageError(f"Input must contain a JSON {kind}.")
    return value


def _write_download(state, output, content):
    if output == Path("-"):
        click.echo(content, nl=False)
        return
    try:
        fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
    except OSError as exc:
        raise click.FileError(str(output), hint=exc.strerror) from exc
    state.emit({"file": str(output), "bytes": len(content)})


def _download(group, name, path, help_text, *options):
    def download(state, output, **values):
        with state.client() as client:
            content = client.request("GET", path, params=_present(values), raw=True)
        _write_download(state, output, content)

    _command(group, name, help_text, download, *options, _output())


def _uses_stdin(source):
    return getattr(source, "name", "<stdin>") in ("<stdin>", "-")


def _upload(client, path, source, filename, mime_type, *, headers=None, data=None):
    if _uses_stdin(source):
        source, name = source.read(), filename
    else:
        name = Path(source.name).name
    return client.request(
        "POST",
        path,
        headers=headers,
        data=data,
        files={"file": (name, source, mime_type)},
    )


def _register_namespaces(root):
    group = _group(root, "namespace", "Manage namespace IDs and display names.")
    namespace_id = _option("--id", "namespaceId", "Namespace ID.", required=True)
    name = _option("--name", "namespaceName", "Namespace display name.", required=True)
    base = f"{V2}/namespaces"
    _action(group, "list", f"{base}/list", "List visible namespaces.")

    def add_namespace(state, namespaceId, namespaceName):
        if namespaceId == "public":
            raise click.UsageError("The public namespace already exists.")
        payload = {"namespaceId": namespaceId or str(uuid4()), "namespaceName": namespaceName}
        with state.client() as client:
            if namespaceId:
                namespaces = client.request("GET", f"{base}/list")
                if any(item["namespaceId"] == namespaceId for item in namespaces):
                    raise click.ClickException(f"Namespace {namespaceId} already exists.")
            client.request("POST", f"{base}/add", json=payload)
        state.emit(payload)

    _command(
        group,
        "add",
        "Create a namespace and return its ID and name.",
        add_namespace,
        _option("--id", "namespaceId", "Namespace ID. A UUID is generated when omitted."),
        name,
    )
    _action(
        group, "update", f"{base}/update", "Rename a namespace.", namespace_id, name, method="POST"
    )
    _action(
        group,
        "remove",
        f"{base}/remove",
        "Delete an empty namespace.",
        namespace_id,
        method="POST",
        destructive=True,
    )


def _register_configs(root):
    group = _group(root, "config", "Read, publish, version, import, and export configuration.")
    base = f"{V2}/config"
    key = (
        _option("--data-id", "dataId", "Configuration data ID.", required=True),
        _option(
            "--group", "group", "Configuration group.", default=DEFAULT_GROUP, show_default=True
        ),
        _namespace("tenant"),
    )
    filters = (
        _namespace("tenant"),
        _option("--group-filter", "groupParam", "Match a configuration group."),
        _option("--data-id-filter", "dataParam", "Match a configuration data ID."),
    )
    config_type = _option("--type", "configType", "Content type such as json, yaml, or text.")
    desc = _option("--description", "desc", "Configuration description.")
    _action(group, "list", f"{base}/list", "List configurations and metadata.", *filters, *_page())
    _action(group, "get", f"{base}/info", "Read configuration content and metadata.", *key)
    _action(
        group,
        "history",
        f"{base}/history",
        "List historical revisions and their contents.",
        *key,
        *_page(),
    )
    _action(
        group,
        "remove",
        f"{base}/remove",
        "Delete a configuration.",
        *key,
        method="POST",
        destructive=True,
    )

    def publish(values):
        values["content"] = _text(values.pop("source"))
        return values

    _action(
        group,
        "set",
        f"{base}/update",
        "Publish configuration content from a UTF-8 file.",
        *key,
        _input("UTF-8 content file or - for stdin."),
        config_type,
        desc,
        method="POST",
        prepare=publish,
    )

    def restore(state, revision, **values):
        target = _present(values)
        with state.client() as client:
            history = client.request("GET", f"{base}/history", params=target)
            for item in history["list"]:
                if item["id"] == revision:
                    result = client.request(
                        "POST", f"{base}/update", json={**target, "content": item["content"]}
                    )
                    break
            else:
                raise click.ClickException(f"Configuration revision {revision} was not found.")
        state.emit(result)

    _command(
        group,
        "restore",
        "Publish the content of a historical revision.",
        restore,
        *key,
        _option(
            "--revision",
            "revision",
            "Revision ID from config history.",
            type=POSITIVE,
            required=True,
        ),
        destructive=True,
    )

    def export_configs(state, output, keys_file, **values):
        filters = _present(values)
        if keys_file is not None and filters:
            raise click.UsageError(
                "--keys-file cannot be combined with namespace or filter options."
            )
        keys = _json_file(keys_file, list) if keys_file is not None else None
        if keys == []:
            raise click.UsageError("--keys-file must contain at least one key.")
        with state.client() as client:
            if keys is None:
                content = client.request("GET", f"{base}/download", params=filters, raw=True)
            else:
                content = client.request("POST", f"{CONSOLE}/config/download", json=keys, raw=True)
        _write_download(state, output, content)

    _command(
        group,
        "export",
        "Export a ZIP archive by filters or exact configuration keys.",
        export_configs,
        *filters,
        _output(),
        _input(
            "JSON array of exact keys with dataId, group, and tenant fields, or - for stdin.",
            flag="--keys-file",
            field="keys_file",
            required=False,
        ),
    )

    def import_configs(state, source, tenant):
        content = source.read()
        if not zipfile.is_zipfile(io.BytesIO(content)):
            raise click.UsageError("--file must contain a ZIP archive.")
        headers = {"tenant": tenant} if tenant is not None else None
        name = "config.zip" if _uses_stdin(source) else Path(source.name).name
        with state.client() as client:
            result = client.request(
                "POST",
                f"{base}/import",
                headers=headers,
                files={"file": (name, content, "application/zip")},
            )
        state.emit(result)

    _command(
        group,
        "import",
        "Import configuration ZIP entries named group/data-id.",
        import_configs,
        _namespace("tenant"),
        _input("Configuration ZIP file or - for stdin."),
        destructive=True,
    )


def _password(source):
    password = _text(source).removesuffix("\n").removesuffix("\r")
    if not password:
        raise click.UsageError("The password file must contain a non-empty password.")
    return password


def _register_users(root):
    group = _group(root, "user", "Manage users, roles, namespace access, and passwords.")
    base = f"{V2}/user"
    username = _option("--username", "username", "Account username.", required=True)
    _action(group, "info", f"{base}/info", "Show the current console user.")
    _action(
        group,
        "permissions",
        f"{base}/web_resources",
        "Show console permissions and server version.",
    )
    _action(
        group,
        "list",
        f"{base}/list",
        "List accounts, roles, and namespace access.",
        _option("--username-filter", "likeUsername", "Match an account username."),
        _option("--reverse", "isRev", "Reverse account order.", is_flag=True),
        *_page(),
    )

    def find_user(client, name):
        page = client.request("GET", f"{base}/list", params={"likeUsername": name})
        return next((item for item in page["list"] if item["username"] == name), None)

    def write_user(
        state, *, create, username, password_file, roles, allow_namespace, access_file, **values
    ):
        payload = {"username": username, **_present(values)}
        if allow_namespace and access_file is not None:
            raise click.UsageError(
                "--allow-namespace and --namespace-access-file are mutually exclusive."
            )
        if (
            access_file is not None
            and password_file is not None
            and all(_uses_stdin(source) for source in (access_file, password_file))
        ):
            raise click.UsageError("At most one input file can be read from stdin.")
        if allow_namespace:
            payload["namespacePrivilegeParam"] = {
                "whitelistIsAll": False,
                "whitelist": list(allow_namespace),
            }
        if access_file is not None:
            payload["namespacePrivilegeParam"] = _json_file(access_file)
        if password_file is not None:
            payload["password"] = _password(password_file)
        if roles:
            role_values = {"admin": "0", "developer": "1", "visitor": "2"}
            payload["roles"] = ",".join(role_values[role] for role in roles)
        if len(payload) == 1:
            raise click.UsageError("Specify at least one account change.")
        with state.client() as client:
            existing = find_user(client, username)
            if create and existing is not None:
                raise click.ClickException(f"User {username} already exists.")
            if not create and existing is None:
                raise click.ClickException(f"User {username} was not found.")
            action = "add" if create else "update"
            client.request("POST", f"{base}/{action}", json=payload)
            saved = find_user(client, username)
        if saved is None:
            raise click.ClickException(f"The server did not save user {username}.")
        for field in ("nickname", "enable"):
            if field in payload and saved.get(field) != payload[field]:
                raise click.ClickException(
                    f"The server did not save the requested {field} for user {username}."
                )
        if roles and set(saved.get("roles") or []) != set(payload["roles"].split(",")):
            raise click.ClickException(
                f"The server did not save the requested roles for user {username}."
            )
        for field, value in payload.get("namespacePrivilegeParam", {}).items():
            saved_value = (saved.get("namespacePrivilege") or {}).get(field)
            if field in {"whitelist", "blacklist"} and isinstance(value, list):
                saved_value, value = set(saved_value or []), set(value)
            if (
                field in {"whitelist", "blacklist", "whitelistIsAll", "blacklistIsAll"}
                and value is not None
                and saved_value != value
            ):
                raise click.ClickException(
                    f"The server did not save namespace access for user {username}."
                )
        state.emit(saved)

    def user_options(create):
        return (
            username,
            _option("--nickname", "nickname", "Account display name."),
            _option(
                "--enabled/--disabled", "enable", "Whether the account can log in.", default=None
            ),
            _option(
                "--role",
                "roles",
                "Account role. Repeat to grant multiple roles.",
                type=click.Choice(["admin", "developer", "visitor"]),
                multiple=True,
                required=create,
            ),
            _option(
                "--allow-namespace",
                "allow_namespace",
                "Limit the whitelist to this namespace. Repeat for more namespaces.",
                multiple=True,
            ),
            _input(
                "JSON namespace access object with whitelistIsAll, whitelist, blacklistIsAll, or blacklist fields.",
                flag="--namespace-access-file",
                field="access_file",
                required=False,
            ),
            _input(
                "Password file or - for stdin. One trailing line ending is removed.",
                flag="--password-file",
                field="password_file",
                required=create,
            ),
        )

    _command(
        group,
        "add",
        "Create an account and read it back.",
        lambda state, **values: write_user(state, create=True, **values),
        *user_options(True),
    )
    _command(
        group,
        "update",
        "Update an account and read it back.",
        lambda state, **values: write_user(state, create=False, **values),
        *user_options(False),
    )

    def remove_user(state, username):
        with state.client() as client:
            client.request("POST", f"{base}/remove", json={"username": username})
            if find_user(client, username) is not None:
                raise click.ClickException(f"The server did not remove user {username}.")
        state.emit(True)

    _command(group, "remove", "Delete an account.", remove_user, username, destructive=True)

    def reset_password(state, old_password, new_password):
        if all(_uses_stdin(source) for source in (old_password, new_password)):
            raise click.UsageError("At most one password file can be read from stdin.")
        payload = {"oldPassword": _password(old_password), "newPassword": _password(new_password)}
        with state.client() as client:
            result = client.request("POST", f"{base}/reset_password", json=payload)
        state.emit(result)

    _command(
        group,
        "reset-password",
        "Change the password of the current account.",
        reset_password,
        _input(
            "Current password file or - for stdin.",
            flag="--old-password-file",
            field="old_password",
        ),
        _input(
            "New password file or - for stdin.", flag="--new-password-file", field="new_password"
        ),
        destructive=True,
    )


def _naming_key():
    return (
        _option("--service", "serviceName", "Service name.", required=True),
        _option("--group", "groupName", "Service group.", default=DEFAULT_GROUP, show_default=True),
        _namespace(),
    )


def _naming_payload(values):
    if "metadata_file" in values:
        metadata = _json_file(values.pop("metadata_file"))
        if not all(isinstance(value, str) for value in metadata.values()):
            raise click.UsageError("Metadata values must be strings.")
        values["metadata"] = json.dumps(metadata, ensure_ascii=False)
    if "ephemeral" in values:
        values["ephemeral"] = str(values["ephemeral"]).lower()
    return values


def _metadata_file():
    return _input(
        "JSON object of string metadata values or - for stdin.",
        flag="--metadata-file",
        field="metadata_file",
        required=False,
    )


def _register_naming(root):
    service = _group(root, "service", "Manage discovered services and inspect subscribers.")
    service_base = f"{V2}/service"
    filters = (
        _namespace(),
        _option("--group-filter", "groupNameParam", "Match a service group."),
        _option("--service-filter", "serviceNameParam", "Match a service name."),
        *_page(),
    )
    _action(service, "list", f"{service_base}/list", "List services.", *filters)
    _action(
        service,
        "subscribers",
        f"{service_base}/subscriber/list",
        "List subscribers on a cluster node.",
        *filters,
        _option("--node-id", "nodeId", "Cluster node ID.", type=POSITIVE),
    )

    def get_service(state, serviceName, groupName, namespaceId):
        params = _present(
            {"serviceName": f"{groupName}@@{serviceName}", "namespaceId": namespaceId}
        )
        with state.client() as client:
            result = client.request("GET", f"{CONSOLE}/ns/service", params=params)
        state.emit(result)

    _command(service, "get", "Show the details of one service.", get_service, *_naming_key())
    for name, summary in (
        ("add", "Create a service."),
        ("update", "Update service metadata and protection."),
    ):
        _action(
            service,
            name,
            f"{service_base}/{name}",
            summary,
            *_naming_key(),
            _metadata_file(),
            _option(
                "--protect-threshold",
                "protectThreshold",
                "Protection threshold from 0 to 1.",
                type=click.FloatRange(0, 1),
            ),
            method="POST",
            prepare=_naming_payload,
        )
    _action(
        service,
        "remove",
        f"{service_base}/remove",
        "Delete a service.",
        *_naming_key(),
        method="POST",
        destructive=True,
    )

    instance = _group(root, "instance", "Manage IP and port entries in the service registry.")
    instance_base = f"{V2}/instance"
    instance_key = (
        *_naming_key(),
        _option("--ip", "ip", "Instance IP address.", required=True),
        _option(
            "--port", "port", "Instance TCP port.", type=click.IntRange(1, 65535), required=True
        ),
        _option("--cluster", "clusterName", "Instance cluster name."),
    )
    _action(
        instance, "list", f"{instance_base}/list", "List instances of a service.", *_naming_key()
    )
    _action(instance, "get", f"{instance_base}/info", "Show one service instance.", *instance_key)
    for name, summary in (
        ("add", "Register a service instance."),
        ("update", "Update a service instance."),
    ):
        _action(
            instance,
            name,
            f"{instance_base}/{name}",
            summary,
            *instance_key,
            _option("--weight", "weight", "Instance weight.", type=click.FloatRange(min=0)),
            _option(
                "--enabled/--disabled", "enabled", "Whether the instance is enabled.", default=None
            ),
            _option("--ephemeral/--persistent", "ephemeral", "Instance persistence.", default=None),
            _metadata_file(),
            method="POST",
            prepare=_naming_payload,
        )
    _action(
        instance,
        "remove",
        f"{instance_base}/remove",
        "Deregister a service instance.",
        *instance_key,
        method="POST",
        destructive=True,
    )


def _register_operations(root):
    cluster = _group(root, "cluster", "Inspect r-nacos cluster nodes.")
    _action(
        cluster,
        "nodes",
        f"{V2}/cluster/cluster_node_list",
        "List cluster nodes and the current leader.",
    )

    metrics = _group(root, "metrics", "Query historical server metrics.")

    def prepare_timeline(values):
        keys = values.pop("keys")
        if keys:
            values["stringKey"] = ",".join(keys)
        return values

    _action(
        metrics,
        "timeline",
        f"{V2}/metrics/timeline",
        "Query a timeline for a node and selected metrics.",
        _option("--node-id", "nodeId", "Cluster node ID.", type=POSITIVE),
        _option("--start-time", "startTime", "Start time as Unix milliseconds.", type=NONNEGATIVE),
        _option(
            "--interval",
            "timelineGroupName",
            "Sample resolution.",
            type=click.Choice(["LEAST", "MINUTE"]),
        ),
        _option(
            "--key",
            "keys",
            "Metric key, for example app_cpu_usage. Repeat for more keys.",
            multiple=True,
        ),
        prepare=prepare_timeline,
    )

    transfer = _group(root, "transfer", "Export and restore r-nacos backup data.")
    transfer_base = f"{CONSOLE}/transfer"
    _download(transfer, "export", f"{transfer_base}/export", "Download a full instance backup.")

    def restore_backup(state, source, config, users, cache):
        headers = {
            "import-config": "1" if config else "0",
            "import-user": "1" if users else "0",
            "import-cache": "1" if cache else "0",
        }
        with state.client() as client:
            result = _upload(
                client,
                f"{transfer_base}/import",
                source,
                "backup.data",
                "application/octet-stream",
                headers=headers,
            )
        state.emit(result)

    _command(
        transfer,
        "import",
        "Restore a backup including service registry and MCP data.",
        restore_backup,
        _input("Backup file or - for stdin."),
        _option(
            "--config/--no-config",
            "config",
            "Restore configurations.",
            default=True,
            show_default=True,
        ),
        _option(
            "--users/--no-users", "users", "Restore accounts.", default=True, show_default=True
        ),
        _option(
            "--cache/--no-cache",
            "cache",
            "Restore cache data including sessions.",
            default=False,
            show_default=True,
        ),
        destructive=True,
    )


def _register_mcp(root):
    specs = _group(root, "tool-spec", "Manage MCP tool schemas and versions.")
    spec_base = f"{V2}/mcp/toolspec"
    spec_filters = (
        _namespace(),
        _option("--group-filter", "groupFilter", "Match a tool group."),
        _option("--name-filter", "toolNameFilter", "Match a tool name."),
    )
    spec_key = (
        _namespace("namespace", default=""),
        _option("--group", "group", "Tool group.", required=True),
        _option("--name", "toolName", "Tool name.", required=True),
    )
    _action(
        specs, "list", f"{spec_base}/list", "List tool specifications.", *spec_filters, *_page()
    )
    _action(
        specs, "get", f"{spec_base}/info", "Show a tool specification and its versions.", *spec_key
    )

    def tool_function(values):
        values["function"] = _json_file(values.pop("source"))
        return values

    _action(
        specs,
        "set",
        f"{spec_base}/update",
        "Publish a tool specification from a JSON function.",
        *spec_key,
        _input("JSON object with name, description, and inputSchema fields, or - for stdin."),
        method="POST",
        prepare=tool_function,
    )
    _action(
        specs,
        "batch-update",
        f"{spec_base}/batch_update",
        "Publish multiple tool specifications.",
        _input(
            "JSON array of objects with namespace, group, toolName, and function fields, or - for stdin."
        ),
        method="POST",
        prepare=lambda values: _json_file(values["source"], list),
    )
    _action(
        specs,
        "remove",
        f"{spec_base}/remove",
        "Delete a tool specification.",
        *spec_key,
        method="POST",
        destructive=True,
    )
    _download(
        specs,
        "export",
        f"{spec_base}/download",
        "Export tool specifications as YAML.",
        *spec_filters,
    )

    servers = _group(
        root, "mcp-server", "Manage MCP servers, tool routing, and published revisions."
    )
    server_base = f"{V2}/mcp/server"
    server_filters = (_namespace(), _option("--name-filter", "nameFilter", "Match a server name."))
    server_id = _option("--id", "id", "Numeric MCP server ID.", type=POSITIVE, required=True)
    _action(servers, "list", f"{server_base}/list", "List MCP servers.", *server_filters, *_page())
    _action(servers, "get", f"{server_base}/info", "Read an MCP server definition.", server_id)
    _action(
        servers,
        "add",
        f"{server_base}/add",
        "Create an MCP server from a JSON definition.",
        _input(
            "JSON with name, authKeys, and optional uniqueKey, namespace, description, and tools."
        ),
        method="POST",
        prepare=lambda values: _json_file(values["source"]),
    )

    def server_update(values):
        definition = _json_file(values["source"])
        if not isinstance(definition.get("namespace"), str) or not isinstance(
            definition.get("tools"), list
        ):
            raise click.UsageError(
                "Specify namespace and tools explicitly. Use an empty namespace for public "
                "or an empty tools array to remove all tools."
            )
        return definition

    _action(
        servers,
        "update",
        f"{server_base}/update",
        "Update an MCP server definition and replace its tool list.",
        _input(
            "JSON with id, namespace, tools, and optional name, authKeys, description, and uniqueKey."
        ),
        method="POST",
        prepare=server_update,
    )
    _action(
        servers,
        "remove",
        f"{server_base}/remove",
        "Delete an MCP server.",
        server_id,
        method="POST",
        destructive=True,
    )
    _action(
        servers,
        "history",
        f"{server_base}/history",
        "List revisions of an MCP server.",
        server_id,
        *_page(),
        _option(
            "--start-time",
            "startTime",
            "Earliest timestamp in Unix milliseconds.",
            type=NONNEGATIVE,
        ),
        _option(
            "--end-time", "endTime", "Latest timestamp in Unix milliseconds.", type=NONNEGATIVE
        ),
    )
    _action(
        servers,
        "publish",
        f"{server_base}/publish",
        "Publish the current revision of an MCP server.",
        server_id,
        method="POST",
    )
    _action(
        servers,
        "publish-history",
        f"{server_base}/publish/history",
        "Publish a historical MCP server revision.",
        server_id,
        _option(
            "--revision",
            "historyValueId",
            "Revision ID from mcp-server history.",
            type=POSITIVE,
            required=True,
        ),
        method="POST",
        destructive=True,
    )
    _download(
        servers,
        "export",
        f"{server_base}/download",
        "Export MCP server definitions as YAML.",
        *server_filters,
    )

    def register_import(group, base, help_text):
        def import_yaml(state, source, namespace):
            data = {"namespace": namespace} if namespace is not None else None
            with state.client() as client:
                result = _upload(
                    client,
                    f"{base}/import",
                    source,
                    "import.yaml",
                    "application/yaml",
                    data=data,
                )
            state.emit(result)

        _command(
            group,
            "import",
            help_text,
            import_yaml,
            _namespace("namespace"),
            _input("YAML file or - for stdin."),
            destructive=True,
        )

    register_import(specs, spec_base, "Import tool specifications from YAML.")
    register_import(servers, server_base, "Import MCP server definitions from YAML.")


def register(root: click.Group) -> None:
    """Register the administration commands on the CLI."""
    _register_namespaces(root)
    _register_configs(root)
    _register_users(root)
    _register_naming(root)
    _register_operations(root)
    _register_mcp(root)
