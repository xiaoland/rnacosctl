import json
import os
import stat
from concurrent.futures import ThreadPoolExecutor

import pytest

from rnacosctl.profiles import Profile, ProfileError, ProfileStore


def _saved_tokens(root):
    return [json.loads(path.read_text())["token"] for path in root.glob("session-*")]


def test_select_and_remove_profiles(tmp_path):
    store = ProfileStore(tmp_path)
    east = Profile("east", "https://east.example.test", "admin", "/tmp/root.crt")
    west = Profile("west", "https://west.example.test", "operator")
    store.save(east, "east-session")
    store.save(west, "west-session")

    assert store.active() == "east"
    assert store.get() == east
    assert store.get("west") == west
    assert store.snapshot() == ("east", [east, west])
    assert store.token(store.get("west")) == "west-session"
    assert store.get() == east
    assert "east-session" not in store.state_path.read_text()
    assert "west-session" not in store.state_path.read_text()

    store.use("west")
    assert store.get() == west
    store.remove("west")
    assert store.active() is None
    assert store.get("east") == east
    with pytest.raises(ProfileError, match="No active profile"):
        store.get()
    with pytest.raises(ProfileError, match="No console session saved"):
        store.token(west)
    assert _saved_tokens(tmp_path) == ["east-session"]


def test_existing_profile_protects_endpoint_and_relogin(tmp_path):
    store = ProfileStore(tmp_path)
    east = Profile("east", "https://east.example.test", "admin")
    wrong_server = Profile("east", "https://other.example.test", "admin")
    store.save(east, "expired-session")
    with pytest.raises(ProfileError) as error:
        store.save(wrong_server, "wrong-server-session")
    assert error.value.code == "PROFILE_CONFLICT"

    store.save(east, "new-session")
    assert not store.clear_session(east, expected_token="expired-session")
    assert store.token(east) == "new-session"
    assert store.clear_session(east, expected_token="new-session")
    assert store.get() == east
    assert not list(tmp_path.glob("session-*"))
    with pytest.raises(ProfileError) as error:
        store.token(east)
    assert error.value.code == "AUTH_REQUIRED"


def test_misconfigured_profile_never_reuses_another_session(tmp_path):
    store = ProfileStore(tmp_path)
    east = Profile("east", "https://east.example.test", "admin")
    west = Profile("west", "https://west.example.test", "admin")
    store.save(east, "east-session")
    store.save(west, "west-session")

    metadata = json.loads(store.state_path.read_text())
    metadata["profiles"]["west"]["session"] = metadata["profiles"]["east"]["session"]
    store.state_path.write_text(json.dumps(metadata))

    with pytest.raises(ProfileError) as error:
        store.token(west)
    assert error.value.code == "AUTH_REQUIRED"
    assert store.token(east) == "east-session"


def test_failed_metadata_write_preserves_prior_session(tmp_path, monkeypatch):
    store = ProfileStore(tmp_path)
    profile = Profile("east", "https://east.example.test", "admin")
    store.save(profile, "original-session")

    def interrupt_commit(*args):
        raise OSError("storage interrupted")

    with monkeypatch.context() as patch:
        patch.setattr(store, "_write", interrupt_commit)
        with pytest.raises(ProfileError) as error:
            store.save(profile, "orphaned-session")
        assert error.value.code == "STATE_ERROR"
    assert store.token(profile) == "original-session"
    store.save(profile, "fresh-session")
    assert _saved_tokens(tmp_path) == ["fresh-session"]


@pytest.mark.skipif(os.name != "posix", reason="POSIX ownership and permission bits")
def test_session_permissions(tmp_path):
    store = ProfileStore(tmp_path / "state")
    profile = Profile("east", "https://east.example.test", "admin")
    store.save(profile, "protected-session")
    assert stat.S_IMODE(store.root.stat().st_mode) == 0o700
    session_path = next(store.root.glob("session-*"))
    assert stat.S_IMODE(session_path.stat().st_mode) == 0o600

    session_path.chmod(0o644)
    with pytest.raises(ProfileError, match="private access"):
        store.token(profile)


def test_parallel_agents_save_distinct_profiles(tmp_path):
    def save(number):
        profile = Profile(f"agent-{number}", f"https://agent-{number}.example.test", "admin")
        ProfileStore(tmp_path).save(profile, f"session-{number}")
        return profile

    with ThreadPoolExecutor(max_workers=8) as agents:
        expected = list(agents.map(save, range(8)))
    store = ProfileStore(tmp_path)
    assert store.list() == expected
    assert [store.token(profile) for profile in expected] == [f"session-{n}" for n in range(8)]
