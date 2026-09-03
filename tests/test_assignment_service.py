from datetime import datetime

from zoneinfo import ZoneInfo

import pandas as pd

from app.assignment_service import AssignmentService
from app.service_now_client import ServiceNowClient


class DummyRosterRow(dict):
    pass


def test_assignment_time_uses_indian_business_timezone():
    service = AssignmentService(timezone_name="Asia/Kolkata")
    roster = pd.DataFrame([
        {
            "Employee_Name": "Alice",
            "Email_ID": "alice@example.com",
            "Availability": "Available",
            "Team_Type": "Ops",
            "Shift_Start_Time": "09:00:00",
            "Shift_End_Time": "17:00:00",
        }
    ])

    matches = service.find_candidates(
        roster,
        "Ops",
        datetime(2026, 8, 30, 5, 30, tzinfo=ZoneInfo("UTC")),
    )

    assert len(matches) == 1


def test_fuzzy_incident_match_is_robust_for_typo_variants():
    service = AssignmentService()
    incidents = [
        {"sys_id": "1", "number": "INC0001", "short_description": "Database server down in prod"},
        {"sys_id": "2", "number": "INC0002", "short_description": "VPN login failures after patch"},
    ]

    matches = service.find_matching_incidents(
        "DB server is down in production",
        incidents,
        threshold=0.6,
    )

    assert matches and matches[0]["sys_id"] == "1"


def test_servicenow_similarity_query_uses_contains_filters():
    client = ServiceNowClient(instance="https://example.service-now.com", username="u", password="p")
    query = client.build_similarity_query("database server down in production")

    assert "short_descriptionCONTAINSdatabase" in query
    assert "short_descriptionCONTAINSserver" in query
    assert "short_descriptionCONTAINSproduction" in query


def test_p1_payload_fields_are_supported():
    from app.models import IncidentPayload

    payload = IncidentPayload(
        sys_id="incident-id",
        number="INC0001",
        assignment_group="Operations",
        assignment_group_sys_id="group-id",
        priority=1,
    )

    assert payload.priority == 1
    assert payload.assignment_group_sys_id == "group-id"


def test_rank_candidates_prefers_lower_active_load_and_priority_penalty():
    service = AssignmentService()
    candidates = [
        DummyRosterRow(
            Employee_Name="Alice",
            Email_ID="alice@example.com",
            Availability="Available",
            Team_Type="Ops",
            Shift_Start_Time="09:00:00",
            Shift_End_Time="17:00:00",
            current_incidents=4,
            high_priority_incidents=1,
        ),
        DummyRosterRow(
            Employee_Name="Bob",
            Email_ID="bob@example.com",
            Availability="Available",
            Team_Type="Ops",
            Shift_Start_Time="09:00:00",
            Shift_End_Time="17:00:00",
            current_incidents=1,
            high_priority_incidents=0,
        ),
    ]

    ranked = service.rank_candidates(candidates, incident_priority=1)

    assert ranked[0]["Employee_Name"] == "Bob"


def test_rotation_only_selects_candidates_tied_for_best_load():
    service = AssignmentService()
    candidates = [
        DummyRosterRow(Employee_Name="Alice", current_incidents=0, high_priority_incidents=0),
        DummyRosterRow(Employee_Name="Bob", current_incidents=0, high_priority_incidents=0),
        DummyRosterRow(Employee_Name="Charlie", current_incidents=3, high_priority_incidents=0),
    ]

    selected = {
        service.pick_agent(candidates, "Ops")["Employee_Name"]
        for _ in range(8)
    }

    assert selected == {"Alice", "Bob"}
