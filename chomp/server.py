"""Chomp MCP server: MCPServer (the mcp SDK's FastMCP, renamed in v2) on streamable HTTP."""

from __future__ import annotations

import logging
from contextlib import closing
from typing import TypedDict

from mcp.server.mcpserver import MCPServer
from starlette.requests import Request
from starlette.responses import JSONResponse

from chomp import __version__, db
from chomp.config import Settings

log = logging.getLogger("chomp")

INSTRUCTIONS = """\
Chomp is the user's meal log. Use its tools to look up foods, log what they ate, and review totals.
Every calorie and macro number you show must come from a Chomp tool result. Never estimate or
invent nutrition values; if a food can't be found, say so and ask for a barcode or label photo.\
"""


class ServerInfo(TypedDict):
    name: str
    version: str
    schema_version: int
    tz: str


def build_server(settings: Settings) -> MCPServer:
    server = MCPServer(name="chomp", version=__version__, instructions=INSTRUCTIONS)

    def connect():
        return closing(db.connect(settings.db_path))

    @server.custom_route("/health", methods=["GET"])
    async def health(request: Request) -> JSONResponse:
        try:
            with connect() as conn:
                version = db.schema_version(conn)
        except Exception:
            log.exception("health check failed")
            return JSONResponse({"status": "error"}, status_code=503)
        return JSONResponse({"status": "ok", "schema_version": version})

    @server.tool()
    def server_info() -> ServerInfo:
        """Report Chomp's version, database schema version, and display timezone.

        Use this to check that the Chomp connector is reachable. It returns no nutrition data.
        """
        with connect() as conn:
            version = db.schema_version(conn)
        return {
            "name": "chomp",
            "version": __version__,
            "schema_version": version,
            "tz": settings.tz,
        }

    return server


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = Settings.from_env()
    with closing(db.connect(settings.db_path)) as conn:
        version = db.migrate(conn, settings.schema_path, settings.migrations_dir)
    log.info("database ready at schema version %d", version)
    server = build_server(settings)
    server.run("streamable-http", host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
