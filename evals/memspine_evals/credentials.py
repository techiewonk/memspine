"""Credential checks for paid runs (C-3): a preflight before any spend, and the rule
that tells an expired or invalid credential apart from an ordinary failed question.

A run whose session token expires part-way otherwise records every later question as
an ERROR row, each one still charged against the budget's call count, and nothing
says why. Two guards:

* :func:`preflight_aws` calls STS ``get_caller_identity`` before the first paid call
  and, when the credentials carry an expiry, compares the time left with the run's
  estimated duration. The STS client and the expiry lookup are parameters, so tests
  pass fakes and no request leaves the process.
* :func:`is_auth_error` recognises an expired, invalid or unauthorised credential
  anywhere in an exception's cause chain. The runner treats one as fatal: the rest
  of the run becomes UNATTEMPTED rows and the summary is still written.

Nothing here reads ``.env``; the AWS keys are already in the environment
(``bedrock.load_aws_credentials``).
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

__all__ = [
    "AUTH_ERROR_NAMES",
    "CredentialCheck",
    "CredentialsInvalid",
    "estimate_run_seconds",
    "is_auth_error",
    "preflight_aws",
]

#: Exception class names that mean the credential itself is bad (litellm's mapped
#: classes, botocore's error codes as exception names, boto's missing-credential errors).
AUTH_ERROR_NAMES = frozenset(
    {
        "AuthenticationError",
        "PermissionDeniedError",
        "ExpiredTokenException",
        "ExpiredToken",
        "UnrecognizedClientException",
        "InvalidSignatureException",
        "InvalidClientTokenId",
        "AccessDeniedException",
        "NoCredentialsError",
        "PartialCredentialsError",
        "CredentialRetrievalError",
    }
)

#: Message fragments of the same failures. litellm wraps an expired-token 403 in an
#: ``APIConnectionError``, so the class alone is not enough.
_AUTH_HINTS = (
    "security token included in the request is expired",
    "security token included in the request is invalid",
    "expiredtoken",
    "token has expired",
    "unrecognizedclient",
    "invalidsignature",
    "invalidclienttokenid",
    "signature we calculated does not match",
    "accessdenied",
    "is not authorized to perform",
    "unable to locate credentials",
    "403 forbidden",
)

#: Seconds per paid call assumed when estimating a run's duration (Bedrock Qwen3-32B
#: reader/judge calls averaged about a second each in the 2026-10 LoCoMo runs).
SECONDS_PER_CALL = 1.5

#: Environment variables some credential tools set to the session's expiry (ISO 8601).
_EXPIRY_ENV = ("AWS_CREDENTIAL_EXPIRATION", "AWS_SESSION_EXPIRATION", "AWS_TOKEN_EXPIRATION")


class CredentialsInvalid(RuntimeError):
    """The preflight found the credentials unusable; nothing was spent."""


def _chain(exc: BaseException) -> list[BaseException]:
    seen: list[BaseException] = []
    current: BaseException | None = exc
    while current is not None and current not in seen and len(seen) < 16:
        seen.append(current)
        current = current.__cause__ or current.__context__
    return seen


def is_auth_error(exc: BaseException) -> bool:
    """True when ``exc`` (or anything in its cause chain) is a credential failure."""
    for err in _chain(exc):
        if any(cls.__name__ in AUTH_ERROR_NAMES for cls in type(err).__mro__):
            return True
        code = getattr(err, "response", None)
        if isinstance(code, dict):
            error_code = str((code.get("Error") or {}).get("Code", ""))
            if error_code in AUTH_ERROR_NAMES:
                return True
        text = str(err).lower()
        if any(hint in text for hint in _AUTH_HINTS):
            return True
    return False


def estimate_run_seconds(
    max_model_calls: int | None, seconds_per_call: float = SECONDS_PER_CALL
) -> float | None:
    """A rough run duration from its call cap (None without a cap)."""
    if max_model_calls is None:
        return None
    return max_model_calls * seconds_per_call


def _parse_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def credential_expiry() -> datetime | None:
    """The current AWS credentials' expiry, when anything states it.

    An environment variable a credential tool set, else the expiry of botocore's
    refreshable credentials (SSO, assumed roles). Plain environment keys carry none.
    """
    for name in _EXPIRY_ENV:
        parsed = _parse_time(os.environ.get(name))
        if parsed is not None:
            return parsed
    try:
        import boto3

        creds = boto3.Session().get_credentials()
    except Exception:
        return None
    return _parse_time(getattr(creds, "_expiry_time", None)) if creds is not None else None


@dataclass
class CredentialCheck:
    """What the preflight found."""

    account: str | None
    arn: str | None
    expires_at: datetime | None
    remaining_s: float | None
    expected_s: float | None
    temporary: bool
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "account": self.account,
            "arn": self.arn,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "remaining_s": self.remaining_s,
            "expected_s": self.expected_s,
            "temporary": self.temporary,
            "warnings": list(self.warnings),
        }


def _default_sts(region: str | None) -> Any:
    import boto3

    return boto3.client("sts", region_name=region) if region else boto3.client("sts")


def preflight_aws(
    *,
    sts_client: Any | None = None,
    expected_seconds: float | None = None,
    expiry_lookup: Callable[[], datetime | None] = credential_expiry,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    region: str | None = None,
    warn: Callable[[str], None] | None = None,
) -> CredentialCheck:
    """Check the AWS credentials before any paid call.

    Raises :class:`CredentialsInvalid` when STS rejects them. Otherwise returns what it
    found, after printing a loud warning (``warn``, default stderr) when the session
    expires before the estimated end of the run, or is temporary with no stated expiry.
    """
    say = warn or (lambda message: print(message, file=sys.stderr, flush=True))
    client = sts_client if sts_client is not None else _default_sts(region)
    try:
        identity = client.get_caller_identity()
    except Exception as exc:
        reason = "expired or invalid" if is_auth_error(exc) else "unusable"
        raise CredentialsInvalid(
            f"AWS credentials are {reason} (STS get_caller_identity: {type(exc).__name__}: "
            f"{exc}); refresh them before a paid run"
        ) from exc
    temporary = bool(os.environ.get("AWS_SESSION_TOKEN"))
    expires_at = expiry_lookup()
    remaining = (expires_at - now()).total_seconds() if expires_at is not None else None
    check = CredentialCheck(
        account=identity.get("Account"),
        arn=identity.get("Arn"),
        expires_at=expires_at,
        remaining_s=remaining,
        expected_s=expected_seconds,
        temporary=temporary or expires_at is not None,
    )
    if remaining is not None and remaining <= 0:
        raise CredentialsInvalid(f"AWS credentials expired at {expires_at:%Y-%m-%d %H:%M:%S %Z}")
    if remaining is not None and expected_seconds is not None and remaining < expected_seconds:
        check.warnings.append(
            f"AWS credentials expire in {remaining / 60:.0f} min, before the run's estimated "
            f"{expected_seconds / 60:.0f} min; the run will stop at expiry (rows left "
            "UNATTEMPTED). Refresh the session or split the run (run_chunked.py)."
        )
    elif remaining is None and temporary:
        check.warnings.append(
            "AWS session credentials (AWS_SESSION_TOKEN) with no stated expiry: if they "
            "expire mid-run, the run stops there and the rest is UNATTEMPTED."
        )
    for message in check.warnings:
        say("!" * 78 + "\nWARNING: " + message + "\n" + "!" * 78)
    return check
