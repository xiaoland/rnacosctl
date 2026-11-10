"""Transport for the r-nacos console, whose sessions are distinct from OpenAPI tokens."""

import base64
import re
import ssl
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import truststore
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

CONSOLE_V2 = "/rnacos/api/console/v2"
DEFAULT_TIMEOUT = 30.0


class ClientError(Exception):
    def __init__(
        self, code: str, message: str, *, hint: str | None = None, status: int | None = None
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint
        self.status = status


@dataclass(frozen=True)
class Captcha:
    token: str = field(repr=False)
    image: bytes | None = field(default=None, repr=False)


def normalize_url(url: str) -> str:
    candidate = url.strip()
    error = ClientError(
        "INVALID_URL",
        "Provide an HTTP or HTTPS r-nacos console URL without credentials, "
        "query parameters, or a fragment.",
    )
    if any(char.isspace() or ord(char) < 32 for char in candidate) or "\\" in candidate:
        raise error
    try:
        parts = urlsplit(candidate)
        parsed = httpx.URL(candidate)
    except (ValueError, httpx.InvalidURL):
        raise error from None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.host
        or "@" in parts.netloc
        or parts.query
        or parts.fragment
    ):
        raise error
    # The supplied URL can be the origin, a proxy prefix, or the console entry itself.
    path = parsed.raw_path.split(b"?", 1)[0].rstrip(b"/")
    path = path.removesuffix(b"/rnacos")
    return str(parsed.copy_with(raw_path=path or b"/", fragment=None)).rstrip("/")


