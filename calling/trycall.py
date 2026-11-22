from __future__ import annotations

from typing import Optional

import requests

from app.call_config import (
    CALLBACK_URI,
    GRAPH_CLIENT_ID,
    GRAPH_CLIENT_SECRET,
    GRAPH_TENANT_ID,
)
from calling.getuserid import get_user_object_id


GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"
REQUEST_TIMEOUT = 20


class GraphCallError(RuntimeError):
    pass


class GraphCallClient:
    def __init__(
        self,
        tenant_id: str = GRAPH_TENANT_ID,
        client_id: str = GRAPH_CLIENT_ID,
        client_secret: str = GRAPH_CLIENT_SECRET,
        callback_uri: str = CALLBACK_URI,
    ):
        self.tenant_id = tenant_id.strip()
        self.client_id = client_id.strip()
        self.client_secret = client_secret
        self.callback_uri = callback_uri.strip()

    def _get_token(self) -> str:
        missing = [
            name
            for name, value in (
                ("GRAPH_TENANT_ID", self.tenant_id),
                ("GRAPH_CLIENT_ID", self.client_id),
                ("GRAPH_CLIENT_SECRET", self.client_secret),
            )
            if not value
        ]
        if missing:
            raise GraphCallError(
                "Microsoft Graph calling is not configured: " + ", ".join(missing)
            )

        response = requests.post(
            f"https://login.microsoftonline.com/{self.tenant_id}/oauth2/v2.0/token",
            data={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "scope": "https://graph.microsoft.com/.default",
                "grant_type": "client_credentials",
            },
            timeout=REQUEST_TIMEOUT,
        )
        if response.status_code != 200:
            raise GraphCallError(
                f"Microsoft Graph token request failed with HTTP {response.status_code}"
            )
        token = response.json().get("access_token")
        if not token:
            raise GraphCallError("Microsoft Graph token response did not contain a token")
        return token

    def create_call(self, email: str) -> str:
        if not self.callback_uri:
            raise GraphCallError(
                "Microsoft Graph calling is not configured: CALLBACK_URI"
            )
        if not self.callback_uri.startswith("https://"):
            raise GraphCallError("CALLBACK_URI must use HTTPS")

        target_user_id = get_user_object_id(email)
        token = self._get_token()
        response = requests.post(
            f"{GRAPH_BASE_URL}/communications/calls",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json={
                "@odata.type": "#microsoft.graph.call",
                "callbackUri": self.callback_uri,
                "direction": "outgoing",
                "targets": [
                    {
                        "@odata.type": "#microsoft.graph.invitationParticipantInfo",
                        "identity": {
                            "@odata.type": "#microsoft.graph.identitySet",
                            "user": {
                                "@odata.type": "#microsoft.graph.identity",
                                "id": target_user_id,
                                "tenantId": self.tenant_id,
                            },
                        },
                    }
                ],
                "requestedModalities": ["audio"],
                "mediaConfig": {
                    "@odata.type": "#microsoft.graph.serviceHostedMediaConfig"
                },
                "tenantId": self.tenant_id,
            },
            timeout=REQUEST_TIMEOUT,
        )
        if response.status_code not in (200, 201):
            raise GraphCallError(
                f"Microsoft Graph call creation failed with HTTP {response.status_code}"
            )
        call_id: Optional[str] = response.json().get("id")
        if not call_id:
            raise GraphCallError("Microsoft Graph call response did not contain a call ID")
        return call_id

    def end_call(self, call_id: str) -> None:
        token = self._get_token()
        response = requests.delete(
            f"{GRAPH_BASE_URL}/communications/calls/{call_id}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=REQUEST_TIMEOUT,
        )
        if response.status_code not in (200, 202, 204, 404):
            raise GraphCallError(
                f"Microsoft Graph call termination failed with HTTP {response.status_code}"
            )
