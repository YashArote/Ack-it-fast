from datetime import datetime, timedelta, timezone

import pytest

from app.call_acknowledgement import CallAcknowledgementService
from calling.getuserid import get_user_object_id


class FakeGraphClient:
    def __init__(self):
        self.ended_calls = []

    def create_call(self, email):
        return "call-123"

    def end_call(self, call_id):
        self.ended_calls.append(call_id)


class FakeServiceNowClient:
    def __init__(self):
        self.incident_notes = []
        self.ritm_notes = []

    def add_work_notes(self, ticket_sys_id, work_notes):
        self.incident_notes.append((ticket_sys_id, work_notes))

    def add_ritm_work_notes(self, ticket_sys_id, work_notes):
        self.ritm_notes.append((ticket_sys_id, work_notes))


def notification(call_id, state, change_type="updated", result_message=""):
    resource_data = {"state": state}
    if result_message:
        resource_data["resultInfo"] = {"message": result_message}
    return {
        "changeType": change_type,
        "resource": f"/app/calls/{call_id}",
        "resourceData": resource_data,
    }


def make_service(tmp_path, ring_seconds=7):
    service_now = FakeServiceNowClient()
    graph = FakeGraphClient()
    service = CallAcknowledgementService(
        service_now,
        graph_client=graph,
        database_path=str(tmp_path / "call-state.sqlite3"),
        ring_seconds=ring_seconds,
        timezone_name="Asia/Kolkata",
    )
    return service, service_now, graph


def test_picked_up_call_is_ended_and_noted_with_acknowledgement_timing(tmp_path):
    service, service_now, graph = make_service(tmp_path)
    placed_at = datetime.now(timezone.utc) - timedelta(seconds=12)
    service.register_call(
        "call-123",
        "incident-sys-id",
        "INC0001234",
        "incident",
        "agent@example.com",
        placed_at,
    )

    notes, calls_to_end = service.handle_notifications(
        {"value": [notification("call-123", "established")]}
    )

    assert notes == []
    assert calls_to_end == ["call-123"]
    service.end_acknowledged_call("call-123")

    assert graph.ended_calls == ["call-123"]
    assert service_now.incident_notes == []
    notes, calls_to_end = service.handle_notifications(
        {"value": [notification("call-123", "terminating"), notification("call-123", "terminated", "deleted")]}
    )
    service.write_notification_notes(notes)
    assert calls_to_end == []
    assert len(service_now.incident_notes) == 1
    _, note = service_now.incident_notes[0]
    assert "Acknowledged: Yes" in note
    assert "call established at" in note

    notes, calls_to_end = service.handle_notifications(
        {"value": [notification("call-123", "terminated", "deleted")]}
    )
    service.write_notification_notes(notes)
    assert calls_to_end == []
    assert len(service_now.incident_notes) == 1


def test_call_ending_before_timeout_is_not_marked_as_timed_out(tmp_path):
    service, service_now, graph = make_service(tmp_path)
    service.register_call(
        "call-123",
        "ritm-sys-id",
        "RITM0001234",
        "ritm",
        "agent@example.com",
        datetime.now(timezone.utc) - timedelta(seconds=2),
    )

    notes, calls_to_end = service.handle_notifications(
        {
            "value": [
                notification("call-123", "establishing"),
                notification("call-123", "terminating"),
            ]
        }
    )
    assert notes == []
    assert calls_to_end == []
    service.timeout_call("call-123")
    assert graph.ended_calls == []

    notes, calls_to_end = service.handle_notifications(
        {"value": [notification("call-123", "terminated", "deleted")]}
    )
    service.write_notification_notes(notes)

    assert calls_to_end == []
    assert len(service_now.ritm_notes) == 1
    assert "Acknowledged: No — the call ended without an established event." in service_now.ritm_notes[0][1]


def test_timeout_ends_call_and_writes_no_acknowledgement_note(tmp_path):
    service, service_now, graph = make_service(tmp_path, ring_seconds=7)
    service.register_call(
        "call-123",
        "incident-sys-id",
        "INC0001234",
        "incident",
        "agent@example.com",
        datetime.now(timezone.utc),
    )

    service.timeout_call("call-123")

    assert graph.ended_calls == ["call-123"]
    assert service_now.incident_notes == []
    notes, calls_to_end = service.handle_notifications(
        {
            "value": [
                notification("call-123", "terminating"),
                notification("call-123", "terminated", "deleted"),
            ]
        }
    )
    service.write_notification_notes(notes)
    assert calls_to_end == []
    assert len(service_now.incident_notes) == 1
    assert "Acknowledged: No — no answer within 7 seconds." in service_now.incident_notes[0][1]


def test_notifications_arriving_before_call_response_are_replayed(tmp_path):
    service, service_now, _ = make_service(tmp_path)
    notes, calls_to_end = service.handle_notifications(
        {
            "value": [
                notification("call-123", "established"),
                notification("call-123", "terminated", "deleted"),
            ]
        }
    )
    assert notes == []
    assert calls_to_end == []

    calls_to_end = service.register_call(
        "call-123",
        "incident-sys-id",
        "INC0001234",
        "incident",
        "agent@example.com",
        datetime.now(timezone.utc) - timedelta(seconds=10),
    )

    assert calls_to_end == []
    assert len(service_now.incident_notes) == 1
    assert "Acknowledged: Yes" in service_now.incident_notes[0][1]


def test_user_id_stub_requires_email_and_returns_configured_fixed_id():
    assert get_user_object_id("agent@example.com") == "6e2d0319-5751-4832-9281-98ed20b705ba"
    with pytest.raises(ValueError, match="email is required"):
        get_user_object_id("")


def test_notification_endpoint_echoes_validation_challenge_and_rejects_bad_token(monkeypatch):
    import main
    from fastapi import BackgroundTasks, HTTPException

    monkeypatch.setattr(main, "CALL_WEBHOOK_TOKEN", "test-webhook-token")

    response = main.validate_call_notification_endpoint(
        "test-webhook-token",
        "validation-challenge",
    )
    assert response == "validation-challenge"

    response = main.receive_call_notifications(
        "test-webhook-token",
        BackgroundTasks(),
        validation_token="validation-challenge",
    )
    assert response.body == b"validation-challenge"

    with pytest.raises(HTTPException) as error:
        main.validate_call_notification_endpoint("wrong-token", "validation-challenge")
    assert error.value.status_code == 404
