import json
import logging

from fastapi import (
    FastAPI,
    Header,
    HTTPException,
    Request,
    status,
)
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from app.schemas import GitHubPullRequestWebhook
from app.services.github_service import (
    verify_github_signature,
)
from app.tasks import (
    process_pull_request_task,
)


logging.basicConfig(
    level=logging.INFO,
)

logger = logging.getLogger(__name__)


app = FastAPI(
    title="CodeSentry AI",
    description=(
        "Asynchronous AI-powered "
        "GitHub Pull Request code reviewer."
    ),
    version="1.0.0",
)


@app.get("/health")
async def health_check() -> dict[str, str]:
    """
    Basic application health endpoint.
    """

    return {
        "status": "ok",
    }


@app.post(
    "/webhook/github",
    status_code=status.HTTP_200_OK,
)
async def github_webhook(
    request: Request,
    x_hub_signature_256: str | None = Header(
        default=None,
        alias="X-Hub-Signature-256",
    ),
    x_github_event: str | None = Header(
        default=None,
        alias="X-GitHub-Event",
    ),
) -> JSONResponse:

    # --------------------------------------------------
    # 1. Get the raw request body
    # --------------------------------------------------

    body = await request.body()

    # --------------------------------------------------
    # 2. Verify GitHub HMAC signature
    # --------------------------------------------------

    is_valid = verify_github_signature(
        payload_body=body,
        signature_header=x_hub_signature_256,
    )

    if not is_valid:

        logger.warning(
            "Rejected GitHub webhook with invalid signature."
        )

        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid GitHub webhook signature.",
        )

    # --------------------------------------------------
    # 3. Ignore non pull-request events
    # --------------------------------------------------

    if x_github_event != "pull_request":

        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={
                "status": "ignored",
                "reason": "unsupported_event",
            },
        )

    # --------------------------------------------------
    # 4. Parse JSON
    # --------------------------------------------------

    try:

        payload_data = json.loads(
            body.decode("utf-8")
        )

    except (UnicodeDecodeError, json.JSONDecodeError) as exc:

        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid JSON payload.",
        ) from exc

    # --------------------------------------------------
    # 5. Validate payload
    # --------------------------------------------------

    try:

        validated_payload = (
            GitHubPullRequestWebhook.model_validate(
                payload_data
            )
        )

        payload = validated_payload.model_dump(
            mode="json"
        )

    except ValidationError as exc:

        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Invalid GitHub pull request payload.",
        ) from exc

    # --------------------------------------------------
    # 6. Only process requested actions
    # --------------------------------------------------

    if payload["action"] not in {
        "opened",
        "synchronized",
    }:

        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={
                "status": "ignored",
                "reason": "unsupported_pull_request_action",
            },
        )

    # --------------------------------------------------
    # 7. Dispatch Celery task
    # --------------------------------------------------

    try:

        task = process_pull_request_task.delay(
            payload
        )

    except Exception as exc:

        logger.exception(
            "Unable to enqueue CodeSentry task."
        )

        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Unable to enqueue review task.",
        ) from exc

    # --------------------------------------------------
    # 8. Return immediately
    # --------------------------------------------------

    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={
            "status": "accepted",
            "task_id": task.id,
        },
    )