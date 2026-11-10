"""Local console profiles and their separate bearer session files."""

from __future__ import annotations

import json
import os
import re
import stat
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from uuid import uuid4

from filelock import FileLock, Timeout
from platformdirs import user_config_path

_PROFILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_SESSION_ID = re.compile(r"[0-9a-f]{32}")


class ProfileError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Profile:
    name: str
    url: str
    username: str
    ca_bundle: str | None = None


@dataclass(frozen=True)
class _SavedProfile:
    profile: Profile
    session: str | None


@dataclass
class _State:
    active: str | None
    profiles: dict[str, _SavedProfile]


class ProfileStore:
    def __init__(self, root: Path | None = None) -> None:
        if root is None:
            override = os.environ.get("RNACOSCTL_HOME")
            root = Path(override) if override else user_config_path("rnacosctl", appauthor=False)
        self.root = root.expanduser().absolute()
        self.state_path = self.root / "profiles.json"

    def save(self, profile: Profile, token: str) -> Profile:
        if not _valid_profile(profile):
            raise ProfileError(
                "INVALID_PROFILE",
                "Use a 1–64 character profile name starting with a letter or number. "
                "Names can contain letters, numbers, periods, underscores, and hyphens. "
                "A URL and username are required.",
            )
        if not token or "\r" in token or "\n" in token:
            raise ProfileError("AUTH_REQUIRED", "Login did not return a valid console session.")
        with self._locked():
            state = self._load()
            existing = state.profiles.get(profile.name)
            if existing and (existing.profile.url, existing.profile.username) != (
                profile.url,
                profile.username,
            ):
                raise ProfileError(
                    "PROFILE_CONFLICT",
                    f"Profile {profile.name!r} already belongs to another URL or user. "
                    "Choose a new name or remove the existing profile first.",
                )
            session = uuid4().hex
            fd = os.open(self._session_path(session), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump({"profile": asdict(profile), "token": token}, stream)
                stream.flush()
                os.fsync(stream.fileno())
            # Publishing the profile is the commit point for its new session.
            state.profiles[profile.name] = _SavedProfile(profile, session)
            if state.active is None:
                state.active = profile.name
            self._write(state)
            self._prune_sessions(state)
        return profile

    def get(self, name: str | None = None) -> Profile:
        with self._locked():
            return self._lookup(self._load(), name).profile

    def token(self, profile: Profile) -> str:
        with self._locked():
            record = self._load().profiles.get(profile.name)
            if record and record.profile == profile and record.session:
                token = self._read_session(record.session, profile)
                if token:
                    return token
        raise ProfileError(
            "AUTH_REQUIRED", f"No console session saved for profile {profile.name!r}."
        )

    def clear_session(self, profile: Profile, expected_token: str | None = None) -> bool:
        with self._locked():
            state = self._load()
            record = state.profiles.get(profile.name)
            if not record or record.profile != profile or not record.session:
                return False
            if (
                expected_token is not None
                and self._read_session(record.session, profile) != expected_token
            ):
                return False
            state.profiles[profile.name] = _SavedProfile(profile, None)
            self._write(state)
            self._prune_sessions(state)
            return True

    def list(self) -> list[Profile]:
        return self.snapshot()[1]

    def snapshot(self) -> tuple[str | None, list[Profile]]:
        with self._locked():
            state = self._load()
            profiles = [state.profiles[name].profile for name in sorted(state.profiles)]
            return state.active, profiles

    def active(self) -> str | None:
        with self._locked():
            return self._load().active

    def use(self, name: str) -> Profile:
        with self._locked():
            state = self._load()
            profile = self._lookup(state, name).profile
            state.active = profile.name
            self._write(state)
            return profile

    def remove(self, name: str) -> Profile:
        with self._locked():
            state = self._load()
            profile = self._lookup(state, name).profile
            del state.profiles[name]
            if state.active == name:
                state.active = None
            self._write(state)
            self._prune_sessions(state)
            return profile

    @contextmanager
    def _locked(self) -> Iterator[None]:
        try:
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
            info = self.root.lstat()
            if not stat.S_ISDIR(info.st_mode) or (
                os.name == "posix" and (info.st_uid != os.getuid() or info.st_mode & 0o022)
            ):
                raise ProfileError(
                    "STATE_ERROR",
                    "Profile directory needs current-user ownership and private write permissions.",
                )
            with FileLock(self.root / "profiles.lock", timeout=10, mode=0o600):
                yield
        except Timeout as exc:
            raise ProfileError(
                "STATE_BUSY", "Profile store is busy. Try the command again."
            ) from exc
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ProfileError(
                "STATE_ERROR", "Could not read or write the local profile store."
            ) from exc

    def _load(self) -> _State:
        try:
            with self.state_path.open(encoding="utf-8") as stream:
                data = json.load(stream)
        except FileNotFoundError:
            return _State(None, {})
        try:
            profiles = {}
            for name, row in data["profiles"].items():
                profile = Profile(name, row["url"], row["username"], row["ca_bundle"])
                session = row["session"]
                if not _valid_profile(profile) or (
                    session is not None and not _SESSION_ID.fullmatch(session)
                ):
                    raise ValueError
                profiles[name] = _SavedProfile(profile, session)
            active = data["active"]
            if active is not None and active not in profiles:
                raise ValueError
            return _State(active, profiles)
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            raise ProfileError("STATE_ERROR", "Local profile metadata is invalid.") from exc

    def _write(self, state: _State) -> None:
        profiles = {
            name: {
                "url": row.profile.url,
                "username": row.profile.username,
                "ca_bundle": row.profile.ca_bundle,
                "session": row.session,
            }
            for name, row in state.profiles.items()
        }
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.root, delete=False
            ) as stream:
                temp_path = Path(stream.name)
                json.dump({"active": state.active, "profiles": profiles}, stream, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_path, self.state_path)
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)

    def _session_path(self, session: str) -> Path:
        return self.root / f"session-{session}"

    def _read_session(self, session: str, profile: Profile) -> str | None:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        try:
            fd = os.open(self._session_path(session), flags)
        except FileNotFoundError:
            return None
        with os.fdopen(fd, encoding="utf-8") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or (
                os.name == "posix" and (info.st_uid != os.getuid() or info.st_mode & 0o077)
            ):
                raise ProfileError(
                    "STATE_ERROR", "Session file needs current-user ownership and private access."
                )
            row = json.load(stream)
            token = row.get("token") if isinstance(row, dict) else None
            if not isinstance(token, str) or not token or "\r" in token or "\n" in token:
                raise ProfileError("STATE_ERROR", "Local console session is invalid.")
            return token if row.get("profile") == asdict(profile) else None

    def _prune_sessions(self, state: _State) -> None:
        in_use = {row.session for row in state.profiles.values()}
        for path in self.root.glob("session-*"):
            session = path.name.removeprefix("session-")
            if _SESSION_ID.fullmatch(session) and session not in in_use:
                path.unlink(missing_ok=True)

    @staticmethod
    def _lookup(state: _State, name: str | None) -> _SavedProfile:
        if name is None:
            name = state.active
        if name is None:
            raise ProfileError(
                "PROFILE_NOT_FOUND", "No active profile. Log in or select a profile first."
            )
        if name not in state.profiles:
            raise ProfileError("PROFILE_NOT_FOUND", f"Profile {name!r} does not exist.")
        return state.profiles[name]


def _valid_profile(profile: Profile) -> bool:
    return (
        isinstance(profile.name, str)
        and _PROFILE_NAME.fullmatch(profile.name) is not None
        and isinstance(profile.url, str)
        and bool(profile.url)
        and isinstance(profile.username, str)
        and bool(profile.username)
        and (profile.ca_bundle is None or isinstance(profile.ca_bundle, str))
    )
