import asyncio
import os
import re
import secrets
from collections.abc import AsyncGenerator
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import jwt
from pydantic import AnyHttpUrl
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from mcp.server import MCPServer
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

BRAIN = Path("/srv/jds-brain")
PUBLIC_URL = "https://brain.junkdrawer.tech"
ISSUER = "https://sensible-belief-53-staging.authkit.app"
OWNER_ID = "user_01M5G064PS6BWNGFZKYQZ0AQ68"
SECURITY = TransportSecuritySettings(
    allowed_hosts=["brain.junkdrawer.tech", "127.0.0.1:*", "localhost:*"],
)
JWKS = jwt.PyJWKClient(f"{ISSUER}/oauth2/jwks")

OAUTH_PLATFORMS = {"claude", "chatgpt", "grok"}
TOKEN_ENV = {"rabbit": "RABBIT_TOKEN"}

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

READ_ONLY = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)
SAFE_WRITE = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False
)

def resource_url(platform: str) -> str:
    return f"{PUBLIC_URL}/{platform}/"


class AuthKitVerifier(TokenVerifier):
    """Accepts only AuthKit tokens issued to JD for this exact endpoint."""

    def __init__(self, resource: str) -> None:
        self.resource = resource

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            signing_key = await asyncio.to_thread(JWKS.get_signing_key_from_jwt, token)
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                issuer=ISSUER,
                audience=self.resource,
            )
        except jwt.PyJWTError:
            return None
        if claims.get("sub") != OWNER_ID:
            return None
        return AccessToken(
            token=token,
            client_id=str(claims.get("client_id", "authkit")),
            scopes=[],
            expires_at=claims.get("exp"),
            resource=self.resource,
        )


class StaticTokenVerifier(TokenVerifier):
    """Accepts one fixed secret token, for clients that can't do OAuth."""

    def __init__(self, resource: str, secret: str) -> None:
        self.resource = resource
        self.secret = secret

    async def verify_token(self, token: str) -> AccessToken | None:
        if self.secret and secrets.compare_digest(token, self.secret):
            return AccessToken(token=token, client_id="static", scopes=[], resource=self.resource)
        return None


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
    resource = resource_url(platform)

    if platform in OAUTH_PLATFORMS:
        verifier: TokenVerifier = AuthKitVerifier(resource)
    else:
        verifier = StaticTokenVerifier(resource, os.environ.get(TOKEN_ENV[platform], ""))

    server = MCPServer(
        f"jds-brain-{platform}",
        token_verifier=verifier,
        auth=AuthSettings(
            issuer_url=AnyHttpUrl(ISSUER),
            resource_server_url=AnyHttpUrl(resource),
            required_scopes=[],
            validate_token_resource=True,
        ),
    )

    if perms["read"]:
        @server.tool(annotations=READ_ONLY)
        def list_notes(zone: str) -> list[str]:
            """List the note files in one zone of the brain."""
            folder = zone_folder(zone, perms["read"])
            return sorted(p.name for p in folder.glob("*.md"))

        @server.tool(annotations=READ_ONLY)
        def read_note(zone: str, name: str) -> str:
            """Return the full text of one note."""
            folder = zone_folder(zone, perms["read"])
            check_name(name)
            path = folder / name
            if not path.is_file():
                raise ValueError(f"No such note: {name}")
            return path.read_text()

    @server.tool(annotations=SAFE_WRITE)
    def save_note(title: str, body: str) -> str:
        """Save a new note to your own zone. Returns the new file name."""
        return create_note(perms["write"], platform, title, body)

    if perms["curate"]:

        @server.tool(annotations=SAFE_WRITE)
        def save_to_shared(title: str, body: str) -> str:
            """Save a new note to the shared zone. Only use when JD explicitly asks."""
            return create_note("shared", platform, title, body)

        @server.tool(annotations=SAFE_WRITE)
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


async def protected_resource_metadata(request: Request) -> JSONResponse:
    """Serve each OAuth endpoint's discovery document where its signpost points."""
    platform = request.path_params["platform"]
    if platform not in OAUTH_PLATFORMS:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(
        {
            "resource": resource_url(platform),
            "authorization_servers": [ISSUER],
            "bearer_methods_supported": ["header"],
        }
    )

SERVERS = {name: build_server(name) for name in PERMISSIONS}

ROUTES = [
    Route("/.well-known/oauth-protected-resource/{platform}/", protected_resource_metadata),
    *[
        Mount(f"/{name}", app=srv.streamable_http_app(streamable_http_path="/", transport_security=SECURITY))
        for name, srv in SERVERS.items()
    ],
]


@asynccontextmanager
async def lifespan(app: Starlette) -> AsyncGenerator[None, None]:
    async with AsyncExitStack() as stack:
        for srv in SERVERS.values():
            await stack.enter_async_context(srv.session_manager.run())
        yield


app = Starlette(routes=ROUTES, lifespan=lifespan)
