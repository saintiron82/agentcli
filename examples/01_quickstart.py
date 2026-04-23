"""Quickstart: a single async call to Claude Code."""

import asyncio
from agentcli import LLMClient, MemoryStore


async def main():
    client = LLMClient(store=MemoryStore())
    resp = await client.chat_async(
        "Say 'hello' in one word.",
        provider="claude",
        owner="demo",
        alias="greeter",
        timeout=60,
    )
    print("Response:", resp.content)
    print("Session ID:", resp.session_id)
    print("Tokens:", resp.tokens)


if __name__ == "__main__":
    asyncio.run(main())
