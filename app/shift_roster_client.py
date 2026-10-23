from __future__ import annotations

from typing import Any

import requests

from app.config import SHIFT_ROSTER_API_KEY, SHIFT_ROSTER_API_URL


class ShiftRosterAPIError(RuntimeError):
    pass


class ShiftRosterClient:
    def __init__(self, url: str = SHIFT_ROSTER_API_URL, api_key: str = SHIFT_ROSTER_API_KEY):
        self.url = url
        self.api_key = api_key

    def get_employees(self, assignment_group: str, date: str) -> list[dict[str, Any]]:
        if not self.api_key:
            raise ShiftRosterAPIError("SHIFT_ROSTER_API_KEY is not configured")

        try:
            response = requests.post(
                self.url,
                headers={"x-api-key": self.api_key, "Accept": "application/json"},
                json={"assignmentGroup": assignment_group, "date": date},
                timeout=10,
            )
            response.raise_for_status()
            payload = response.json()
        except requests.HTTPError as error:
            status_code = error.response.status_code if error.response is not None else "unknown"
            raise ShiftRosterAPIError(
                f"Shift roster API returned HTTP {status_code}"
            ) from error
        except requests.Timeout as error:
            raise ShiftRosterAPIError("Shift roster API request timed out") from error
        except requests.ConnectionError as error:
            raise ShiftRosterAPIError(
                f"Could not connect to the shift roster API: {error}"
            ) from error
        except ValueError as error:
            raise ShiftRosterAPIError("Shift roster API returned invalid JSON") from error
        except requests.RequestException as error:
            raise ShiftRosterAPIError(
                f"Shift roster API request failed ({type(error).__name__})"
            ) from error

        if not isinstance(payload, dict):
            raise ShiftRosterAPIError("Shift roster API returned an invalid response")
        if payload.get("assignmentGroup") != assignment_group:
            raise ShiftRosterAPIError("Shift roster API returned a different assignment group")
        if payload.get("date") != date:
            raise ShiftRosterAPIError("Shift roster API returned a different date")
        employees = payload.get("employees")
        if not isinstance(employees, list) or any(not isinstance(employee, dict) for employee in employees):
            raise ShiftRosterAPIError("Shift roster API returned an invalid employee list")
        return employees