class Client:
    def __init__(
        self,
        url: str,
        *,
        ca_bundle: str | Path | None = None,
        token: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        transport: httpx.BaseTransport | None = None,
    ):
        self.url = normalize_url(url)
        self.token = token
        try:
            tls = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            if ca_bundle is not None:
                tls.load_verify_locations(cafile=ca_bundle)
        except (OSError, ValueError):
            raise ClientError(
                "INVALID_CA_BUNDLE",
                "Could not load the trusted CA certificate bundle.",
                hint="Check that the configured CA bundle is a readable PEM file.",
            ) from None
        self._http = httpx.Client(
            verify=tls,
            timeout=timeout,
            follow_redirects=False,
            transport=transport,
        )

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self._http.close()

    def request(
        self,
        method: str,
        path: str,
        *,
        params=None,
        json=None,
        files=None,
        data=None,
        headers=None,
        raw: bool = False,
    ) -> Any:
        response = self._send(
            method,
            path,
            params=params,
            json=json,
            files=files,
            data=data,
            headers=headers,
        )
        result = _unpack(response, raw=raw, secrets=(self.token, *_payload_secrets(json, data)))
        if path in (f"{CONSOLE_V2}/user/list", "/rnacos/api/console/user/list"):
            _remove_user_credentials(result)
        return result

    def captcha(self) -> Captcha:
        response = self._send("GET", f"{CONSOLE_V2}/login/captcha", authenticated=False)
        payload = _unpack(response)
        token = response.headers.get("Captcha-Token", "")
        if not re.fullmatch(r"[0-9a-fA-F]{32}", token):
            raise _invalid_response("The server did not provide a valid CAPTCHA challenge.")
        if payload is None:
            return Captcha(token)
        try:
            image = base64.b64decode(payload, validate=True)
        except (TypeError, ValueError):
            raise _invalid_response("The server returned invalid CAPTCHA image data.") from None
        if not image.startswith(b"\x89PNG\r\n\x1a\n"):
            raise _invalid_response("The server returned a CAPTCHA image in an unexpected format.")
        return Captcha(token, image)

    def login(
        self,
        username: str,
        password: str,
        *,
        captcha: Captcha,
        answer: str | None = None,
    ) -> str:
        if not re.fullmatch(r"[0-9a-fA-F]{32}", captcha.token):
            raise ClientError(
                "INVALID_CAPTCHA", "Provide the token returned with the CAPTCHA image."
            )
        normalized_answer = answer.strip().upper() if answer else ""
        if captcha.image is not None and not normalized_answer:
            raise ClientError("CAPTCHA_REQUIRED", "This instance requires a CAPTCHA answer.")
        encoded = _encode_password(password, captcha.token)
        form = {"username": username, "password": encoded}
        if normalized_answer:
            form["captcha"] = normalized_answer
        response = self._send(
            "POST",
            f"{CONSOLE_V2}/login/login",
            authenticated=False,
            data=form,
            headers={"Cookie": f"captcha_token={captcha.token}"},
        )
        return self._accept_session(
            response, secrets=(password, encoded, captcha.token, normalized_answer)
        )

    def login_config(self) -> dict[str, Any]:
        response = self._send("GET", f"{CONSOLE_V2}/login/config", authenticated=False)
        config = _unpack(response)
        if not isinstance(config, dict) or not isinstance(config.get("oauth2Enable"), bool):
            raise _invalid_response("The server returned an invalid login configuration.")
        return {
            key: config.get(key) for key in ("oauth2Enable", "oauth2Button", "oauth2AuthorizeUrl")
        }

    def login_oauth2(self, code: str, *, state: str | None = None) -> str:
        response = self._send(
            "POST",
            f"{CONSOLE_V2}/login/oauth2/login",
            authenticated=False,
            json={"code": code, "state": state},
        )
        return self._accept_session(response, secrets=(code, state))

    def whoami(self) -> dict[str, Any]:
        info = self.request("GET", f"{CONSOLE_V2}/user/info")
        access = self.request("GET", f"{CONSOLE_V2}/user/web_resources")
        if (
            not isinstance(info, dict)
            or not isinstance(info.get("username"), str)
            or not isinstance(access, dict)
            or access.get("username") != info["username"]
            or not isinstance(access.get("resources"), list)
        ):
            raise _invalid_response("The server returned incomplete console identity information.")
        return {
            "username": info["username"],
            "nickname": info.get("nickname"),
            "version": access.get("version"),
            "admin": "USER_UPDATE" in access["resources"],
        }

    def logout(self) -> None:
        self.request("POST", f"{CONSOLE_V2}/login/logout")
        self.token = None

    def _accept_session(self, response: httpx.Response, *, secrets) -> str:
        result = _unpack(response, secrets=secrets)
        token = result.get("token") if isinstance(result, dict) else None
        if not isinstance(token, str) or not re.fullmatch(r"[\x21-\x7e]+", token):
            raise _invalid_response("The server did not return a valid console session token.")
        self.token = token
        return token

    def _send(self, method: str, path: str, *, authenticated: bool = True, headers=None, **kwargs):
        if not path.startswith("/") or path.startswith("//") or "\\" in path:
            raise ClientError("INVALID_PATH", "The API path must be relative to the console URL.")
        request_headers = httpx.Headers(headers, encoding="utf-8")
        if authenticated:
            # Upstream gives cookies priority over the Token header. Use only the selected session.
            request_headers.pop("Cookie", None)
            request_headers.pop("Token", None)
            if self.token is not None:
                if not re.fullmatch(r"[\x21-\x7e]+", self.token):
                    raise ClientError("INVALID_TOKEN", "The console session token is invalid.")
                request_headers["Token"] = self.token
        try:
            return self._http.request(
                method, f"{self.url}{path}", headers=request_headers, **kwargs
            )
        except httpx.TimeoutException:
            raise ClientError("TIMEOUT", "The r-nacos console request timed out.") from None
        except httpx.ConnectError as exc:
            if _is_certificate_error(exc):
                raise ClientError(
                    "TLS_VERIFY_FAILED",
                    "Could not verify the console TLS certificate.",
                    hint="Install the instance CA in the trust store or specify --ca-bundle.",
                ) from None
            raise ClientError(
                "CONNECTION_ERROR", "Could not connect to the r-nacos console."
            ) from None
        except httpx.RequestError:
            raise ClientError(
                "NETWORK_ERROR", "The r-nacos console request failed in transit."
            ) from None
        finally:
            self._http.cookies.clear()


def _is_certificate_error(error: BaseException | None) -> bool:
    while error is not None:
        if isinstance(error, ssl.SSLCertVerificationError):
            return True
        error = error.__cause__ or error.__context__
    return False


def _encode_password(password: str, token: str) -> str:
    # The challenge token supplies the AES key and IV for the console login wire format.
    key_and_iv = token.encode("ascii")
    padder = padding.PKCS7(128).padder()
    padded = padder.update(password.encode("utf-8")) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key_and_iv[:16]), modes.CBC(key_and_iv[16:])).encryptor()
    return base64.b64encode(encryptor.update(padded) + encryptor.finalize()).decode("ascii")


