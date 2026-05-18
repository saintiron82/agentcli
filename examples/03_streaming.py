"""Streaming: print text chunks as the agent produces them."""

import asyncio
from agentcli import LLMClient, MemoryStore


async def main():
    client = LLMClient(store=MemoryStore())

    async for chunk in client.chat_stream(
        "Write a 3-line haiku about CLI tools.",
        provider="claude",
        owner="demo",
        alias="poet",
        idle_timeout=120,
        wall_timeout=300,
    ):
        if chunk.type == "text":
            print(chunk.content, end="", flush=True)
        elif chunk.type == "tool_use":
            name = (chunk.data or {}).get("name", "?")
            print(f"\n[tool_use: {name}]", flush=True)
        elif chunk.type == "done":
            usage = chunk.usage
            print(f"\n\n[done: session={chunk.session_id[:8]}… "
                  f"tokens={usage.total_tokens if usage else 0}]")
        elif chunk.type == "error":
            print(f"\n[error: {chunk.content}]")


if __name__ == "__main__":
    asyncio.run(main())
