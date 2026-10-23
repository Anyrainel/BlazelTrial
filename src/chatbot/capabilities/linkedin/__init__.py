"""LinkedIn posts: a writer agent drafts, code validates and repairs, the user gets it verbatim.

The rules are never left to the model. The writer returns body and hashtags separately; a
too-long body gets one retry with the exact overrun; `validator.assemble` then guarantees the
final post (links stripped, inline hashtags unwrapped, <= 3 tags on the last line, trimmed to
1,300 characters). The tool also sets the turn's reply to the post, so the chat model's own
wording can't break the rules afterwards.
"""

import logging
from functools import cache
from pathlib import Path

from pydantic import BaseModel, Field
from pydantic_ai import Agent, ModelRetry, RunContext

from chatbot.agent.budget import call_context
from chatbot.agent.models import build_model, mechanical_task_settings
from chatbot.capabilities.linkedin import validator
from chatbot.capabilities.registry import capability
from chatbot.config import get_settings

logger = logging.getLogger(__name__)

_BRIEF_CHARS = 6000
_TAG_RESERVE = 60  # room for "\n\n#Tag #Tag #Tag"
BODY_LIMIT = validator.MAX_CHARS - _TAG_RESERVE


class LinkedInPost(BaseModel):
    body: str = Field(description=f"post text without hashtags or links, <= {BODY_LIMIT} chars")
    hashtags: list[str] = Field(description="1-3 hashtags, without '#'", max_length=3)


@cache
def get_writer() -> Agent[dict, LinkedInPost]:
    instructions = (Path(__file__).parent / "writer.md").read_text(encoding="utf-8")
    agent = Agent(
        build_model(get_settings()),
        output_type=LinkedInPost,
        deps_type=dict,
        instructions=instructions.replace("{body_limit}", str(BODY_LIMIT)),
        retries={"output": 2},
        model_settings=mechanical_task_settings(get_settings(), max_tokens=1500),
    )

    @agent.output_validator
    def check_length(ctx: RunContext[dict], post: LinkedInPost) -> LinkedInPost:
        ctx.deps["last"] = post  # kept for the fallback if every attempt is too long
        body = validator.clean_body(post.body)
        if len(body) > BODY_LIMIT:
            raise ModelRetry(
                f"The body is {len(body)} characters; rewrite it to at most {BODY_LIMIT}."
            )
        return post

    return agent


async def write_linkedin_post(ctx: RunContext, brief: str) -> str:
    """Write a LinkedIn post from a factual brief. Returns the final post, which is shown to
    the user exactly as returned.

    Args:
        brief: What to announce, the key facts/dates verbatim, audience, requested tone, and
            the user's standing writing preferences from the instructions.
    """
    prompt = f"Brief:\n{brief[:_BRIEF_CHARS]}"
    seen: dict = {}
    try:
        with call_context(purpose="linkedin"):
            draft = (await get_writer().run(prompt, deps=seen)).output
    except Exception:  # every attempt failed its checks: trim the last draft deterministically
        logger.warning("LinkedIn writer never met the length rule; trimming its last draft")
        draft = seen.get("last") or LinkedInPost(body=brief, hashtags=[])
    post = validator.assemble(draft.body, draft.hashtags)
    ctx.deps.outputs["reply"] = post
    return f"Final post (shown to the user verbatim):\n\n{post}"


CAPABILITY = capability(
    "linkedin",
    "Write a LinkedIn post that follows the platform rules.",
    __file__,
    write_linkedin_post,
)
