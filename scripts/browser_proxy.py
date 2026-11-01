"""One shared connection to the candidate's Chrome for every agent session of an assist run.

The Playwright MCP server in --extension mode makes Chrome ask the candidate to "Allow and select" the
tab on each new connection. Agent sessions are short, so that meant a prompt every couple of minutes.
This proxy opens the one upstream connection (a single Playwright MCP server over stdio) and serves it
over HTTP; each agent session connects here instead, and its tool calls go through the same upstream
session, so Chrome is asked once.

  python3 scripts/browser_proxy.py --port 8931 -- npx -y @playwright/mcp@0.0.83 --extension ...
"""

import argparse
import asyncio
import re
import sys

import uvicorn
from mcp import ClientSession, types
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.server.lowlevel import Server


async def main():
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, required=True)
    p.add_argument("upstream", nargs=argparse.REMAINDER, help="-- command and args of the MCP server")
    a = p.parse_args()
    command = a.upstream[1:] if a.upstream[:1] == ["--"] else a.upstream

    params = StdioServerParameters(command=command[0], args=command[1:])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as upstream:
            await upstream.initialize()
            lock = asyncio.Lock()   # sessions run one at a time; this keeps their calls in order anyway

            async def list_tools(ctx, params):
                async with lock:
                    return await upstream.list_tools()

            def refuse(text):
                return types.CallToolResult(content=[types.TextContent(type="text", text=text)])

            async def call_tool(ctx, params: types.CallToolRequestParams):
                args = params.arguments or {}
                async with lock:
                    # Closing the browser, or the last tab, drops the connection to Chrome and the
                    # candidate would have to click "Allow and select" again. Never let that through.
                    if params.name == "browser_close":
                        return refuse("Not needed: the browser stays open between jobs. Navigate this tab to the next page instead.")
                    if params.name == "browser_tabs" and args.get("action") == "close":
                        listing = await upstream.call_tool("browser_tabs", {"action": "list"})
                        text = " ".join(c.text for c in listing.content if getattr(c, "text", None))
                        if len(re.findall(r"(?m)^\s*-?\s*\d+:", text)) <= 1:
                            return refuse("Not closed: this is the only tab, and closing it would disconnect the browser. Navigate it instead.")
                    return await upstream.call_tool(params.name, args)

            server = Server("browser-proxy", on_list_tools=list_tools, on_call_tool=call_tool)
            config = uvicorn.Config(server.streamable_http_app(), host="127.0.0.1", port=a.port, log_level="warning")
            print(f"browser proxy on http://127.0.0.1:{a.port}/mcp", file=sys.stderr, flush=True)
            await uvicorn.Server(config).serve()


if __name__ == "__main__":
    asyncio.run(main())
