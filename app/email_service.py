from __future__ import annotations

from typing import Iterable, Optional


def send_incident_match_email(recipient: Optional[str], incident_number: str, matches: Iterable[dict]) -> dict:
    """Placeholder for sending matching incident emails.

    Replace with SendGrid, SES, SMTP, or another provider when connected.
    """
    match_list = list(matches)
    payload = {
        "provider": "placeholder",
        "status": "queued",
        "to": recipient,
        "subject": f"Matching incidents for {incident_number}",
        "summary": [
            {
                "number": item.get("number"),
                "short_description": item.get("short_description"),
                "score": round(float(item.get("_score", 0)), 3),
            }
            for item in match_list
        ],
    }
    return payload
