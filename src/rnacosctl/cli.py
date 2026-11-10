"""Command entry point, profile selection and structured output."""

from __future__ import annotations

import ipaddress
import json
import math
import sys
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlsplit

import click

from . import commands
from .client import Captcha, Client, ClientError, normalize_url
from .profiles import Profile, ProfileError, ProfileStore


class CliError(Exception):
    def __init__(self, code: str, message: str, hint: str | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint


@dataclass
class Context:
    name: str | None
    timeout: float
    store: ProfileStore

    @staticmethod
    def emit(data: object) -> None:
        click.echo(json.dumps({"ok": True, "data": data}, ensure_ascii=False))

    def connect(self, profile: Profile, token: str | None = None) -> Client:
        return Client(
            profile.url,
            ca_bundle=profile.ca_bundle,
            token=token,
            timeout=self.timeout,
        )

    @contextmanager
    def client(self):
        profile = self.store.get(self.name)
        token = self.store.token(profile)
        with self.connect(profile, token) as client:
            yield client

    def target(
        self,
        url: str | None,
        username: str | None,
        ca_bundle: Path | None,
        allow_http: bool,
    ) -> Profile:
        name = self.name or self.store.active() or "default"
        try:
            previous = self.store.get(name)
        except ProfileError as error:
            if error.code != "PROFILE_NOT_FOUND":
                raise
            previous = None
        if not url and previous is None:
            raise CliError("USAGE_ERROR", "--url is required for a new profile.")
        base_url = normalize_url(url) if url else previous.url
        account = username or (previous.username if previous else "admin")
        if previous and (base_url, account) != (previous.url, previous.username):
            raise CliError(
                "PROFILE_CONFLICT",
                f"Profile '{name}' is bound to another URL or account.",
                "Choose a new --profile name or remove the existing profile first.",
            )
        origin = urlsplit(base_url)
        if origin.scheme == "http" and not allow_http:
            host = origin.hostname
            try:
                loopback = ipaddress.ip_address(host).is_loopback
            except ValueError:
                loopback = host == "localhost"
            if not loopback:
                raise CliError(
                    "USAGE_ERROR",
                    "Remote HTTP would send console credentials over a cleartext connection.",
                    "Use HTTPS or explicitly pass --allow-http on a trusted private network.",
                )
        return Profile(
            name,
            base_url,
            account,
            str(ca_bundle.resolve()) if ca_bundle else (previous.ca_bundle if previous else None),
        )


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(package_name="rnacosctl")
@click.option("--profile", envvar="RNACOSCTL_PROFILE", help="Select an instance for this command.")
@click.option(
    "--timeout",
    type=click.FloatRange(min=0, min_open=True),
    default=30.0,
    show_default=True,
    help="HTTP timeout in seconds.",
)
@click.pass_context
def cli(ctx: click.Context, profile: str | None, timeout: float) -> None:
    """Administer r-nacos. Run 'rnacosctl schema' for the machine-readable command tree."""
    if not math.isfinite(timeout):
        raise click.BadParameter("must be a finite number", param_hint="--timeout")
    ctx.obj = Context(profile, timeout, ProfileStore())


@cli.group()
def profile() -> None:
    """List, inspect, switch or remove saved instances."""


@profile.command("list")
@click.pass_obj
def profile_list(ctx: Context) -> None:
    """List saved instances and the active default."""
    active, profiles = ctx.store.snapshot()
    ctx.emit({"active": active, "profiles": [asdict(p) for p in profiles]})


@profile.command("show")
@click.argument("name", required=False)
@click.pass_obj
def profile_show(ctx: Context, name: str | None) -> None:
    """Show the connection settings for a saved instance."""
    ctx.emit(asdict(ctx.store.get(name or ctx.name)))


@profile.command("use")
@click.argument("name")
@click.pass_obj
def profile_use(ctx: Context, name: str) -> None:
    """Change the default instance for future commands."""
    ctx.emit(asdict(ctx.store.use(name)))


@profile.command("remove")
@click.argument("name")
@click.option("--yes", is_flag=True, help="Confirm removal of this local profile and session.")
@click.pass_obj
def profile_remove(ctx: Context, name: str, yes: bool) -> None:
    """Remove local connection settings and the saved session."""
    if not yes:
        raise CliError("CONFIRMATION_REQUIRED", "Removing a profile requires --yes.")
    ctx.emit(asdict(ctx.store.remove(name)))


@cli.group()
def auth() -> None:
    """Log in, inspect or revoke an r-nacos console session."""


def target_options(command):
    """Apply connection options shared by login and challenge commands."""
    command = click.option(
        "--allow-http", is_flag=True, help="Allow cleartext HTTP outside loopback."
    )(command)
    command = click.option(
        "--ca-bundle",
        type=click.Path(exists=True, dir_okay=False, readable=True, path_type=Path),
        help="PEM CA bundle for this instance.",
    )(command)
    command = click.option("--username", help="Console account (new profiles default to admin).")(
        command
    )
    return click.option("--url", help="Base URL of the console, e.g. https://config.example.org.")(
        command
    )


@auth.command("config")
@target_options
@click.pass_obj
def auth_config(ctx: Context, **target) -> None:
    """Discover the instance's console login settings, including its OAuth2 provider."""
    with ctx.connect(ctx.target(**target)) as client:
        ctx.emit(client.login_config())


@auth.command("challenge")
@target_options
@click.option(
    "--image",
    type=click.Path(dir_okay=False, path_type=Path),
    required=True,
    help="Write the server CAPTCHA image as PNG to this file.",
)
@click.pass_obj
def auth_challenge(ctx: Context, image: Path, **target) -> None:
    """Fetch a fresh CAPTCHA challenge without opening the web console."""
    with ctx.connect(ctx.target(**target)) as client:
        challenge = client.captcha()
    if challenge.image is not None:
        try:
            with image.open("xb") as output:
                output.write(challenge.image)
        except FileExistsError as error:
            raise CliError("FILE_EXISTS", "The CAPTCHA image path already exists.") from error
    ctx.emit(
        {
            "required": challenge.image is not None,
            "challenge_token": challenge.token,
            "image": str(image.resolve()) if challenge.image is not None else None,
            "expires_in": 300,
        }
    )


def read_secret(option: str, prompt: str, *, interactive: bool = False) -> str:
    stream = sys.stdin
    if interactive and stream.isatty():
        return click.prompt(prompt, hide_input=True, err=True)
    if interactive or stream.isatty():
        raise CliError("USAGE_ERROR", f"Pipe {prompt.lower()} to stdin and pass {option}.")
    value = stream.readline().rstrip("\r\n")
    if not value:
        raise CliError("USAGE_ERROR", f"No {prompt.lower()} received on stdin.")
    return value


@auth.command("login")
@target_options
@click.option("--password-stdin", is_flag=True, help="Read the console password from stdin.")
@click.option("--challenge-token", help="Challenge token from auth challenge.")
@click.option("--captcha", help="Answer for the supplied CAPTCHA challenge.")
@click.option(
    "--token-stdin", is_flag=True, help="Validate and import a console session token from stdin."
)
@click.option(
    "--oauth-code-stdin", is_flag=True, help="Read an OAuth2 authorization code from stdin."
)
@click.option("--oauth-state", help="State returned with the OAuth2 authorization code.")
@click.pass_obj
def auth_login(
    ctx: Context,
    password_stdin: bool,
    challenge_token: str | None,
    captcha: str | None,
    token_stdin: bool,
    oauth_code_stdin: bool,
    oauth_state: str | None,
    **target,
) -> None:
    """Authenticate and save the session under the selected profile."""
    if sum((password_stdin, token_stdin, oauth_code_stdin)) > 1:
        raise CliError("USAGE_ERROR", "Choose one stdin credential type.")
    if bool(challenge_token) != bool(captcha):
        raise CliError("USAGE_ERROR", "--challenge-token and --captcha must be supplied together.")
    if (token_stdin or oauth_code_stdin) and captcha:
        raise CliError("USAGE_ERROR", "CAPTCHA options apply to password login only.")
    if oauth_state and not oauth_code_stdin:
        raise CliError("USAGE_ERROR", "--oauth-state requires --oauth-code-stdin.")
    saved = ctx.target(**target)
    if token_stdin:
        token = read_secret("--token-stdin", "Console session token")
    else:
        with ctx.connect(saved) as client:
            if oauth_code_stdin:
                code = read_secret("--oauth-code-stdin", "OAuth2 authorization code")
                token = client.login_oauth2(code, state=oauth_state)
            else:
                challenge = Captcha(challenge_token, None) if challenge_token else client.captcha()
                if challenge.image is not None and not captcha:
                    raise CliError(
                        "CAPTCHA_REQUIRED",
                        "The instance requires a CAPTCHA answer.",
                        "Run auth challenge --image FILE, read the PNG, then pass "
                        "--challenge-token and --captcha to auth login.",
                    )
                password = read_secret(
                    "--password-stdin", "Console password", interactive=not password_stdin
                )
                token = client.login(saved.username, password, captcha=challenge, answer=captcha)
    with ctx.connect(saved, token) as client:
        account = client.whoami()
    if account["username"] != saved.username:
        if target["username"] or not (token_stdin or oauth_code_stdin):
            raise CliError("AUTH_ACCOUNT_MISMATCH", "The console returned an unexpected account.")
        saved = ctx.target(
            target["url"], account["username"], target["ca_bundle"], target["allow_http"]
        )
    ctx.store.save(saved, token)
    ctx.emit({"profile": saved.name, **account})


@auth.command("status")
@click.pass_obj
def auth_status(ctx: Context) -> None:
    """Verify the saved session and show its console identity and version."""
    saved = ctx.store.get(ctx.name)
    with ctx.connect(saved, ctx.store.token(saved)) as client:
        ctx.emit({"profile": saved.name, **client.whoami()})


@auth.command("logout")
@click.pass_obj
def auth_logout(ctx: Context) -> None:
    """Revoke the server session and retain local connection settings."""
    saved = ctx.store.get(ctx.name)
    token = ctx.store.token(saved)
    try:
        with ctx.connect(saved, token) as client:
            client.logout()
    except ClientError as error:
        if error.code not in {"AUTH_REQUIRED", "SESSION_EXPIRED"}:
            raise
    cleared = ctx.store.clear_session(saved, expected_token=token)
    ctx.emit({"profile": saved.name, "session_removed": cleared})


def parameter_schema(parameter: click.Parameter) -> dict:
    result = {
        "name": parameter.name,
        "type": parameter.type.name,
        "required": parameter.required,
        "multiple": parameter.multiple or parameter.nargs == -1,
    }
    if isinstance(parameter.type, click.Choice):
        result["choices"] = list(parameter.type.choices)
    if isinstance(parameter.type, (click.IntRange, click.FloatRange)):
        result["range"] = {
            "min": parameter.type.min,
            "min_open": parameter.type.min_open,
            "max": parameter.type.max,
            "max_open": parameter.type.max_open,
        }
    if isinstance(parameter, click.Option):
        default = parameter.default
        result.update(
            flags=parameter.opts + parameter.secondary_opts,
            help=parameter.help,
            default=default if isinstance(default, (str, int, float, bool, list, tuple)) else None,
            is_flag=parameter.is_flag,
        )
        if parameter.envvar:
            result["envvar"] = parameter.envvar
    return result


def command_schema(command: click.Command) -> dict:
    result = {
        "name": command.name,
        "help": command.help,
        "parameters": [
            parameter_schema(p) for p in command.params if not getattr(p, "hidden", False)
        ],
    }
    if isinstance(command, click.Group):
        result["commands"] = [
            command_schema(c) for _, c in sorted(command.commands.items()) if not c.hidden
        ]
    return result


@cli.command()
@click.argument("command", nargs=-1)
@click.pass_obj
def schema(ctx: Context, command: tuple[str, ...]) -> None:
    """Show JSON command and parameter metadata, with an optional command path."""
    selected: click.Command = cli
    for part in command:
        if not isinstance(selected, click.Group) or part not in selected.commands:
            raise CliError("USAGE_ERROR", f"Unknown command path: {' '.join(command)}")
        selected = selected.commands[part]
    ctx.emit({"schema_version": 1, "command": command_schema(selected)})


commands.register(cli)


def exit_code(code: str) -> int:
    if code in {
        "USAGE_ERROR",
        "CONFIRMATION_REQUIRED",
        "FILE_EXISTS",
        "INVALID_PROFILE",
        "INVALID_URL",
        "INVALID_CA_BUNDLE",
    }:
        return 2
    if code.startswith("AUTH_") or code in {
        "CAPTCHA_REQUIRED",
        "CAPTCHA_CHECK_ERROR",
        "INVALID_CAPTCHA",
        "INVALID_TOKEN",
        "LOGIN_LIMITE_ERROR",
        "OAUTH2_AUTH_ERROR",
        "OAUTH2_NOT_ENABLED",
        "PERMISSION_DENIED",
        "SAVE_SESSION_ERROR",
        "SESSION_EXPIRED",
        "USER_CHECK_ERROR",
    }:
        return 3
    return 1


def main() -> None:
    try:
        status = cli.main(prog_name="rnacosctl", standalone_mode=False)
    except commands.ConfirmationRequired as error:
        fail("CONFIRMATION_REQUIRED", error.format_message())
    except click.UsageError as error:
        hint = "Run rnacosctl schema or the command with --help."
        fail("USAGE_ERROR", error.format_message(), hint)
    except click.FileError as error:
        fail("IO_ERROR", error.format_message())
    except click.ClickException as error:
        fail("OPERATION_FAILED", error.format_message())
    except (CliError, ClientError, ProfileError) as error:
        hint = getattr(error, "hint", None)
        if error.code in {"AUTH_REQUIRED", "SESSION_EXPIRED"} and not hint:
            hint = "Run auth login with the same --profile."
        fail(error.code, error.message, hint, getattr(error, "status", None))
    except OSError as error:
        if isinstance(error, BrokenPipeError):
            raise SystemExit(1) from None
        fail("IO_ERROR", error.strerror or "Could not read or write a local file.")
    except (click.Abort, KeyboardInterrupt):
        fail("INTERRUPTED", "The command was interrupted.", status_code=130)
    if isinstance(status, int):
        raise SystemExit(status)


def fail(
    code: str,
    message: str,
    hint: str | None = None,
    http_status: int | None = None,
    status_code: int | None = None,
) -> None:
    error: dict = {"code": code, "message": message}
    if hint:
        error["hint"] = hint
    if http_status:
        error["http_status"] = http_status
    click.echo(json.dumps({"ok": False, "error": error}, ensure_ascii=False), err=True)
    raise SystemExit(status_code or exit_code(code))


if __name__ == "__main__":
    main()
