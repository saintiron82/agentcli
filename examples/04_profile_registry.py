"""Agent profile + materialization.

Folder layout:
    ~/agents-registry/
        bull-analyst/
            AGENTS.md
            profile.json
        bear-analyst/
            AGENTS.md
            profile.json
"""

import asyncio
import json
import tempfile
from pathlib import Path

from agentcli import AgentRegistry, LLMClient, MemoryStore


async def main():
    # Build a throwaway registry dir for demo purposes.
    with tempfile.TemporaryDirectory() as registry_root:
        root = Path(registry_root)
        for name, instructions in [
            ("bull-analyst", "You are a bull-case analyst. Focus on growth."),
            ("bear-analyst", "You are a bear-case analyst. Focus on risks."),
        ]:
            d = root / name
            d.mkdir()
            (d / "AGENTS.md").write_text(instructions, encoding="utf-8")
            (d / "profile.json").write_text(
                json.dumps({"model": "sonnet", "provider": "claude"}),
                encoding="utf-8")

        registry = AgentRegistry.from_dir(root)
        print("Loaded profiles:", registry.names())

        client = LLMClient(store=MemoryStore())

        # Materialize into a project cwd, then call.
        with tempfile.TemporaryDirectory() as project_dir:
            bull = registry.get("bull-analyst")
            manifest = bull.materialize(project_dir)
            print("Materialized:", [Path(p).name for p in manifest["files_written"]])

            resp = await bull.chat_async(
                "NVDA outlook in one line?",
                client=client, owner="demo",
                cwd=project_dir, materialize=False,  # already materialized above
                timeout=60,
            )
            print(f"\n{bull.name}: {resp.content}")


if __name__ == "__main__":
    asyncio.run(main())
