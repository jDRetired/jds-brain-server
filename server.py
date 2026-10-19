import re
from datetime import datetime, timezone
from pathlib import Path

from collections.abc import AsyncGenerator
from contextlib import AsyncExitStack, asynccontextmanager

from starlette.applications import Starlette
from starlette.routing import Mount

from mcp.server import MCPServer

BRAIN = Path("/srv/jds-brain")

ZONE_PATHS = {
    "claude": "claude",
    "chatgpt": "chatgpt",
    "inbox": "inbox",
    "archive": "inbox/archive",
    "shared": "shared",
}

PERMISSIONS = {
    "claude":  {"read": {"claude", "inbox", "archive", "shared"}, "write": "claude",  "curate": True},
    "chatgpt": {"read": {"chatgpt", "inbox", "archive", "shared"}, "write": "chatgpt", "curate": True},
    "grok":    {"read": set(),                                    "write": "inbox",   "curate": False},
    "rabbit":  {"read": {"inbox", "shared"},                      "write": "inbox",   "curate": False},
}


def zone_folder(zone: str, allowed: set[str]) -> Path:
    if zone not in allowed:
        raise ValueError(f"Zone not allowed: {zone}")
    return BRAIN / ZONE_PATHS[zone]


def check_name(name: str) -> None:
    if Path(name).name != name or not name.endswith(".md"):
        raise ValueError("Note name must be a plain .md file name")


def slugify(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return slug[:40] or "note"


def create_note(zone: str, platform: str, title: str, body: str) -> str:
    now = datetime.now(timezone.utc)
    folder = BRAIN / ZONE_PATHS[zone]
    base = f"{now:%Y-%m-%d-%H%M}-{platform}-{slugify(title)}"
    header = f"---\nsource: {platform}\ncreated: {now:%Y-%m-%dT%H:%MZ}\n---\n\n"
    for n in range(1, 100):
        name = f"{base}.md" if n == 1 else f"{base}-{n}.md"
        try:
            with open(folder / name, "x") as f:
                f.write(header + body + "\n")
            return name
        except FileExistsError:
            continue
    raise ValueError("Too many notes with this name this minute")


def build_server(platform: str) -> MCPServer:
    perms = PERMISSIONS[platform]
    server = MCPServer(f"jds-brain-{platform}")

    @server.tool()
    def list_notes(zone: str) -> list[str]:
        """List the note files in one zone of the brain."""
        folder = zone_folder(zone, perms["read"])
        return sorted(p.name for p in folder.glob("*.md"))

    @server.tool()
    def read_note(zone: str, name: str) -> str:
        """Return the full text of one note."""
        folder = zone_folder(zone, perms["read"])
        check_name(name)
        path = folder / name
        if not path.is_file():
            raise ValueError(f"No such note: {name}")
        return path.read_text()

    @server.tool()
    def save_note(title: str, body: str) -> str:
        """Save a new note to your own zone. Returns the new file name."""
        return create_note(perms["write"], platform, title, body)

    if perms["curate"]:

        @server.tool()
        def save_to_shared(title: str, body: str) -> str:
            """Save a new note to the shared zone. Only use when JD explicitly asks."""
            return create_note("shared", platform, title, body)

        @server.tool()
        def archive_note(name: str) -> str:
            """Move a processed note from the inbox into the inbox archive."""
            check_name(name)
            src = BRAIN / "inbox" / name
            dest = BRAIN / "inbox" / "archive" / name
            if not src.is_file():
                raise ValueError(f"No such inbox note: {name}")
            if dest.exists():
                raise ValueError(f"Already archived: {name}")
            src.rename(dest)
            return f"Archived {name}"

    return server


SERVERS = {name: build_server(name) for name in PERMISSIONS}

ROUTES = [
    Mount(f"/{name}", app=srv.streamable_http_app(streamable_http_path="/"))
    for name, srv in SERVERS.items()
]


@asynccontextmanager
async def lifespan(app: Starlette) -> AsyncGenerator[None, None]:
    async with AsyncExitStack() as stack:
        for srv in SERVERS.values():
            await stack.enter_async_context(srv.session_manager.run())
        yield


app = Starlette(routes=ROUTES, lifespan=lifespan)
