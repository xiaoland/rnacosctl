import base64
import ssl
from urllib.parse import parse_qs

import httpx
import pytest

from rnacosctl.client import CONSOLE_V2, Captcha, Client, ClientError, normalize_url

CHALLENGE = "0123456789abcdeffedcba9876543210"
PNG = b"\x89PNG\r\n\x1a\nexample"


def success(data, **kwargs):
    return httpx.Response(200, json={"success": True, "data": data}, **kwargs)


@pytest.mark.parametrize("captcha_required", [False, True])
def test_login_uses_console_challenge_and_preserves_only_selected_session(captcha_required):
    def handler(request):
        path = request.url.path
        if path == f"{CONSOLE_V2}/login/captcha":
            assert "token" not in request.headers
            image = base64.b64encode(PNG).decode() if captcha_required else None
            return success(image, headers={"Captcha-Token": CHALLENGE})
        if path == f"{CONSOLE_V2}/login/login":
            assert request.headers["Cookie"] == f"captcha_token={CHALLENGE}"
            form = parse_qs(request.content.decode())
            assert form["username"] == ["admin"]
            assert form["password"] == ["XeKL5JKoV47ouUtmqi9KTw=="]
            assert form.get("captcha") == (["Z9ER"] if captcha_required else None)
            return success({"token": "session"}, headers={"Set-Cookie": "token=cookie; Path=/"})
        assert request.headers["Token"] == "session"
        assert "Cookie" not in request.headers
        if path == f"{CONSOLE_V2}/user/info":
            return success({"username": "admin", "nickname": "Admin", "token": "not-exposed"})
        if path == f"{CONSOLE_V2}/user/web_resources":
            return success({"username": "admin", "version": "0.8.8", "resources": ["USER_UPDATE"]})
        assert path == f"{CONSOLE_V2}/login/logout"
        assert request.method == "POST"
        return success(True)

    with Client("https://example.test/rnacos/", transport=httpx.MockTransport(handler)) as client:
        challenge = client.captcha()
        assert challenge.image == (PNG if captcha_required else None)
        assert (
            client.login(
                "admin",
                "a-test-pass",
                captcha=challenge,
                answer=" z9er " if captcha_required else None,
            )
            == "session"
        )
        assert client.whoami() == {
            "username": "admin",
            "nickname": "Admin",
            "version": "0.8.8",
            "admin": True,
        }
        client.logout()
        assert client.token is None


def test_captcha_enabled_requires_answer_before_posting_password():
    def unexpected_request(request):
        pytest.fail(f"Unexpected {request.method} request")

    with (
        Client("https://example.test", transport=httpx.MockTransport(unexpected_request)) as client,
        pytest.raises(ClientError) as raised,
    ):
        client.login("admin", "password", captcha=Captcha(CHALLENGE, PNG))
    assert raised.value.code == "CAPTCHA_REQUIRED"


def test_response_envelopes_and_downloads():
    responses = iter(
        (
            httpx.Response(200, json={"count": 1, "list": ["a"]}),
            httpx.Response(200),
            httpx.Response(
                200, content=b"PK bytes", headers={"Content-Type": "application/octet-stream"}
            ),
            httpx.Response(200, json={"success": False, "code": "NO_LOGIN", "msg": None}),
            httpx.Response(401, text="user no such namespace permission: p"),
        )
    )
    transport = httpx.MockTransport(lambda request: next(responses))
    with Client("https://example.test", transport=transport) as client:
        assert client.request("GET", f"{CONSOLE_V2}/legacy") == {"count": 1, "list": ["a"]}
        assert client.request("POST", f"{CONSOLE_V2}/config/import") is None
        assert client.request("GET", f"{CONSOLE_V2}/config/download", raw=True) == b"PK bytes"
        with pytest.raises(ClientError) as expired:
            client.request("GET", f"{CONSOLE_V2}/config/download", raw=True)
        assert expired.value.code == "AUTH_REQUIRED"
        with pytest.raises(ClientError) as denied:
            client.request("POST", f"{CONSOLE_V2}/config/import")
        assert denied.value.code == "PERMISSION_DENIED"


def test_upstream_error_redacts_token_and_password():
    def handler(_request):
        return httpx.Response(
            400,
            json={
                "success": False,
                "code": "PARAM_ERROR",
                "message": "bad logged-token new-password",
            },
        )

    with (
        Client(
            "https://example.test", token="logged-token", transport=httpx.MockTransport(handler)
        ) as client,
        pytest.raises(ClientError) as raised,
    ):
        client.request("POST", f"{CONSOLE_V2}/user/update", json={"password": "new-password"})
    assert raised.value.code == "PARAM_ERROR"
    assert str(raised.value) == "bad [redacted] [redacted]"


def test_user_list_omits_upstream_password_hashes():
    def handler(_request):
        return success(
            {
                "totalCount": 1,
                "list": [
                    {
                        "username": "admin",
                        "password": None,
                        "passwordHash": "sensitive-hash",
                    }
                ],
            }
        )

    with Client("https://example.test", transport=httpx.MockTransport(handler)) as client:
        assert client.request("GET", f"{CONSOLE_V2}/user/list") == {
            "totalCount": 1,
            "list": [{"username": "admin"}],
        }


def test_admin_token_is_never_forwarded_on_redirect():
    calls = []

    def handler(request):
        calls.append(request.url.host)
        return httpx.Response(307, headers={"Location": "https://elsewhere.test/console"})

    with (
        Client(
            "https://example.test", token="session", transport=httpx.MockTransport(handler)
        ) as client,
        pytest.raises(ClientError) as raised,
    ):
        client.request("GET", f"{CONSOLE_V2}/user/info")
    assert raised.value.code == "ENDPOINT_UNAVAILABLE"
    assert calls == ["example.test"]


def test_native_certificate_failure_has_actionable_error():
    def handler(request):
        raise httpx.ConnectError("certificate is not trusted", request=request) from (
            ssl.SSLCertVerificationError("certificate is not trusted")
        )

    with (
        Client("https://example.test", transport=httpx.MockTransport(handler)) as client,
        pytest.raises(ClientError) as raised,
    ):
        client.login_config()
    assert raised.value.code == "TLS_VERIFY_FAILED"
    assert "--ca-bundle" in raised.value.hint


@pytest.mark.parametrize(
    "url",
    [
        "https://user:secret@example.test/",
        "https://example.test/?accessToken=secret",
        "file:///tmp/console",
        "https://example.test/#fragment",
        "example.test",
    ],
)
def test_reject_invalid_console_urls(url):
    with pytest.raises(ClientError) as raised:
        normalize_url(url)
    assert raised.value.code == "INVALID_URL"
    assert "secret" not in str(raised.value)
