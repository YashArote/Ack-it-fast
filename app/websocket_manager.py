"""WebSocket connection manager for real-time P1 assignment notifications.

Clients connect to  /ws/{sg_id}  and receive a JSON push every time a P1
incident or RITM is roster-assigned to the agent identified by that SG_ID.
"""
import asyncio
import json
import logging
from collections import defaultdict

from fastapi import WebSocket

logger = logging.getLogger("ackitfast.ws")


class ConnectionManager:
    """Manages active WebSocket connections keyed by SG_ID (upper-cased)."""

    def __init__(self) -> None:
        # { "P5647570": [<WebSocket>, ...], ... }
        self._connections: dict[str, list[WebSocket]] = defaultdict(list)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self, sg_id: str, websocket: WebSocket) -> None:
        await websocket.accept()
        key = sg_id.strip().upper()
        self._connections[key].append(websocket)
        logger.info(
            "WebSocket client connected",
            extra={"sg_id": key, "total_listeners": len(self._connections[key])},
        )
        # Send an immediate ack so the client knows it is live
        await websocket.send_text(
            json.dumps({"type": "subscribed", "sg_id": key})
        )

    def disconnect(self, sg_id: str, websocket: WebSocket) -> None:
        key = sg_id.strip().upper()
        conns = self._connections.get(key, [])
        if websocket in conns:
            conns.remove(websocket)
        if not conns:
            self._connections.pop(key, None)
        logger.info("WebSocket client disconnected", extra={"sg_id": key})

    # ------------------------------------------------------------------
    # Sending
    # ------------------------------------------------------------------

    async def notify(self, sg_id: str, payload: dict) -> None:
        """Push a JSON payload to every client subscribed for this SG_ID."""
        key = sg_id.strip().upper()
        conns = list(self._connections.get(key, []))
        if not conns:
            logger.debug("no ws listeners for sg_id", extra={"sg_id": key})
            return
        message = json.dumps(payload)
        dead: list[WebSocket] = []
        for ws in conns:
            try:
                await ws.send_text(message)
            except Exception as exc:
                logger.warning(
                    "ws send failed — dropping connection",
                    extra={"sg_id": key, "error": str(exc)},
                )
                dead.append(ws)
        for ws in dead:
            self.disconnect(key, ws)
        logger.info(
            "ws notification dispatched",
            extra={"sg_id": key, "delivered": len(conns) - len(dead), "dropped": len(dead)},
        )

    def notify_sync(self, sg_id: str, payload: dict) -> None:
        """Thread-safe wrapper so sync FastAPI handlers can fire notifications.

        FastAPI runs sync handlers in a thread-pool executor while the event
        loop keeps running in the main thread.  We schedule the coroutine on
        that running loop from the worker thread.
        """
        if not sg_id:
            return
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.run_coroutine_threadsafe(self.notify(sg_id, payload), loop)
            else:
                loop.run_until_complete(self.notify(sg_id, payload))
        except Exception as exc:
            logger.warning("notify_sync scheduling failed", extra={"error": str(exc)})

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    @property
    def active_sg_ids(self) -> list[str]:
        return list(self._connections.keys())


# Module-level singleton — imported by main.py and used everywhere.
ws_manager = ConnectionManager()
