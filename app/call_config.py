import os
from pathlib import Path

from app import config as local_config


GRAPH_TENANT_ID = os.environ.get(
    "GRAPH_TENANT_ID",
    getattr(local_config, "GRAPH_TENANT_ID", ""),
)
GRAPH_CLIENT_ID = os.environ.get(
    "GRAPH_CLIENT_ID",
    getattr(local_config, "GRAPH_CLIENT_ID", ""),
)
GRAPH_CLIENT_SECRET = os.environ.get(
    "GRAPH_CLIENT_SECRET",
    getattr(local_config, "GRAPH_CLIENT_SECRET", ""),
)
CALL_WEBHOOK_TOKEN = os.environ.get(
    "CALL_WEBHOOK_TOKEN",
    getattr(local_config, "CALL_WEBHOOK_TOKEN", ""),
)
CALL_PUBLIC_BASE_URL = os.environ.get(
    "CALL_PUBLIC_BASE_URL",
    getattr(
        local_config,
        "CALL_PUBLIC_BASE_URL",
        os.environ.get("RENDER_EXTERNAL_URL", ""),
    ),
).rstrip("/")
CALLBACK_URI = getattr(local_config, "CALLBACK_URI", "")
if not CALLBACK_URI and CALL_PUBLIC_BASE_URL and CALL_WEBHOOK_TOKEN:
    CALLBACK_URI = (
        f"{CALL_PUBLIC_BASE_URL}/calling/notifications/{CALL_WEBHOOK_TOKEN}"
    )

try:
    CALL_RING_SECONDS = int(
        os.environ.get(
            "CALL_RING_SECONDS",
            str(getattr(local_config, "CALL_RING_SECONDS", 7)),
        )
    )
except ValueError as error:
    raise ValueError("CALL_RING_SECONDS must be an integer") from error
if CALL_RING_SECONDS < 1:
    raise ValueError("CALL_RING_SECONDS must be at least 1")

CALL_STATE_DB_PATH = os.environ.get(
    "CALL_STATE_DB_PATH",
    getattr(
        local_config,
        "CALL_STATE_DB_PATH",
        str(Path(__file__).resolve().parent.parent / "call_state.sqlite3"),
    ),
)
WORKSPACE_TIMEZONE = local_config.WORKSPACE_TIMEZONE
