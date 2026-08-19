"""
Incident Auto-Assignment Bot
-----------------------------
Receives a POST from ServiceNow whenever a new incident is created,
looks up who is on-shift and available for the incident's business
service in the roster spreadsheet, and writes the assignment back to
ServiceNow via the Table API.

Run locally:
    uvicorn main:app --reload --port 8000

Env vars (see .env.example):
    APP_API_KEY   shared secret ServiceNow must send back as X-API-Key
    ROSTER_PATH   path to the roster .xlsx (defaults to roster.xlsx)
    SN_INSTANCE   e.g. https://yourinstance.service-now.com
    SN_USER       ServiceNow integration account username
    SN_PASSWORD   ServiceNow integration account password
"""
import os
import threading
from datetime import datetime, time as dtime
from typing import Optional

import pandas as pd
import requests
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

APP_API_KEY = os.environ.get("APP_API_KEY", "ackitfast")
ROSTER_PATH = os.environ.get("ROSTER_PATH", "roster.xlsx")
SN_INSTANCE = os.environ.get("SN_INSTANCE", "https://dev405393.service-now.com/")
SN_USER = os.environ.get("SN_USER", "admin")
SN_PASSWORD = os.environ.get("SN_PASSWORD", "6^%oNrNFxW1k")

app = FastAPI(title="Incident Auto-Assignment Bot")

# In-memory state. Fine for a single free-tier instance; if you ever run
# more than one replica, move this to Redis (or any shared store) so the
# round-robin pointer and sys_id cache are shared across instances.
_state_lock = threading.Lock()
_rotation_state: dict = {}   # {business_service: last_picked_index}
_sysid_cache: dict = {}      # {email: servicenow sys_id}


class IncidentPayload(BaseModel):
    sys_id: str
    number: str
    short_description: Optional[str] = ""
    business_service: Optional[str] = ""
    assignment_group: Optional[str] = ""
    opened_at: Optional[str] = None  # "YYYY-MM-DD HH:MM:SS", ServiceNow local time


# ---------------------------------------------------------------------
# Roster loading / matching
# ---------------------------------------------------------------------

def load_roster() -> pd.DataFrame:
    """Re-read the roster on every call so edits to the spreadsheet take
    effect immediately without restarting the server."""
    df = pd.read_excel(ROSTER_PATH, sheet_name="Users")
    df.columns = [str(c).strip() for c in df.columns]
    return df


def parse_time(value) -> Optional[dtime]:
    if isinstance(value, dtime):
        return value
    if isinstance(value, datetime):
        return value.time()
    if value is None:
        return None
    s = str(value).strip()
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(s, fmt).time()
        except ValueError:
            continue
    return None


def is_within_shift(now_t: dtime, start_t: Optional[dtime], end_t: Optional[dtime]) -> bool:
    if start_t is None or end_t is None:
        return False
    if start_t <= end_t:
        return start_t <= now_t <= end_t
    # overnight shift, e.g. Night shift 21:00 -> 06:00
    return now_t >= start_t or now_t <= end_t


def shift_hours(start_t: dtime, end_t: dtime) -> float:
    start_min = start_t.hour * 60 + start_t.minute
    end_min = end_t.hour * 60 + end_t.minute
    diff = end_min - start_min
    if diff <= 0:
        diff += 24 * 60
    return diff / 60


def find_candidates(df: pd.DataFrame, business_service: str, now_dt: datetime) -> list:
    now_t = now_dt.time()
    candidates = []
    for _, row in df.iterrows():
        if str(row.get("Availability", "")).strip().lower() != "available":
            continue

        services_raw = str(row.get("BU/service", "") or "")
        service_list = [s.strip() for s in services_raw.split("/") if s.strip()]
        if business_service:
            exact = business_service in service_list
            fuzzy = any(business_service.lower() in s.lower() for s in service_list)
            if not (exact or fuzzy):
                continue

        start_t = parse_time(row.get("Shift_Start_Time"))
        end_t = parse_time(row.get("Shift_End_Time"))

        # Data-quality guard: a shift that computes to >16h almost always
        # means Shift_End_Time was entered in 12-hour form without noting
        # it's PM (e.g. a 9am-6pm shift stored as 09:00:00 -> 06:00:00,
        # which reads as a ~21h overnight shift instead of 9h). Flag it
        # loudly instead of silently mis-assigning off it.
        if start_t and end_t and shift_hours(start_t, end_t) > 16:
            print(
                f"[roster warning] {row.get('Employee_Name')}: shift "
                f"{start_t}-{end_t} computes to {shift_hours(start_t, end_t):.1f}h. "
                f"Check Shift_End_Time is in 24h format (e.g. 18:00, not 06:00)."
            )

        if not is_within_shift(now_t, start_t, end_t):
            continue

        candidates.append(row)
    return candidates


def pick_agent(candidates: list, service_key: str):
    """Simple round-robin so repeat matches for the same service don't
    always land on the first row of the spreadsheet."""
    with _state_lock:
        idx = _rotation_state.get(service_key, -1)
        idx = (idx + 1) % len(candidates)
        _rotation_state[service_key] = idx
    return candidates[idx]


# ---------------------------------------------------------------------
# ServiceNow calls
# ---------------------------------------------------------------------

def get_sys_id_for_email(email: str) -> Optional[str]:
    if not email:
        return None
    if email in _sysid_cache:
        return _sysid_cache[email]
    url = f"{SN_INSTANCE}/api/now/table/sys_user"
    resp = requests.get(
        url,
        params={"sysparm_query": f"email={email}", "sysparm_fields": "sys_id", "sysparm_limit": 1},
        auth=(SN_USER, SN_PASSWORD),
        headers={"Accept": "application/json"},
        timeout=10,
    )
    resp.raise_for_status()
    result = resp.json().get("result", [])
    if not result:
        return None
    sys_id = result[0]["sys_id"]
    _sysid_cache[email] = sys_id
    return sys_id


def assign_in_servicenow(incident_sys_id: str, agent_sys_id: str) -> dict:
    url = f"{SN_INSTANCE}/api/now/table/incident/{incident_sys_id}"
    resp = requests.patch(
        url,
        json={"assigned_to": agent_sys_id},
        auth=(SN_USER, SN_PASSWORD),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------

@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/assign")
def assign(payload: IncidentPayload, x_api_key: str = Header(None)):
    if x_api_key != APP_API_KEY:
        raise HTTPException(status_code=401, detail="invalid api key")

    df = load_roster()

    now_dt = datetime.utcnow()  # swap for your team's local timezone if needed
    if payload.opened_at:
        try:
            now_dt = datetime.strptime(payload.opened_at, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass

    candidates = find_candidates(df, payload.business_service, now_dt)
    if not candidates:
        return {"assigned": False, "reason": "no available agent on shift for this service"}

    agent = pick_agent(candidates, payload.business_service)
    email = agent.get("Email_ID")

    if SN_INSTANCE and SN_USER and SN_PASSWORD:
        agent_sys_id = get_sys_id_for_email(email)
        if not agent_sys_id:
            return {"assigned": False, "reason": f"no ServiceNow user found for {email}"}
        assign_in_servicenow(payload.sys_id, agent_sys_id)

    return {
        "assigned": True,
        "agent_name": agent.get("Employee_Name"),
        "agent_email": email,
        "incident": payload.number,
    }
