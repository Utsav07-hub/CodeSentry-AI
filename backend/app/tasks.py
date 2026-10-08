import logging
from typing import Any

from celery import Task
from sqlalchemy import select

from app.celery_app import celery_app
from app.database import SessionLocal
from app.models import Repository, ReviewLog, User
from app.services.ai_service import (
    AIReviewError,
    analyze_diff,
)
from app.services.github_service import (
    GitHubAPIError,
    fetch_pull_request_diff,
    post_pr_comment,
)

logger = logging.getLogger(__name__)


class ReviewTask(Task):
    """
    Base Celery task configuration for CodeSentry.
    """

    max_retries = 3

    default_retry_delay = 10


def _get_or_create_repository(
    db,
    payload: dict[str, Any],
) -> Repository:
    """
    Find the repository associated with the GitHub event.

    If it doesn't exist yet, create the corresponding
    User and Repository records.
    """

    repository_data = payload.get(
        "repository",
        {},
    )

    full_name = repository_data.get(
        "full_name"
    )

    if not full_name:
        raise ValueError(
            "Missing repository.full_name in GitHub payload."
        )

    owner_data = repository_data.get(
        "owner",
        {},
    )

    sender_data = payload.get(
        "sender",
        {},
    )

    github_id = (
        owner_data.get("id")
        or sender_data.get("id")
    )

    username = (
        owner_data.get("login")
        or sender_data.get("login")
        or "github-user"
    )

    if github_id is None:
        raise ValueError(
            "Missing GitHub user ID in webhook payload."
        )

    user = db.scalar(
        select(User).where(
            User.github_id == int(github_id)
        )
    )

    if user is None:
        user = User(
            username=str(username),
            github_id=int(github_id),
        )

        db.add(user)
        db.flush()

    else:
        user.username = str(username)

    repository = db.scalar(
        select(Repository).where(
            Repository.user_id == user.id,
            Repository.repo_name == str(full_name),
        )
    )

    if repository is None:
        repository = Repository(
            user_id=user.id,
            repo_name=str(full_name),
            is_active=True,
        )

        db.add(repository)
        db.flush()

    return repository


def _mark_review_failed(
    review_id: int,
    error_message: str,
) -> None:
    """
    Mark an existing review as failed.
    """

    with SessionLocal() as db:

        review = db.get(
            ReviewLog,
            review_id,
        )

        if review is None:
            return

        review.status = "failed"

        review.ai_feedback = error_message[:50000]

        db.commit()


@celery_app.task(
    bind=True,
    base=ReviewTask,
    name="app.tasks.process_pull_request_task",
)
def process_pull_request_task(
    self,
    payload: dict[str, Any],
) -> dict[str, Any]:

    db = SessionLocal()

    review_id: int | None = None

    try:

        # --------------------------------------------------
        # 1. Extract event data
        # --------------------------------------------------

        action = payload.get(
            "action"
        )

        pull_request = payload.get(
            "pull_request",
            {},
        )

        repository_data = payload.get(
            "repository",
            {},
        )

        if action not in {
            "opened",
            "synchronized",
        }:
            logger.info(
                "Ignoring GitHub action: %s",
                action,
            )

            return {
                "status": "ignored",
                "action": action,
            }

        pr_number = pull_request.get(
            "number"
        )

        repo_full_name = repository_data.get(
            "full_name"
        )

        if not pr_number:
            raise ValueError(
                "Missing pull request number."
            )

        if not repo_full_name:
            raise ValueError(
                "Missing repository full name."
            )

        if "/" not in repo_full_name:
            raise ValueError(
                "Invalid repository full name."
            )

        # --------------------------------------------------
        # 2. Resolve repository
        # --------------------------------------------------

        repository = _get_or_create_repository(
            db,
            payload,
        )

        # --------------------------------------------------
        # 3. Check if repository is active
        # --------------------------------------------------

        if not repository.is_active:
            logger.info(
                "Repository %s is disabled.",
                repository.repo_name,
            )

            return {
                "status": "ignored",
                "reason": "repository_disabled",
            }

        # --------------------------------------------------
        # 4. Create processing log
        # --------------------------------------------------

        review = ReviewLog(
            repository_id=repository.id,
            pr_number=int(pr_number),
            status="processing",
        )

        db.add(review)

        db.commit()

        db.refresh(review)

        review_id = review.id

        # --------------------------------------------------
        # 5. Parse owner/repository
        # --------------------------------------------------

        owner, repo_name = repo_full_name.split(
            "/",
            1,
        )

        # --------------------------------------------------
        # 6. Fetch PR diff
        # --------------------------------------------------

        logger.info(
            "Fetching diff for %s#%s",
            repo_full_name,
            pr_number,
        )

        diff = fetch_pull_request_diff(
            owner=owner,
            repo=repo_name,
            pull_number=int(pr_number),
        )

        # --------------------------------------------------
        # 7. Run AI analysis
        # --------------------------------------------------

        logger.info(
            "Running AI analysis for %s#%s",
            repo_full_name,
            pr_number,
        )

        feedback = analyze_diff(
            diff
        )

        # --------------------------------------------------
        # 8. Post review to GitHub
        # --------------------------------------------------

        logger.info(
            "Posting CodeSentry review to %s#%s",
            repo_full_name,
            pr_number,
        )

        post_pr_comment(
            owner=owner,
            repo=repo_name,
            pull_number=int(pr_number),
            markdown_body=feedback,
        )

        # --------------------------------------------------
        # 9. Persist successful review
        # --------------------------------------------------

        review.status = "success"

        review.ai_feedback = feedback[:50000]

        db.commit()

        logger.info(
            "Successfully reviewed %s#%s",
            repo_full_name,
            pr_number,
        )

        return {
            "status": "success",
            "review_id": review.id,
            "repository": repo_full_name,
            "pr_number": int(pr_number),
        }

    except GitHubAPIError as exc:

        db.rollback()

        # Retry only transient GitHub errors.
        if (
            exc.retryable
            and self.request.retries < self.max_retries
        ):

            logger.warning(
                "Retrying GitHub operation: %s",
                exc,
            )

            countdown = exc.retry_after
            if countdown is None:
                countdown = min(
                    60,
                    10 * (
                        2 ** self.request.retries
                    ),
                )

            raise self.retry(
                exc=exc,
                countdown=countdown,
            )

        if review_id is not None:
            _mark_review_failed(
                review_id,
                str(exc),
            )

        raise

    except AIReviewError as exc:

        db.rollback()

        if review_id is not None:
            _mark_review_failed(
                review_id,
                str(exc),
            )

        raise

    except Exception as exc:

        db.rollback()

        logger.exception(
            "Unexpected CodeSentry review failure."
        )

        if review_id is not None:
            _mark_review_failed(
                review_id,
                str(exc),
            )

        raise

    finally:

        db.close()