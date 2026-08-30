from __future__ import annotations

import logging
import re
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional

import requests

from app.config import SN_INSTANCE, SN_USER, SN_PASSWORD

logger = logging.getLogger("ackitfast.service_now")


class TicketingClient(ABC):
    @abstractmethod
    def get_user_by_email(self, email: str) -> Optional[Dict[str, Any]]:
        raise NotImplementedError

    @abstractmethod
    def list_open_incidents(self, exclude_sys_id: str = None) -> list:
        raise NotImplementedError

    @abstractmethod
    def get_assignment_load_for_user(self, email: str) -> dict:
        raise NotImplementedError

    @abstractmethod
    def assign_incident(self, incident_sys_id: str, agent_sys_id: str) -> dict:
        raise NotImplementedError


class ServiceNowClient(TicketingClient):
    def __init__(self, instance: str = SN_INSTANCE, username: str = SN_USER, password: str = SN_PASSWORD):
        self.instance = instance.rstrip("/")
        self.username = username
        self.password = password

    def normalize_search_terms(self, value: Optional[str]) -> list[str]:
        if not value:
            return []
        cleaned = re.sub(r"[^a-z0-9]+", " ", str(value).lower()).strip()
        terms = [term for term in cleaned.split() if len(term) >= 3]
        deduped = []
        for term in terms:
            if term not in deduped:
                deduped.append(term)
        return deduped

    def build_similarity_query(self, description: Optional[str]) -> str:
        terms = self.normalize_search_terms(description)
        if not terms:
            return "active=true^stateNOT IN6,7,8"

        conditions = []
        for term in terms[:8]:
            conditions.append(f"short_descriptionCONTAINS{term}")
        return "active=true^stateNOT IN6,7,8^" + "^OR".join(conditions)

    def get_user_by_email(self, email: str) -> Optional[Dict[str, Any]]:
        if not email:
            return None
        url = f"{self.instance}/api/now/table/sys_user"
        resp = requests.get(
            url,
            params={"sysparm_query": f"email={email}", "sysparm_fields": "sys_id,email", "sysparm_limit": 1},
            auth=(self.username, self.password),
            headers={"Accept": "application/json"},
            timeout=10,
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        result = resp.json().get("result", [])
        if not result:
            return None
        return result[0]

    def list_open_incidents(self, exclude_sys_id: str = None, similar_to: str = None) -> list:
        url = f"{self.instance}/api/now/table/incident"
        query = self.build_similarity_query(similar_to) if similar_to else "active=true^stateNOT IN6,7,8"
        if exclude_sys_id:
            query = f"{query}^sys_id!= {exclude_sys_id}"
        logger.info("Fetching open incidents from ServiceNow", extra={"query": query, "exclude_sys_id": exclude_sys_id, "similar_to": similar_to})
        resp = requests.get(
            url,
            params={
                "sysparm_query": query,
                "sysparm_fields": "sys_id,number,short_description,priority,state,assigned_to",
                "sysparm_limit": 50,
            },
            auth=(self.username, self.password),
            headers={"Accept": "application/json"},
            timeout=10,
        )
        if resp.status_code == 404:
            logger.warning("ServiceNow open incidents query returned 404", extra={"query": query})
            return []
        resp.raise_for_status()
        incidents = resp.json().get("result", [])
        logger.info("ServiceNow incident query completed", extra={"count": len(incidents)})
        return incidents

    def get_assignment_load_for_user(self, email: str) -> dict:
        if not email:
            return {"current_incidents": 0, "high_priority_incidents": 0}
        user = self.get_user_by_email(email)
        if not user:
            return {"current_incidents": 0, "high_priority_incidents": 0}
        sys_id = user.get("sys_id")
        if not sys_id:
            return {"current_incidents": 0, "high_priority_incidents": 0}

        url = f"{self.instance}/api/now/table/incident"
        resp = requests.get(
            url,
            params={
                "sysparm_query": f"active=true^stateNOT IN6,7,8^assigned_to={sys_id}",
                "sysparm_fields": "sys_id,priority",
                "sysparm_limit": 250,
            },
            auth=(self.username, self.password),
            headers={"Accept": "application/json"},
            timeout=10,
        )
        if resp.status_code == 404:
            return {"current_incidents": 0, "high_priority_incidents": 0}
        resp.raise_for_status()
        incidents = resp.json().get("result", [])
        current_incidents = len(incidents)
        high_priority_incidents = sum(1 for item in incidents if str(item.get("priority", "")).strip() in {"1", "2"})
        return {"current_incidents": current_incidents, "high_priority_incidents": high_priority_incidents}

    def assign_incident(self, incident_sys_id: str, agent_sys_id: str) -> dict:
        url = f"{self.instance}/api/now/table/incident/{incident_sys_id}"
        resp = requests.patch(
            url,
            json={"assigned_to": agent_sys_id},
            auth=(self.username, self.password),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()


def get_ticketing_client(provider_name: str = "servicenow") -> TicketingClient:
    provider_name = (provider_name or "servicenow").lower()
    if provider_name == "jira":
        raise NotImplementedError("Jira adapter is not implemented yet; add a JiraTicketingClient to this factory.")
    if provider_name not in {"servicenow", "service_now"}:
        raise ValueError(f"Unsupported ticketing provider: {provider_name}")
    return ServiceNowClient(SN_INSTANCE, SN_USER, SN_PASSWORD)
