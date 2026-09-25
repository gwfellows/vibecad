"""Stdio MCP server exposing a VibeCAD Workspace (for Claude Code and other MCP clients).

Run with `vibecad-mcp`. The heavy CAD imports happen lazily, warmed in a background thread, so the
server answers the client's handshake immediately.
"""
from __future__ import annotations

import threading

from mcp.server.mcpserver import Image, MCPServer

from .workspace import GUIDE_PATH, TOOLS, Workspace, call

server = MCPServer(name="vibecad",
                   instructions=GUIDE_PATH.read_text() if GUIDE_PATH.exists() else "VibeCAD CAD tools.")
ws = Workspace(".")


def _make(spec):
    def fn(**kwargs):
        text, png = call(ws, spec["name"], kwargs)
        return Image(data=png, format="png") if png is not None else text

    fn.__name__ = spec["name"]
    fn.__doc__ = spec["desc"]
    return fn


def _register():
    import inspect

    for spec in TOOLS:
        fn = _make(spec)
        params = []
        for k, v in spec["props"].items():
            ann = {"string": str, "number": float, "array": list, "object": dict}[v["type"]]
            if v["type"] == "array" and v["items"].get("type") == "object":
                ann = list[dict]
            elif v["type"] == "array":
                ann = list[str]
            default = inspect.Parameter.empty if k in spec["req"] else None
            params.append(inspect.Parameter(k, inspect.Parameter.KEYWORD_ONLY, default=default,
                                            annotation=ann if default is inspect.Parameter.empty else ann | None))
        fn.__signature__ = inspect.Signature(params)
        fn.__annotations__ = {p.name: p.annotation for p in params}
        server.tool(name=spec["name"], description=spec["desc"])(fn)


_register()
_warm = threading.Thread(target=lambda: __import__("vibecad.session") and __import__("vibecad.render"), daemon=True)


def main() -> None:
    _warm.start()
    server.run("stdio")


if __name__ == "__main__":
    main()
