"""Narrow request-only normalization for Claude Code's token-budget notices.

Some OpenRouter routes hoist historical system messages ahead of the conversation.
A newly appended counter then invalidates the reusable conversation prefix. Keep
the notice and its position, but send these synthetic notices as user context.
Leading instructions and every other message are left untouched.
"""

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from inspect_ai.model import ChatMessage

TOKEN_COUNTER_NORMALIZATION_VERSION = "counter-user-v1"
_TOKEN_COUNTER = re.compile(r"<total_tokens>[0-9]+ tokens left</total_tokens>")


def normalize_token_counters(messages: list["ChatMessage"]) -> tuple[list["ChatMessage"], int]:
    """Return effective request messages and the number of counter role changes.

    Only standalone string counters in non-leading ``ChatMessageSystem`` objects
    change. Never mutate the scaffold's list or messages. Unchanged objects retain
    identity; an unchanged request returns the original list. This does not enable
    whole-response caching or alter spending enforcement.
    """
    from inspect_ai.model import ChatMessageSystem, ChatMessageUser

    effective = None
    count = 0
    historical = False
    for index, message in enumerate(messages):
        if not isinstance(message, ChatMessageSystem):
            historical = True
        elif historical and isinstance(message.content, str) and _TOKEN_COUNTER.fullmatch(message.content):
            if effective is None:
                effective = list(messages)
            # Construct the actual user class: changing only `.role` on a system
            # model leaves serializers which branch on its class treating it as
            # system. These fields are already validated common message fields.
            effective[index] = ChatMessageUser.model_construct(
                id=message.id, content=message.content,
                source=message.source, metadata=message.metadata,
            )
            # Inspect's post-init generates an ID even for model_construct when
            # the original explicitly had none (e.g. deserialized messages).
            effective[index].id = message.id
            count += 1
    return (messages if effective is None else effective), count
