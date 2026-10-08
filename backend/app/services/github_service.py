import hashlib
import hmac
import logging
import time
from typing import Any
from urllib.parse import quote

import requests

from app.config import settings

logger = logging.getLogger(__name__)


class GitHubAPIError(RuntimeError):
    """
    Error raised when a GitHub API operation fails.
    """

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        retryable: bool = False,
        retry_after: float | None = None,
    ):
        super().__init__(message)

        self.status_code = status_code
        self.retryable = retryable
        self.retry_after = retry_after


def verify_github_signature(
    payload_body: bytes,
    signature_header: str | None,
) -> bool:
    """
    Verify GitHub's X-Hub-Signature-256 header.

    GitHub computes:
        HMAC-SHA256(webhook_secret, raw_request_body)

    and sends it as:
        sha256=<hex_digest>
    """

    if not signature_header:
        return False

    if not signature_header.startswith("sha256="):
        return False

    provided_signature = signature_header.removeprefix(
        "sha256="
    ).strip()

    if not provided_signature:
        return False

    expected_signature = hmac.new(
        settings.github_webhook_secret.get_secret_value().encode("utf-8"),
        payload_body,
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(
        expected_signature,
        provided_signature,
    )


def _github_headers() -> dict[str, str]:
    """
    Standard headers for GitHub API requests.
    """

    return {
        "Accept": "application/vnd.github+json",
        "Authorization": (
            f"Bearer "
            f"{settings.github_token.get_secret_value()}"
        ),
        "X-GitHub-Api-Version": settings.github_api_version,
        "User-Agent": "CodeSentry-AI/1.0",
        "Content-Type": "application/json",
    }


def _raise_for_github_response(
    response: requests.Response,
    context: str,
) -> None:
    """
    Convert non-2xx GitHub responses into application errors.
    """

    if response.ok:
        return

    status_code = response.status_code

    try:
        body = response.json()

        detail = body.get(
            "message",
            response.text[:500],
        )

    except ValueError:
        detail = response.text[:500]

    rate_limited = (
        status_code == 403
        and (
            response.headers.get("X-RateLimit-Remaining") == "0"
            or "Retry-After" in response.headers
            or "rate limit" in detail.lower()
        )
    )
    retryable = (
        status_code == 429
        or status_code >= 500
        or rate_limited
    )
    retry_after: float | None = None
    retry_after_header = response.headers.get("Retry-After")
    if retry_after_header is not None:
        try:
            retry_after = max(0.0, float(retry_after_header))
        except ValueError:
            pass

    if (
        retry_after is None
        and response.headers.get("X-RateLimit-Remaining") == "0"
    ):
        try:
            reset_at = float(response.headers["X-RateLimit-Reset"])
        except (KeyError, ValueError):
            pass
        else:
            retry_after = max(0.0, reset_at - time.time())

    raise GitHubAPIError(
        (
            f"GitHub API error while {context}: "
            f"HTTP {status_code}: {detail}"
        ),
        status_code=status_code,
        retryable=retryable,
        retry_after=retry_after,
    )


def _request(
    method: str,
    url: str,
    **kwargs: Any,
) -> requests.Response:
    """
    Perform an authenticated GitHub REST API request.
    """

    try:
        response = requests.request(
            method,
            url,
            headers=_github_headers(),
            timeout=settings.github_timeout_seconds,
            **kwargs,
        )

    except requests.RequestException as exc:
        raise GitHubAPIError(
            f"GitHub API request failed: {exc}",
            retryable=True,
        ) from exc

    return response


def fetch_pull_request_diff(
    owner: str,
    repo: str,
    pull_number: int,
) -> str:
    """
    Fetch changed files and patches for a pull request.

    GitHub can omit textual patches for binary/large files, so those files
    are represented explicitly rather than silently discarded.
    """

    owner_q = quote(owner, safe="")
    repo_q = quote(repo, safe="")

    base_url = (
        f"{settings.github_api_url.rstrip('/')}"
        f"/repos/{owner_q}/{repo_q}"
        f"/pulls/{pull_number}/files"
    )

    patches: list[str] = []

    page = 1
    total_chars = 0

    while True:
        response = _request(
            "GET",
            base_url,
            params={
                "per_page": 100,
                "page": page,
            },
        )

        _raise_for_github_response(
            response,
            f"fetching PR #{pull_number} files",
        )

        files = response.json()

        if not isinstance(files, list):
            raise GitHubAPIError(
                "GitHub returned an unexpected file-list response"
            )

        for file in files:
            filename = file.get(
                "filename",
                "unknown",
            )

            file_status = file.get(
                "status",
                "modified",
            )

            patch = file.get("patch")

            header = (
                f"diff --git a/{filename} b/{filename}\n"
                f"status: {file_status}\n"
            )

            if patch:
                block = (
                    header
                    + patch.strip()
                    + "\n"
                )
            else:
                block = (
                    header
                    + (
                        "[No textual patch available; "
                        "file may be binary or too large.]\n"
                    )
                )

            patches.append(block)

            total_chars += len(block)

            if total_chars >= settings.max_diff_chars:
                break

        if (
            total_chars >= settings.max_diff_chars
            or len(files) < 100
        ):
            break

        page += 1

    if not patches:
        raise GitHubAPIError(
            f"PR #{pull_number} has no reviewable file patches"
        )

    diff = "\n".join(patches)

    if len(diff) > settings.max_diff_chars:
        diff = (
            diff[: settings.max_diff_chars]
            + "\n\n"
            + "[Diff truncated by CodeSentry AI.]\n"
        )

    return diff


def _comments_url(
    owner: str,
    repo: str,
    pull_number: int,
) -> str:
    return (
        f"{settings.github_api_url.rstrip('/')}"
        f"/repos/"
        f"{quote(owner, safe='')}/"
        f"{quote(repo, safe='')}"
        f"/issues/{pull_number}/comments"
    )


def upsert_pr_comment(
    owner: str,
    repo: str,
    pull_number: int,
    markdown_body: str,
) -> dict[str, Any]:
    """
    Create or update CodeSentry's PR timeline comment.

    The hidden marker makes the task idempotent:
    Celery retries update the existing CodeSentry comment
    instead of creating duplicate comments.
    """

    marker = "<!-- codesentry-ai -->"

    body = (
        marker
        + "\n"
        + markdown_body.strip()
    )

    url = _comments_url(
        owner,
        repo,
        pull_number,
    )

    page = 1

    while True:
        response = _request(
            "GET",
            url,
            params={
                "per_page": 100,
                "page": page,
            },
        )

        _raise_for_github_response(
            response,
            (
                "checking existing comments "
                f"on PR #{pull_number}"
            ),
        )

        comments = response.json()

        if not isinstance(comments, list):
            raise GitHubAPIError(
                "GitHub returned an unexpected comments response"
            )

        for comment in comments:
            existing_body = comment.get("body") or ""

            if (
                marker in existing_body
                and comment.get("id")
            ):
                comment_id = comment["id"]

                update_url = (
                    f"{settings.github_api_url.rstrip('/')}"
                    f"/repos/"
                    f"{quote(owner, safe='')}/"
                    f"{quote(repo, safe='')}"
                    f"/issues/comments/{comment_id}"
                )

                update_response = _request(
                    "PATCH",
                    update_url,
                    json={
                        "body": body,
                    },
                )

                _raise_for_github_response(
                    update_response,
                    (
                        "updating the CodeSentry "
                        f"comment on PR #{pull_number}"
                    ),
                )

                return update_response.json()

        if len(comments) < 100:
            break

        page += 1

    create_response = _request(
        "POST",
        url,
        json={
            "body": body,
        },
    )

    _raise_for_github_response(
        create_response,
        (
            "posting the CodeSentry "
            f"comment on PR #{pull_number}"
        ),
    )

    return create_response.json()


def post_pr_comment(
    owner: str,
    repo: str,
    pull_number: int,
    markdown_body: str,
) -> dict[str, Any]:
    """
    Public application-level function used by the Celery task.
    """

    return upsert_pr_comment(
        owner,
        repo,
        pull_number,
        markdown_body,
    )