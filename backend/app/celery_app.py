from celery import Celery

from app.config import settings


celery_app = Celery(
    "codesentry_ai",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["app.tasks"],
)


celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",

    # Expose STARTED state to the dashboard if desired.
    task_track_started=True,

    # Acknowledge only after processing has completed.
    task_acks_late=True,

    # Avoid one worker grabbing a large batch of tasks.
    worker_prefetch_multiplier=1,

    # Protect the worker from indefinitely running reviews.
    task_time_limit=300,
    task_soft_time_limit=240,

    # Celery result TTL.
    result_expires=86400,

    enable_utc=True,
    timezone="UTC",

    # Important when Redis isn't immediately available during startup.
    broker_connection_retry_on_startup=True,

    # Redis broker visibility timeout.
    broker_transport_options={
        "visibility_timeout": 3600,
    },

    # Redis result-backend visibility timeout.
    result_backend_transport_options={
        "visibility_timeout": 3600,
    },
)