def _payload_secrets(*payloads):
    names = {"password", "oldpassword", "newpassword", "token", "accesstoken", "authorization"}
    for payload in payloads:
        if isinstance(payload, dict):
            for key, value in payload.items():
                if isinstance(key, str) and key.lower().replace("_", "") in names:
                    if isinstance(value, str):
                        yield value
                else:
                    yield from _payload_secrets(value)
        elif isinstance(payload, list):
            yield from _payload_secrets(*payload)


def _remove_user_credentials(result) -> None:
    users = result.get("list") if isinstance(result, dict) else None
    if isinstance(users, list):
        for user in users:
            if isinstance(user, dict):
                user.pop("password", None)
                user.pop("passwordHash", None)


def _safe_message(value, secrets) -> str | None:
    if not isinstance(value, str):
        return None
    for secret in sorted({secret for secret in secrets if secret}, key=len, reverse=True):
        value = value.replace(secret, "[redacted]")
    return " ".join(value.split())[:500] or None


def _invalid_response(message: str) -> ClientError:
    return ClientError(
        "INVALID_RESPONSE",
        message,
        hint="Check that the URL points to the r-nacos console (port 10848 by default).",
    )


def _unpack(response: httpx.Response, *, raw: bool = False, secrets=()) -> Any:
    status = response.status_code
    content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
    is_json = content_type == "application/json" or content_type.endswith("+json")
    result = None
    if response.content and (is_json or (not raw and response.is_success)):
        try:
            result = response.json()
        except ValueError:
            if response.is_success:
                raise _invalid_response("The console returned an invalid JSON response.") from None
    error_code = result.get("code") if isinstance(result, dict) else None
    if response.headers.get("No-Login") == "1" or error_code in (
        "NO_LOGIN",
        "NOT_FOUND_USER_SESSION",
    ):
        raise ClientError(
            "AUTH_REQUIRED",
            "The console session is missing, expired, or revoked.",
            hint="Log in to this profile again.",
            status=status,
        )
    if response.headers.get("No-Permission") == "1" or error_code in (
        "NO_PERMISSION",
        "NO_NAMESPACE_PERMISSION",
    ):
        raise ClientError(
            "PERMISSION_DENIED",
            "The console session lacks permission for this operation.",
            status=status,
        )
    if isinstance(result, dict) and result.get("success") is False:
        _raise_console_error(result, secrets, status)
    if not response.is_success:
        _raise_http_error(response, secrets)
    if raw:
        if is_json or content_type == "text/html":
            raise _invalid_response(
                "The console returned a document instead of the requested file."
            )
        return response.content
    if isinstance(result, dict) and result.get("success") is True:
        return result.get("data")
    return result


def _raise_console_error(result: dict, secrets, status: int) -> None:
    code = result.get("code")
    if not isinstance(code, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", code):
        code = "CONSOLE_ERROR"
    if code == "CAPTCHA_CHECK_ERROR":
        raise ClientError(
            code,
            "The CAPTCHA answer was incorrect or the challenge expired.",
            hint="Request a new CAPTCHA and try again.",
            status=status,
        )
    if code == "USER_CHECK_ERROR":
        raise ClientError(code, "The console rejected the username or password.", status=status)
    message = _safe_message(result.get("message") or result.get("msg"), secrets)
    raise ClientError(code, message or "The r-nacos console rejected the request.", status=status)


def _raise_http_error(response: httpx.Response, secrets) -> None:
    status = response.status_code
    if status == 401 and response.text.startswith("user no such namespace permission:"):
        raise ClientError(
            "PERMISSION_DENIED", "The session cannot access this namespace.", status=status
        )
    if status == 401:
        raise ClientError("AUTH_REQUIRED", "The console rejected the session.", status=status)
    if status == 403:
        raise ClientError("PERMISSION_DENIED", "The console denied this operation.", status=status)
    if status == 404 or response.is_redirect:
        raise ClientError(
            "ENDPOINT_UNAVAILABLE",
            f"The r-nacos console endpoint returned HTTP {status}.",
            hint="Check the console URL, reverse proxy, and r-nacos version.",
            status=status,
        )
    content_type = response.headers.get("Content-Type", "")
    message = (
        _safe_message(response.text, secrets) if content_type.startswith("text/plain") else None
    )
    raise ClientError(
        "HTTP_ERROR", message or f"The console returned HTTP {status}.", status=status
    )
