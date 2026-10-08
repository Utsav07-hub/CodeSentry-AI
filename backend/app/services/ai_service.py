from langchain_groq import ChatGroq
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser

from app.config import settings


class AIReviewError(RuntimeError):
    """Raised when the AI code review fails."""


llm = ChatGroq(
    model=settings.groq_model,
    api_key=settings.groq_api_key.get_secret_value(),
    temperature=0,
    request_timeout=settings.ai_timeout_seconds,
)


prompt = ChatPromptTemplate.from_messages([
    (
        "system",
        """
You are CodeSentry AI, an expert software engineer and code reviewer.

Analyze the given GitHub pull request diff and find real problems.

Focus on:

- Security vulnerabilities
- Bugs and incorrect logic
- Performance problems
- Code quality and maintainability

Do not complain about minor formatting or personal coding style.

Only report issues that are actually supported by the code diff.

For every important issue, provide:

- Severity: Critical / High / Medium / Low
- Category: Security / Bug / Performance / Code Quality
- File
- Problem
- Why it matters
- Suggested fix

Return the review in clear Markdown.

Use this format:

# CodeSentry AI Review

## Summary

Briefly summarize the changes and overall quality.

## Findings

### [Severity] Issue title

- **Category:** Security / Bug / Performance / Code Quality
- **File:** `file/path`
- **Problem:** Explain the issue.
- **Why it matters:** Explain the impact.
- **Suggested Fix:** Explain how to fix it.

## Overall Recommendation

Choose one:

- Approve
- Approve with changes
- Request changes

If there are no meaningful issues, say so clearly and recommend Approve.
""",
    ),
    (
        "human",
        """
Review the following GitHub pull request diff:

```diff
{diff}
```
""",
    ),
])
review_chain = prompt | llm | StrOutputParser()


def analyze_diff(diff: str) -> str:
    """
    Analyze a GitHub pull request diff using Groq.
    """
    if not diff.strip():
        raise AIReviewError(
            "Pull request diff cannot be empty."
        )

    try:
        result = review_chain.invoke({
            "diff": diff
        })

        return result.strip()

    except Exception as e:
        raise AIReviewError(
            f"AI code review failed: {e}"
        ) from e