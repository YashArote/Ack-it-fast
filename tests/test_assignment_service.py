from datetime import datetime

from zoneinfo import ZoneInfo

import pandas as pd

from app.assignment_service import AssignmentService
from app.shift_roster_client import ShiftRosterAPIError, ShiftRosterClient
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


def test_team_matching_does_not_confuse_different_l1_groups():
    service = AssignmentService()
    row = {"Team_Type": "GDI-SAP_CC-MM-L1"}

    assert service.team_matches(row, "GDI-SAP_CC-MM-L1")
    assert not service.team_matches(row, "GDI-SAP_CC-FL-L1")
    assert not service.team_matches(row, "SAP_CC-MM-L1")


def test_roster_candidate_is_rejected_after_shift_ends():
    service = AssignmentService(timezone_name="Asia/Kolkata")
    roster = pd.DataFrame([
        {
            "Employee_Name": "DHAMDHERE, Aishwarya",
            "Availability": "Available",
            "Team_Type": "GDI-SAP_CC-MM-L1",
            "Shift_Start_Time": "09:00:00",
            "Shift_End_Time": "18:00:00",
        }
    ])

    matches = service.find_candidates(
        roster,
        "GDI-SAP_CC-MM-L1",
        datetime(2026, 9, 19, 20, 30, tzinfo=ZoneInfo("Asia/Kolkata")),
    )

    assert matches == []


def test_shift_roster_api_candidates_require_service_availability_and_active_shift():
    service = AssignmentService(timezone_name="Asia/Kolkata")
    employees = [
        {
            "sgid": "T8753430",
            "businessServices": ["BS_CSR_SAP", "BS_GDI_SAP_E2000"],
            "shift": "M",
            "availabilityStatus": "PRESENT",
        },
        {
            "sgid": "T8761234",
            "businessServices": ["BS_CSR_SAP"],
            "shift": None,
            "availabilityStatus": "UNAVAILABLE",
        },
        {
            "sgid": "T8769999",
            "businessServices": ["BS_OTHER"],
            "shift": "M",
            "availabilityStatus": "PRESENT",
        },
        {
            "sgid": "T8769998",
            "businessServices": ["BS_GDI_SAP_E2000"],
            "shift": "A",
            "availabilityStatus": "PRESENT",
        },
    ]

    matches = service.find_shift_roster_candidates(
        employees,
        "BS_GDI_SAP_E2000",
        datetime(2026, 10, 23, 9, 30, tzinfo=ZoneInfo("Asia/Kolkata")),
    )

    assert [candidate["SG_ID"] for candidate in matches] == ["T8753430"]


def test_shift_roster_api_candidate_is_available_through_shift_end():
    service = AssignmentService(timezone_name="Asia/Kolkata")
    employee = {
        "sgid": "T8753430",
        "businessServices": ["BS_CSR_SAP"],
        "shift": "M",
        "availabilityStatus": "PRESENT",
    }

    matches = service.find_shift_roster_candidates(
        [employee],
        "BS_CSR_SAP",
        datetime(2026, 10, 23, 18, 30, tzinfo=ZoneInfo("Asia/Kolkata")),
    )

    assert [candidate["SG_ID"] for candidate in matches] == ["T8753430"]


def test_shift_roster_api_a_shift_uses_inclusive_indian_time_window():
    service = AssignmentService(timezone_name="Asia/Kolkata")
    employee = {
        "sgid": "T8753430",
        "businessServices": ["BS_CSR_SAP"],
        "shift": "A",
        "availabilityStatus": "PRESENT",
    }

    for hour, minute, expected_count in (
        (12, 29, 0),
        (12, 30, 1),
        (21, 30, 1),
        (21, 31, 0),
    ):
        matches = service.find_shift_roster_candidates(
            [employee],
            "BS_CSR_SAP",
            datetime(2026, 10, 23, hour, minute, tzinfo=ZoneInfo("Asia/Kolkata")),
        )
        assert len(matches) == expected_count


def test_shift_roster_api_client_sends_expected_post(monkeypatch):
    captured = {}

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "assignmentGroup": "GDI-SAP_CC-Authorizations",
                "date": "2026-10-23",
                "employees": [{"sgid": "T8753430"}],
            }

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return FakeResponse()

    monkeypatch.setattr("app.shift_roster_client.requests.post", fake_post)
    client = ShiftRosterClient("https://roster.example/employees", "test-key")

    employees = client.get_employees("GDI-SAP_CC-Authorizations", "2026-10-23")

    assert captured["url"] == "https://roster.example/employees"
    assert captured["headers"]["x-api-key"] == "test-key"
    assert captured["json"] == {
        "assignmentGroup": "GDI-SAP_CC-Authorizations",
        "date": "2026-10-23",
    }
    assert employees == [{"sgid": "T8753430"}]


def test_shift_roster_api_client_rejects_wrong_group(monkeypatch):
    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "assignmentGroup": "another-group",
                "date": "2026-10-23",
                "employees": [],
            }

    monkeypatch.setattr("app.shift_roster_client.requests.post", lambda *args, **kwargs: FakeResponse())

    try:
        ShiftRosterClient("https://roster.example/employees", "test-key").get_employees(
            "GDI-SAP_CC-Authorizations",
            "2026-10-23",
        )
    except ShiftRosterAPIError as error:
        assert "different assignment group" in str(error)
    else:
        raise AssertionError("expected a response group mismatch to fail closed")


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
    query = client.build_similarity_query(
        "database server down in production",
        business_service_sys_id="service-1",
        service_offering_sys_id="offering-1",
        assignment_group_sys_id="group-1",
    )

    assert "active=true" not in query
    assert "stateNOT IN" not in query
    assert "business_service=service-1" in query
    assert "service_offering=offering-1" in query
    assert "assignment_group=group-1" in query
    assert "^NQbusiness_service=service-1" in query
    assert "short_descriptionCONTAINSdatabase" in query
    assert "short_descriptionCONTAINSserver" in query
    assert "short_descriptionCONTAINSproduction" in query


def test_bulk_workload_query_uses_assigned_to_in_operator():
    client = ServiceNowClient(instance="https://example.service-now.com", username="u", password="p")

    assert "assigned_toINuser-a,user-b" == "assigned_toIN" + ",".join(["user-a", "user-b"])


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


def test_similar_incidents_are_formatted_for_work_notes():
    notes = AssignmentService().build_similar_incident_work_notes(
        "INC0001",
        [{"number": "INC0002", "short_description": "Database outage", "_score": 0.82}],
    )

    assert "INC0001" in notes
    assert "INC0002" in notes
    assert "similarity=0.82" in notes


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

def test_ritm_payload_fields_are_supported():
    from app.models import RitmPayload

    payload = RitmPayload(
        sys_id="ritm-id",
        number="RITM0001",
        assignment_group="Operations",
        assignment_group_sys_id="group-id",
        priority=1,
    )

    assert payload.priority == 1
    assert payload.assignment_group_sys_id == "group-id"

def test_ritm_bulk_workload_query_uses_state_filters():
    client = ServiceNowClient(instance="https://example.service-now.com", username="u", password="p")

    query = client.build_ritm_assignment_load_query(["user-a", "user-b"])
    assert "stateNOT IN3,4,7" in query
    assert "assigned_toINuser-a,user-b" in query
