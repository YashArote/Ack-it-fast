from __future__ import annotations

import logging
import re
import threading
from datetime import datetime, time as dtime
from typing import Iterable, List, Optional

import pandas as pd
from zoneinfo import ZoneInfo

from app.config import ROSTER_PATH, WORKSPACE_TIMEZONE

logger = logging.getLogger("ackitfast.assignment")


class AssignmentService:
    def __init__(self, timezone_name: str = WORKSPACE_TIMEZONE):
        self.timezone_name = timezone_name
        self.timezone = ZoneInfo(self.timezone_name)
        self._state_lock = threading.Lock()
        self._rotation_state: dict = {}
        self._sysid_cache: dict = {}

    def load_roster(self) -> pd.DataFrame:
        df = pd.read_excel(ROSTER_PATH, sheet_name="Users")
        df.columns = [str(c).strip() for c in df.columns]
        return df

    def parse_time(self, value) -> Optional[dtime]:
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

    def is_within_shift(self, now_t: dtime, start_t: Optional[dtime], end_t: Optional[dtime]) -> bool:
        if start_t is None or end_t is None:
            return False
        if start_t <= end_t:
            return start_t <= now_t <= end_t
        return now_t >= start_t or now_t <= end_t

    def shift_hours(self, start_t: dtime, end_t: dtime) -> float:
        start_min = start_t.hour * 60 + start_t.minute
        end_min = end_t.hour * 60 + end_t.minute
        diff = end_min - start_min
        if diff <= 0:
            diff += 24 * 60
        return diff / 60

    def normalize_text(self, value: Optional[str]) -> str:
        if value is None:
            return ""
        value = str(value)
        value = value.lower().strip()
        value = re.sub(r"[^a-z0-9]+", " ", value)
        return " ".join(value.split())

    def token_set_ratio(self, a: str, b: str) -> float:
        if not a or not b:
            return 0.0
        a_tokens = set(self.normalize_text(a).split())
        b_tokens = set(self.normalize_text(b).split())
        if not a_tokens or not b_tokens:
            return 0.0
        intersection = len(a_tokens & b_tokens)
        union = len(a_tokens | b_tokens)
        return 0.0 if union == 0 else intersection / union

    def similarity_score(self, left: str, right: str) -> float:
        left_norm = self.normalize_text(left)
        right_norm = self.normalize_text(right)
        if not left_norm or not right_norm:
            return 0.0
        if left_norm == right_norm:
            return 1.0
        if left_norm in right_norm or right_norm in left_norm:
            return 0.9
        token_ratio = self.token_set_ratio(left_norm, right_norm)
        char_overlap = 1.0 - (len(set(left_norm) ^ set(right_norm)) / max(len(set(left_norm) | set(right_norm)), 1))
        return max(token_ratio, char_overlap, 0.0)

    def find_matching_incidents(self, description: str, incidents: Iterable[dict], threshold: float = 0.6) -> list:
        query = self.normalize_text(description)
        hits = []
        for incident in incidents:
            candidate = incident.get("short_description") or incident.get("description") or ""
            score = self.similarity_score(query, candidate)
            if score >= threshold:
                incident_copy = dict(incident)
                incident_copy["_score"] = score
                hits.append(incident_copy)
        hits.sort(key=lambda item: item.get("_score", 0.0), reverse=True)
        logger.info("completed fuzzy incident matching", extra={"query": query, "candidate_count": len(hits), "threshold": threshold})
        return hits

    def team_matches(self, row: dict, assignment_group: str) -> bool:
        if not assignment_group:
            return True
        groups_raw = str(row.get("Team_Type", "") or "")
        group_list = [g.strip() for g in groups_raw.split("/") if g.strip()]
        normalized_group = self.normalize_text(assignment_group)
        if not group_list:
            return False
        for g in group_list:
            normalized = self.normalize_text(g)
            if normalized == normalized_group:
                return True
        return False

    def business_service_matches(self, row: dict, business_service: str) -> bool:
        """Check whether the row's BU/service column covers the given business service name.

        The column uses the same slash-delimited format as Team_Type.
        Returns True when business_service is empty/None so that callers which
        don't supply a BS still get all team-matching candidates.
        """
        if not business_service:
            return True
        bs_raw = str(row.get("BU/service", "") or "")
        bs_list = [b.strip() for b in bs_raw.split("/") if b.strip()]
        normalized_bs = self.normalize_text(business_service)
        if not bs_list:
            return False
        for b in bs_list:
            if self.normalize_text(b) == normalized_bs:
                return True
        return False

    def normalize_datetime(self, value: Optional[datetime]) -> datetime:
        if value is None:
            return datetime.now(self.timezone)
        if value.tzinfo is None:
            return value.replace(tzinfo=self.timezone)
        return value.astimezone(self.timezone)

    def find_candidates(self, df: pd.DataFrame, assignment_group: str, now_dt: datetime, business_service: str = "") -> list:
        current_local = self.normalize_datetime(now_dt)
        now_t = current_local.time()
        candidates = []
        for _, row in df.iterrows():
            if str(row.get("Availability", "")).strip().lower() != "available":
                continue
            if not self.team_matches(row, assignment_group):
                continue
            if not self.business_service_matches(row, business_service):
                continue
            start_t = self.parse_time(row.get("Shift_Start_Time"))
            end_t = self.parse_time(row.get("Shift_End_Time"))
            if start_t and end_t and self.shift_hours(start_t, end_t) > 16:
                logger.warning(
                    "shift duration looks invalid",
                    extra={
                        "employee_name": row.get("Employee_Name"),
                        "start_time": str(start_t),
                        "end_time": str(end_t),
                        "duration_hours": round(self.shift_hours(start_t, end_t), 1),
                    },
                )
            if not self.is_within_shift(now_t, start_t, end_t):
                continue
            candidates.append(row)
        logger.info(
            "filtered roster candidates",
            extra={
                "assignment_group": assignment_group,
                "business_service": business_service,
                "local_time": now_t.isoformat(),
                "candidate_count": len(candidates),
            },
        )
        return candidates

    def candidate_load_score(self, row: dict, incident_priority: Optional[int] = None) -> tuple:
        active_count = int(row.get("current_incidents", 0) or 0)
        high_priority = int(row.get("high_priority_incidents", 0) or 0)
        priority_penalty = 0 if incident_priority in (None, 1) else 2
        return (active_count + high_priority + priority_penalty, high_priority)

    def rank_candidates(self, candidates: List[dict], incident_priority: Optional[int] = None) -> List[dict]:
        ranked = []
        for row in candidates:
            row_copy = dict(row)
            active_load, high_priority = self.candidate_load_score(row_copy, incident_priority)
            row_copy["_active_load"] = active_load
            row_copy["_high_priority_count"] = high_priority
            row_copy["_priority_penalty"] = 0 if incident_priority in (None, 1) else 2
            ranked.append(row_copy)
        ranked.sort(key=lambda row: (row.get("_active_load", 0), row.get("_high_priority_count", 0), row.get("Employee_Name", "")))
        return ranked

    def pick_agent(self, candidates: list, service_key: str, incident_priority: Optional[int] = None):
        ranked = self.rank_candidates(candidates, incident_priority)
        if not ranked:
            return None

        # Rotation is only a tie-breaker. It must never select a higher-load
        # candidate over the lowest-load candidate.
        best_score = (
            ranked[0].get("_active_load", 0),
            ranked[0].get("_high_priority_count", 0),
        )
        tied = [
            candidate
            for candidate in ranked
            if (
                candidate.get("_active_load", 0),
                candidate.get("_high_priority_count", 0),
            ) == best_score
        ]
        with self._state_lock:
            idx = self._rotation_state.get(service_key, -1)
            idx = (idx + 1) % len(tied)
            self._rotation_state[service_key] = idx
        return tied[idx]

    def email_for_agent(self, row: dict) -> Optional[str]:
        if isinstance(row, pd.Series):
            value = row.get("Email_ID") or row.get("email")
        else:
            value = row.get("Email_ID") or row.get("email")
        return str(value).strip() if value is not None and str(value).strip() else None

    def get_sys_id_for_email(self, email: str, service_now_client=None) -> Optional[str]:
        if not email:
            return None
        if email in self._sysid_cache:
            return self._sysid_cache[email]
        if service_now_client is None:
            return None
        result = service_now_client.get_user_by_email(email)
        if not result:
            return None
        sys_id = result.get("sys_id")
        self._sysid_cache[email] = sys_id
        return sys_id

    def build_match_payload(self, incident: dict, matching_incidents: list) -> dict:
        return {
            "incident_number": incident.get("number"),
            "incident_description": incident.get("short_description"),
            "matching_incidents": matching_incidents,
        }

    def build_similar_incident_work_notes(self, incident_number: str, matching_incidents: list) -> str:
        if not matching_incidents:
            return f"Assignment bot found no similar open incidents for {incident_number}."

        lines = [f"Assignment bot found similar open incidents for {incident_number}:"]
        for match in matching_incidents[:5]:
            number = match.get("number", "unknown")
            description = (match.get("short_description") or "No short description").strip()
            score = round(float(match.get("_score", 0)), 3)
            lines.append(f"- {number} | similarity={score} | {description}")
        return "\n".join(lines)

    def build_no_agent_work_notes(self, incident_number: str, assignment_group: str, priority: Optional[int]) -> str:
        return (
            f"[AssignmentBot] Assignment bot could not find an eligible agent for {incident_number}. "
            f"Assignment group: {assignment_group or 'not provided'}; "
            f"priority: {priority if priority is not None else 'not provided'}."
        )
