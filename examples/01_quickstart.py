"""Quickstart: a single async call to Claude Code."""

import asyncio
from agentcli import LLMClient, MemoryStore


async def main():
    client = LLMClient(store=MemoryStore())

    health = client.health_check("claude")
    if not health.ok:
        raise SystemExit(health.suggested_action or health.message)

    resp = await client.chat_async(
        "Say 'hello' in one word.",
        provider="claude",
        owner="demo",
        alias="greeter",
        wall_timeout=60,
        reset_on_instruction_change=True,
    )
    if not resp.content:
        raise SystemExit(resp.suggested_action or resp.error)
    print(resp.content)


if __name__ == "__main__":
    asyncio.run(main())
