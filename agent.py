"""Rebuild the top of a landing page as one editable Shopify section.

Usage:
    python agent.py <url>
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from claude_agent_sdk import (  # noqa: E402
    AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient, ResultMessage,
    TextBlock, ToolResultBlock, ToolUseBlock, UserMessage,
)

from prompts import SYSTEM_PROMPT, task_prompt  # noqa: E402
from tools.agent_tools import OUT, SERVER, STATE, TOOL_NAMES  # noqa: E402

DIM, CYAN, GREEN, RED, BOLD, RESET = "\033[2m", "\033[36m", "\033[32m", "\033[31m", "\033[1m", "\033[0m"
BUILTINS = ["Bash", "Read", "Write", "Edit", "MultiEdit", "Glob", "Grep", "WebFetch", "WebSearch",
            "NotebookEdit", "Task", "TodoWrite"]


def short(obj, n=160) -> str:
    s = json.dumps(obj) if not isinstance(obj, str) else obj
    return s if len(s) <= n else s[:n] + "..."


async def main(url: str, max_turns: int) -> int:
    for var in ("ANTHROPIC_API_KEY", "SHOPIFY_STORE"):
        if not os.getenv(var):
            print(f"{RED}Missing {var} in .env{RESET}")
            return 1
    OUT.mkdir(exist_ok=True)

    options = ClaudeAgentOptions(
        system_prompt=SYSTEM_PROMPT,
        model=os.getenv("CLAUDE_MODEL", "claude-sonnet-5-5"),
        mcp_servers={"shopify_rebuild": SERVER},
        allowed_tools=TOOL_NAMES,
        disallowed_tools=BUILTINS,
        permission_mode="bypassPermissions",
        max_turns=max_turns,
        # Screenshot-heavy tool results are larger than the SDK's 1 MB default.
        max_buffer_size=20 * 1024 * 1024,
    )

    print(f"{BOLD}Landing page rebuild agent{RESET}\n{DIM}target: {url}\nstore:  {os.getenv('SHOPIFY_STORE')}{RESET}\n")
    t0 = time.time()
    result: ResultMessage | None = None
    async with ClaudeSDKClient(options=options) as client:
        await client.query(task_prompt(url))
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for b in msg.content:
                    if isinstance(b, TextBlock) and b.text.strip():
                        print(f"\n{b.text.strip()}\n")
                    elif isinstance(b, ToolUseBlock):
                        name = b.name.replace("mcp__shopify_rebuild__", "")
                        args = {k: (f"<{len(v)} chars>" if isinstance(v, str) and len(v) > 200 else v)
                                for k, v in b.input.items()}
                        print(f"{CYAN}> {name}{RESET} {DIM}{short(args)}{RESET}")
            elif isinstance(msg, UserMessage) and isinstance(msg.content, list):
                for b in msg.content:
                    if isinstance(b, ToolResultBlock):
                        ok = not b.is_error
                        texts = [c.get("text", "") for c in (b.content or []) if isinstance(c, dict) and c.get("type") == "text"] \
                            if isinstance(b.content, list) else [str(b.content)]
                        imgs = sum(1 for c in (b.content or []) if isinstance(c, dict) and c.get("type") == "image") \
                            if isinstance(b.content, list) else 0
                        tag = f"{GREEN}  ok{RESET}" if ok else f"{RED}  error{RESET}"
                        extra = f" [{imgs} screenshots]" if imgs else ""
                        print(f"{tag}{extra} {DIM}{short(' '.join(texts), 220)}{RESET}")
            elif isinstance(msg, ResultMessage):
                result = msg

    elapsed = time.time() - t0
    cost = getattr(result, "total_cost_usd", None) if result else None
    summary = {
        "url": url,
        "storefront_url": STATE["storefront_url"],
        "section": STATE["section_type"],
        "compare_passes": STATE["captures"],
        "turns": getattr(result, "num_turns", None),
        "duration_s": round(elapsed, 1),
        "api_cost_usd": round(cost, 4) if cost is not None else None,
        "usage": getattr(result, "usage", None),
    }
    (OUT / "run_summary.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    print(f"\n{BOLD}Done in {elapsed/60:.1f} min{RESET}")
    print(f"Storefront: {STATE['storefront_url']}")
    print(f"API cost:   ${cost:.4f}" if cost is not None else "API cost:   (not reported)")
    print(f"{DIM}Files and screenshots in ./output/{RESET}")
    return 0 if result and not getattr(result, "is_error", False) else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--max-turns", type=int, default=60)
    a = ap.parse_args()
    sys.exit(asyncio.run(main(a.url, a.max_turns)))