from typing import Optional

from pydantic import BaseModel


class IncidentPayload(BaseModel):
    sys_id: str
    number: str
    short_description: Optional[str] = ""
    business_service: Optional[str] = ""
    business_service_sys_id: Optional[str] = None
    service_offering: Optional[str] = ""
    service_offering_sys_id: Optional[str] = None
    assignment_group: Optional[str] = ""
    assignment_group_sys_id: Optional[str] = None
    opened_at: Optional[str] = None
    priority: Optional[int] = None


class AssignmentDecision(BaseModel):
    assigned: bool
    agent_name: Optional[str] = None
    agent_email: Optional[str] = None
    incident: Optional[str] = None
    reason: Optional[str] = None
    matching_incidents: list = []


class RitmPayload(IncidentPayload):
    pass


class RitmAssignmentDecision(AssignmentDecision):
    pass
