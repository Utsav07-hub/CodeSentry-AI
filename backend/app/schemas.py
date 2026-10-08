from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class PullRequestPayload(BaseModel):
    number: int = Field(gt=0)

    model_config = ConfigDict(
        extra="allow",
    )


class RepositoryPayload(BaseModel):
    full_name: str = Field(min_length=3)

    model_config = ConfigDict(
        extra="allow",
    )


class GitHubPullRequestWebhook(BaseModel):
    action: str

    pull_request: PullRequestPayload

    repository: RepositoryPayload

    sender: dict[str, Any] = Field(
        default_factory=dict,
    )

    model_config = ConfigDict(
        extra="allow",
    )