"""Privacy guard: nothing leaves this computer unless the user deliberately allows it.

The only places where the app can send anything over the network are the AI
model (LLM_BASE_URL) and Langfuse tracing (LANGFUSE_BASE_URL). Both are checked
here before use:

    local     localhost, 127.0.0.0/8, ::1                         -> allowed
    private   RFC 1918 addresses (10/8, 172.16/12, 192.168/16) and
              hosts ending in .local, .internal or .lan          -> allowed
    external  anything else (a cloud service on the internet)    -> blocked unless
              ALLOW_EXTERNAL_AI=true / ALLOW_EXTERNAL_LANGFUSE=true is set in .env

No function here makes a network call or resolves a host name: the decision is
made from the address as written.
"""
from __future__ import annotations

import ipaddress
import os
from contextvars import ContextVar
import tomllib
from urllib.parse import urlsplit

from app.core.config import PROJECT_ROOT, data_dir

AI_BLOCKED_REASON = "AI endpoint is outside your network (set ALLOW_EXTERNAL_AI=true to allow)"

# The dataset class of the job being worked on: "client" (default) or "public". Set by jobs.load_job /
# create_job (and page 1); None = no job (e.g. the AI settings page): no class restriction.
DATASET_CLASSES = ("client", "public")
_dataset_class: ContextVar[str | None] = ContextVar("dataset_class", default=None)


def set_dataset_class(value: str | None) -> None:
    """Remember the current job's dataset class; anything unknown counts as "client"."""
    _dataset_class.set(None if value is None else (value if value in DATASET_CLASSES else "client"))


def dataset_class() -> str | None:
    return _dataset_class.get()


def external_for_client() -> bool:
    """settings ai.allow_external_for_client: client jobs may use providers that are not private."""
    from app.core.config import settings
    return bool(settings()["ai"].get("allow_external_for_client", False))


def provider_private(p: dict) -> bool:
    """A provider is private when ai_providers.yaml says `private: true`, or (the custom LLM_BASE_URL
    endpoint) when its address is on this computer or the private network."""
    if p.get("private") is not None:
        return bool(p["private"])
    return classify_endpoint(p.get("base_url") or "") in ("local", "private")


def client_block(p: dict) -> str | None:
    """Why a client job may not use this provider, or None."""
    if dataset_class() == "client" and not provider_private(p) and not external_for_client():
        return (f"{p['key']} is not marked private and this is a client dataset "
                f"(set ai.allow_external_for_client: true, or mark the job public on Import data)")
    return None
_PRIVATE_NETWORKS = [ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")]
_PRIVATE_SUFFIXES = (".local", ".internal", ".lan")


def _flag(name: str) -> bool:
    return os.getenv(name, "false").strip().lower() in ("1", "true", "yes", "on")


def classify_endpoint(url: str | None) -> str:
    """"local" | "private" | "external" | "unset", from the address only (no DNS lookup)."""
    text = (url or "").strip()
    if not text:
        return "unset"
    host = urlsplit(text if "//" in text else f"//{text}").hostname or ""
    host = host.strip("[]").rstrip(".").lower()
    if not host:
        return "unset"
    if host == "localhost" or host.endswith(".localhost"):
        return "local"
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return "private" if host.endswith(_PRIVATE_SUFFIXES) else "external"
    if ip.is_loopback:
        return "local"
    if any(ip in net for net in _PRIVATE_NETWORKS):
        return "private"
    return "external"


def ai_endpoint_kind() -> str:
    """Where the AI would be called. An empty LLM_BASE_URL means OpenAI itself: external."""
    kind = classify_endpoint(os.getenv("LLM_BASE_URL"))
    return "external" if kind == "unset" else kind


def ai_allowed() -> tuple[bool, str | None]:
    """Whether the AI endpoint may be called (the band and AI_ENABLED are checked elsewhere).

    Only an external endpoint can be refused: an explicit external LLM_BASE_URL, or
    an empty one (OpenAI's own service) while AI_ENABLED is on.
    """
    kind = classify_endpoint(os.getenv("LLM_BASE_URL"))
    ai_on = _flag("AI_ENABLED")
    if (kind == "external" or (kind == "unset" and ai_on)) and not _flag("ALLOW_EXTERNAL_AI"):
        return False, AI_BLOCKED_REASON
    return True, None


def langfuse_allowed() -> bool:
    """False when LANGFUSE_BASE_URL is on the internet and ALLOW_EXTERNAL_LANGFUSE is not true."""
    return classify_endpoint(os.getenv("LANGFUSE_BASE_URL")) != "external" or _flag("ALLOW_EXTERNAL_LANGFUSE")


def usage_stats_on() -> bool:
    """Whether Streamlit would send anonymous usage statistics (from .streamlit/config.toml)."""
    try:
        with open(PROJECT_ROOT / ".streamlit" / "config.toml", "rb") as f:
            return bool(tomllib.load(f).get("browser", {}).get("gatherUsageStats", True))
    except (OSError, tomllib.TOMLDecodeError):
        return True     # Streamlit's own default


def status_line() -> str:
    """One line for the sidebar and the command line: where data is, and what may leave."""
    if not _flag("AI_ENABLED"):
        ai = "off"
    else:
        from app.core import ai_client   # local import avoids a cycle
        names = [p.get("label") or k for k, p in ai_client.providers().items()
                 if k != "custom" and ai_client.has_key(p) and p.get("model")]
        kind = ai_endpoint_kind() if "custom" in ai_client.providers() or not names else "external"
        ai = kind if kind != "external" else f"external ({'allowed' if _flag('ALLOW_EXTERNAL_AI') else 'blocked'})"
        if names:
            ai += " · " + ", ".join(names)
    keys = all(os.getenv(k) for k in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_BASE_URL"))
    if not keys:
        langfuse = "off"
    elif classify_endpoint(os.getenv("LANGFUSE_BASE_URL")) == "external":
        langfuse = f"external ({'allowed' if langfuse_allowed() else 'blocked'})"
    else:
        langfuse = "self-hosted"
    stats = "on" if usage_stats_on() else "off"
    return f"🔒 Data folder: {data_dir()} · AI: {ai} · Langfuse: {langfuse} · Usage stats: {stats}"
