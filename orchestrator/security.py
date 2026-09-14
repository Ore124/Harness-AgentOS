"""OIDC authentication, project RBAC, and structured redaction."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

import jwt


class AuthenticationError(PermissionError):
    pass


class AuthorizationError(PermissionError):
    pass


ROLE_PERMISSIONS = {
    "viewer": {"read"},
    "approver": {"read", "approve"},
    "operator": {"read", "create_run", "control_run"},
    "owner": {"read", "create_run", "control_run", "approve", "admin"},
}

SENSITIVE_KEYS = {
    "authorization",
    "password",
    "secret",
    "token",
    "api_key",
    "apikey",
    "access_token",
    "refresh_token",
}


@dataclass(frozen=True)
class AuthContext:
    subject: str
    claims: dict[str, Any]
    local_trusted: bool = False


class Authenticator:
    def __init__(
        self,
        mode: str,
        *,
        issuer: str = "",
        audience: str = "",
        jwks_url: str = "",
    ):
        if mode not in {"disabled", "oidc"}:
            raise ValueError(f"unsupported auth mode: {mode}")
        if mode == "oidc" and not (issuer and audience and jwks_url):
            raise ValueError("OIDC mode requires issuer, audience, and JWKS URL")
        self.mode = mode
        self.issuer = issuer
        self.audience = audience
        self.jwks_url = jwks_url
        self._jwk_client = jwt.PyJWKClient(jwks_url) if mode == "oidc" else None

    def authenticate(self, authorization: str | None) -> AuthContext:
        if self.mode == "disabled":
            return AuthContext("local", {"sub": "local"}, local_trusted=True)
        scheme, separator, token = (authorization or "").partition(" ")
        if not separator or scheme.lower() != "bearer" or not token:
            raise AuthenticationError("Bearer token required")
        try:
            signing_key = self._jwk_client.get_signing_key_from_jwt(token)
            header = jwt.get_unverified_header(token)
            algorithm = str(header.get("alg", ""))
            if algorithm not in {"RS256", "RS384", "RS512", "ES256", "ES384", "ES512"}:
                raise AuthenticationError("token algorithm is not allowed")
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=[algorithm],
                audience=self.audience,
                issuer=self.issuer,
                options={"require": ["exp", "iat", "sub"]},
            )
        except AuthenticationError:
            raise
        except Exception as exc:
            raise AuthenticationError("invalid OIDC token") from exc
        return AuthContext(str(claims["sub"]), dict(claims))


def authorize_project(
    context: AuthContext,
    repository,
    project_id: str,
    permission: str,
) -> str:
    if context.local_trusted:
        return "owner"
    role = repository.get_membership_role(project_id, context.subject)
    if not role or permission not in ROLE_PERMISSIONS.get(role, set()):
        raise AuthorizationError(
            f"subject {context.subject!r} lacks {permission!r} on project {project_id!r}"
        )
    return role


def redact(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): "[redacted]" if str(key).lower() in SENSITIVE_KEYS else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact(item) for item in value)
    return value
