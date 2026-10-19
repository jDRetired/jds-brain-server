import asyncio

from mcp import Client


async def main() -> None:
    async with Client("http://localhost:8000/grok/") as grok:
        r = await grok.call_tool("save_note", {"title": "Grok test", "body": "From the car."})
        print("grok saved:", r.structured_content)

        r = await grok.call_tool("list_notes", {"zone": "inbox"})
        print("grok read refused:", r.is_error)

        try:
            r = await grok.call_tool("save_to_shared", {"title": "x", "body": "y"})
            print("grok save_to_shared refused:", r.is_error)
        except Exception as e:
            print("grok save_to_shared refused:", type(e).__name__)

    async with Client("http://localhost:8000/claude/") as claude:
        r = await claude.call_tool("list_notes", {"zone": "inbox"})
        print("claude sees inbox:", r.structured_content)


asyncio.run(main())
