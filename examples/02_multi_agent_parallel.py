"""Multi-agent in parallel — bull/bear/trader each with their own session."""

import asyncio
from agentcli import LLMClient, MemoryStore


async def main():
    client = LLMClient(store=MemoryStore())

    prompts = {
        "bull":   "Give me one sentence: why NVDA might go up this week.",
        "bear":   "Give me one sentence: why NVDA might go down this week.",
        "trader": "Neutral one-liner on NVDA this week.",
    }

    # Each alias gets its own Claude Code session, run concurrently.
    coros = [
        client.chat_async(
            prompt,
            provider="claude",
            owner="team",
            alias=alias,
            timeout=90,
        )
        for alias, prompt in prompts.items()
    ]
    results = await asyncio.gather(*coros)

    for alias, resp in zip(prompts, results):
        print(f"--- {alias} ({resp.session_id[:8]}…) ---")
        print(resp.content)

    # Aggregate token usage per alias.
    stats = client.get_token_stats("team", group_by="alias")
    print("\nUsage by alias:")
    for name, bucket in stats["groups"].items():
        print(f"  {name}: tokens={bucket['total_tokens']} "
              f"cached={bucket['total_cached']} calls={bucket['total_calls']}")


if __name__ == "__main__":
    asyncio.run(main())
