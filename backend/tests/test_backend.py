import hashlib
import hmac
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient
from pydantic import SecretStr
from redis import Redis
from sqlalchemy import inspect

from app import main
from app.celery_app import celery_app
from app.config import settings
from app.database import engine
from app.models import Repository, ReviewLog, User
from app.services import github_service
from app.services.ai_service import AIReviewError, analyze_diff, llm
from app.services.github_service import GitHubAPIError
from app.tasks import (
    _get_or_create_repository,
    process_pull_request_task,
)


class FakeSession:
    def __init__(self) -> None:
        self.objects: list[object] = []
        self.records: dict[int, object] = {}
        self.commits = 0
        self.closed = False

    def add(self, instance: object) -> None:
        self.objects.append(instance)

    def commit(self) -> None:
        self.commits += 1

    def flush(self) -> None:
        for instance in self.objects:
            if isinstance(instance, User) and instance.id is None:
                instance.id = 101
            elif isinstance(instance, Repository) and instance.id is None:
                instance.id = 202

    def refresh(self, instance: ReviewLog) -> None:
        instance.id = 303

    def scalar(self, _statement: object) -> None:
        return None

    def get(self, _model: type, identifier: int) -> object | None:
        return self.records.get(identifier)

    def rollback(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()


class BackendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.client = TestClient(main.app)

    def _signed_webhook(
        self,
        body: bytes,
        event: str = "pull_request",
        include_signature: bool = True,
    ):
        signature = hmac.new(
            settings.github_webhook_secret.get_secret_value().encode(),
            body,
            hashlib.sha256,
        ).hexdigest()
        headers = {"X-GitHub-Event": event}
        if include_signature:
            headers["X-Hub-Signature-256"] = f"sha256={signature}"
        return self.client.post(
            "/webhook/github",
            content=body,
            headers=headers,
        )

    def _using_test_webhook_secret(self):
        return patch.object(
            settings,
            "github_webhook_secret",
            SecretStr("test-only-webhook-secret"),
        )

    def test_configuration_and_celery_registration(self) -> None:
        self.assertTrue(settings.database_url)
        self.assertTrue(settings.redis_url)
        self.assertEqual(llm.request_timeout, settings.ai_timeout_seconds)
        self.assertIn(
            "app.tasks.process_pull_request_task",
            celery_app.tasks,
        )

    def test_postgres_and_redis_connectivity(self) -> None:
        with engine.connect() as connection:
            self.assertEqual(
                connection.exec_driver_sql("SELECT 1").scalar_one(),
                1,
            )
        self.assertTrue(inspect(engine).has_table("review_logs"))
        with Redis.from_url(settings.redis_url) as redis_client:
            self.assertTrue(redis_client.ping())

    def test_github_api_authentication(self) -> None:
        response = github_service._request(
            "GET",
            f"{settings.github_api_url.rstrip('/')}/user",
        )
        self.assertEqual(response.status_code, 200)

    def test_groq_analysis(self) -> None:
        diff = (
            "diff --git a/example.py b/example.py\n"
            "--- a/example.py\n"
            "+++ b/example.py\n"
            "@@ -1 +1 @@\n"
            "-print('old')\n"
            "+print('new')\n"
        )
        try:
            result = analyze_diff(diff)
        except AIReviewError:
            raise AssertionError(
                "Groq analysis failed; details suppressed."
            ) from None
        self.assertTrue(result.strip())

    def test_webhook_verifies_signature_and_queues_supported_actions(self) -> None:
        payload = {
            "action": "opened",
            "pull_request": {"number": 7},
            "repository": {"full_name": "owner/repo"},
            "sender": {},
        }
        body = json.dumps(payload).encode()
        task = SimpleNamespace(id="test-task")

        with (
            self._using_test_webhook_secret(),
            patch.object(
                main.process_pull_request_task,
                "delay",
                return_value=task,
            ) as enqueue,
        ):
            response = self._signed_webhook(body)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["task_id"], task.id)
            enqueue.assert_called_once_with(payload)

            payload["action"] = "synchronized"
            response = self._signed_webhook(json.dumps(payload).encode())
            self.assertEqual(response.status_code, 200)
            self.assertEqual(enqueue.call_count, 2)

            payload["action"] = "closed"
            response = self._signed_webhook(json.dumps(payload).encode())
            self.assertEqual(response.status_code, 200)
            self.assertEqual(enqueue.call_count, 2)

            response = self._signed_webhook(body, event="push")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(enqueue.call_count, 2)

    def test_webhook_rejects_bad_signature_and_payload(self) -> None:
        payload = {
            "action": "opened",
            "pull_request": {"number": 7},
            "repository": {"full_name": "owner/repo"},
        }
        body = json.dumps(payload).encode()
        with self._using_test_webhook_secret():
            self.assertEqual(
                self._signed_webhook(
                    body,
                    include_signature=False,
                ).status_code,
                401,
            )
            self.assertEqual(self._signed_webhook(b"{").status_code, 400)
            self.assertEqual(self._signed_webhook(b"\xff").status_code, 400)
            self.assertEqual(self._signed_webhook(b"{}").status_code, 422)

    def test_webhook_reports_enqueue_failure(self) -> None:
        payload = {
            "action": "opened",
            "pull_request": {"number": 7},
            "repository": {"full_name": "owner/repo"},
        }
        with (
            self._using_test_webhook_secret(),
            patch.object(
                main.process_pull_request_task,
                "delay",
                side_effect=RuntimeError("broker unavailable"),
            ),
            patch.object(main.logger, "exception") as log_exception,
        ):
            response = self._signed_webhook(json.dumps(payload).encode())
        self.assertEqual(response.status_code, 503)
        log_exception.assert_called_once()

    def test_repository_is_created_from_github_owner(self) -> None:
        db = FakeSession()
        repository = _get_or_create_repository(
            db,
            {
                "repository": {
                    "full_name": "owner/repo",
                    "owner": {"id": 1234, "login": "owner"},
                }
            },
        )
        user = next(item for item in db.objects if isinstance(item, User))
        self.assertEqual(user.github_id, 1234)
        self.assertEqual(repository.user_id, user.id)
        self.assertEqual(repository.repo_name, "owner/repo")

    def test_task_persists_and_posts_successful_review(self) -> None:
        db = FakeSession()
        repository = SimpleNamespace(
            id=20,
            is_active=True,
            repo_name="owner/repo",
        )
        payload = {
            "action": "opened",
            "pull_request": {"number": 7},
            "repository": {"full_name": "owner/repo"},
        }

        def fetch_diff(**_kwargs: object) -> str:
            review = next(
                item for item in db.objects
                if isinstance(item, ReviewLog)
            )
            self.assertEqual(review.status, "processing")
            self.assertEqual(review.repository_id, repository.id)
            self.assertEqual(review.pr_number, 7)
            return "test diff"

        with (
            patch("app.tasks.SessionLocal", return_value=db),
            patch("app.tasks._get_or_create_repository", return_value=repository),
            patch("app.tasks.fetch_pull_request_diff", side_effect=fetch_diff),
            patch("app.tasks.analyze_diff", return_value="test review") as analyze,
            patch("app.tasks.post_pr_comment") as post_comment,
        ):
            result = process_pull_request_task.run(payload)

        review = next(item for item in db.objects if isinstance(item, ReviewLog))
        self.assertEqual(result["status"], "success")
        self.assertEqual(review.status, "success")
        self.assertEqual(review.ai_feedback, "test review")
        self.assertEqual(db.commits, 2)
        self.assertTrue(db.closed)
        analyze.assert_called_once_with("test diff")
        post_comment.assert_called_once_with(
            owner="owner",
            repo="repo",
            pull_number=7,
            markdown_body="test review",
        )

    def test_task_marks_nonretryable_github_failure_as_failed(self) -> None:
        task_db = FakeSession()
        failure_db = FakeSession()
        repository = SimpleNamespace(
            id=20,
            is_active=True,
            repo_name="owner/repo",
        )
        payload = {
            "action": "opened",
            "pull_request": {"number": 7},
            "repository": {"full_name": "owner/repo"},
        }

        def session_factory() -> FakeSession:
            if not task_db.objects:
                return task_db
            review = next(
                item for item in task_db.objects
                if isinstance(item, ReviewLog)
            )
            failure_db.records[review.id] = review
            return failure_db

        error = GitHubAPIError(
            "Not found",
            status_code=404,
            retryable=False,
        )
        with (
            patch("app.tasks.SessionLocal", side_effect=session_factory),
            patch("app.tasks._get_or_create_repository", return_value=repository),
            patch("app.tasks.fetch_pull_request_diff", side_effect=error),
        ):
            with self.assertRaises(GitHubAPIError):
                process_pull_request_task.run(payload)

        review = next(
            item for item in task_db.objects
            if isinstance(item, ReviewLog)
        )
        self.assertEqual(review.status, "failed")
        self.assertEqual(failure_db.commits, 1)

    def test_task_retries_retryable_github_failure(self) -> None:
        db = FakeSession()
        repository = SimpleNamespace(
            id=20,
            is_active=True,
            repo_name="owner/repo",
        )
        payload = {
            "action": "opened",
            "pull_request": {"number": 7},
            "repository": {"full_name": "owner/repo"},
        }
        error = GitHubAPIError(
            "rate limited",
            status_code=403,
            retryable=True,
            retry_after=45,
        )
        with (
            patch("app.tasks.SessionLocal", return_value=db),
            patch("app.tasks._get_or_create_repository", return_value=repository),
            patch("app.tasks.fetch_pull_request_diff", side_effect=error),
            patch.object(
                process_pull_request_task,
                "retry",
                side_effect=RuntimeError("retry scheduled"),
            ) as retry,
        ):
            with self.assertRaisesRegex(RuntimeError, "retry scheduled"):
                process_pull_request_task.run(payload)

        retry.assert_called_once()
        self.assertEqual(retry.call_args.kwargs["countdown"], 45)
        self.assertTrue(db.closed)

    def test_github_rate_limit_is_retryable(self) -> None:
        response = SimpleNamespace(
            ok=False,
            status_code=403,
            headers={
                "X-RateLimit-Remaining": "0",
                "Retry-After": "45",
            },
            json=lambda: {"message": "API rate limit exceeded"},
            text="",
        )
        with self.assertRaises(GitHubAPIError) as raised:
            github_service._raise_for_github_response(
                response,
                "testing",
            )
        self.assertTrue(raised.exception.retryable)
        self.assertEqual(raised.exception.retry_after, 45.0)

    def test_ordinary_github_forbidden_response_is_not_retryable(self) -> None:
        response = SimpleNamespace(
            ok=False,
            status_code=403,
            headers={},
            json=lambda: {"message": "Forbidden"},
            text="",
        )
        with self.assertRaises(GitHubAPIError) as raised:
            github_service._raise_for_github_response(
                response,
                "testing",
            )
        self.assertFalse(raised.exception.retryable)

    def test_comment_upsert_updates_existing_comment_after_retry(self) -> None:
        def response(body: object) -> SimpleNamespace:
            return SimpleNamespace(
                ok=True,
                status_code=200,
                headers={},
                json=lambda: body,
                text="",
            )

        with patch.object(
            github_service,
            "_request",
            side_effect=[
                response([]),
                response({"id": 42}),
                response([{"id": 42, "body": "<!-- codesentry-ai -->\nold"}]),
                response({"id": 42}),
            ],
        ) as request:
            github_service.upsert_pr_comment(
                "owner",
                "repo",
                7,
                "review",
            )
            github_service.upsert_pr_comment(
                "owner",
                "repo",
                7,
                "review",
            )

        methods = [call.args[0] for call in request.call_args_list]
        self.assertEqual(methods.count("POST"), 1)
        self.assertEqual(methods.count("PATCH"), 1)


if __name__ == "__main__":
    unittest.main()
