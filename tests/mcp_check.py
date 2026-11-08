"""Does the MCP server start over stdio and list its tools? (no phone needed)
Also calls `status`, which must answer even when phoneuse isn't running.

Run: .venv\\Scripts\\python tests\\mcp_check.py
"""
import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXE = os.path.join(ROOT, "bin", "phoneuse-mcp.exe" if sys.platform == "win32" else "phoneuse-mcp")


async def main():
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    async with stdio_client(StdioServerParameters(command=EXE, args=[])) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = sorted(t.name for t in (await s.list_tools()).tools)
            print("tools:", ", ".join(tools))
            res = await s.call_tool("status", {})
            print("status:", res.content[0].text[:300])
            ok = {"tap", "look", "scroll_by", "open_app", "status"} <= set(tools)
            print("PASS" if ok else "FAIL")
            return ok


if __name__ == "__main__":
    sys.exit(0 if asyncio.run(main()) else 1)
