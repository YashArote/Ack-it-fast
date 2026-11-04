from __future__ import annotations

import logging
import math
import re
import threading
from collections import Counter
from datetime import datetime, time as dtime
from typing import Dict, Iterable, List, Optional, Set, Tuple

import pandas as pd
from zoneinfo import ZoneInfo

from app.config import ROSTER_PATH, WORKSPACE_TIMEZONE

logger = logging.getLogger("ackitfast.assignment")


class AssignmentService:
    ENFORCE_SHIFT_ROSTER_ELIGIBILITY = False
    BOILERPLATE_REGEX = re.compile(
        r"(the symptom is:|o sintoma é:|describe the issue:|descreva o problema:|"
        r"procedure to reproduce the issue:|procedimento para reproduzir o problema:|"
        r"justification:|justificativa:|"
        r"hello team|dear team|good morning|good afternoon|hello everyone|"
        r"thanks\s*&?\s*regards|best regards|kind regards|thank you in advance|"
        r"please check and|could you please|see attached|attached screenshot|"
        r"\[cid:[^\]]+\]|from:.*|sent:.*|to:.*|cc:.*|subject:.*)",
        re.IGNORECASE,
    )
    TECH_ENTITY_REGEX = re.compile(
        r"\b(?:"
        r"/n/[a-z0-9_/]+|"
        r"[p]_\d{2}_[a-z0-9_-]+|"
        r"z[a-z0-9_]{3,}|"
        r"(?:va|vl|vf|me|mm|mb|md|mi|sm|se|su|st|fb|fk|fd|fs|f-|ko|ks|kp|ke|lt|vi|vd|vk|vv|we)\d{2}[a-zn]?|"
        r"e1[a-z0-9]{4,}|"
        r"(?:incsd|ritmsd|enhc|dmnd|qsm-)\d+|"
        r"(?:p01|p16|tg4|ro1|ro2|q11|r10|a11|pi1|pv1|de6|te6|ts6|pwy|ewm|ibp|mdg|s4h|medius|ksef|factivity|pagero|notilus|zentrada|tagxter|orion)"
        r")\b",
        re.IGNORECASE,
    )
    STOPWORDS: Set[str] = {
        "the", "is", "in", "to", "for", "of", "and", "on", "with", "not", "are", "be",
        "this", "that", "have", "has", "was", "from", "by", "as", "at", "or", "an", "it",
        "we", "can", "you", "please", "hello", "hi", "dear", "team", "thanks", "thank",
        "regards", "br", "kr", "issue", "problem", "error", "working", "unable", "need",
        "help", "check", "support", "user", "system", "sap", "attached", "attachment",
        "see", "below", "following", "when", "try", "trying", "getting", "de", "la", "le",
        "en", "para", "por", "com", "não", "no", "na", "und", "nicht", "mit", "die", "der",
    }

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

    def clean_and_extract(
        self,
        short_desc: Optional[str],
        desc: Optional[str],
    ) -> Tuple[List[str], Set[str], Set[str]]:
        s_desc = str(short_desc or "").strip()
        f_desc = str(desc or "").strip()

        if "from:" in f_desc.lower() and len(f_desc) > 1200:
            f_desc = f_desc[:1200]

        combined = f"{s_desc} {s_desc} {f_desc}"
        tech_entities = {match.group(0).lower() for match in self.TECH_ENTITY_REGEX.finditer(combined)}
        cleaned = self.BOILERPLATE_REGEX.sub(" ", combined.lower())
        cleaned = re.sub(r"\b\d{6,}\b", " _docnum_ ", cleaned)
        cleaned = re.sub(r"[^a-z0-9_-]+", " ", cleaned)
        tokens = [
            token for token in cleaned.split()
            if len(token) > 1 and token not in self.STOPWORDS
        ]
        bigrams = {
            f"{tokens[index]}_{tokens[index + 1]}"
            for index in range(len(tokens) - 1)
            if not (tokens[index] == "_docnum_" and tokens[index + 1] == "_docnum_")
        }
        return tokens, tech_entities, bigrams

    def _build_idf(
        self,
        processed_docs: List[Tuple[List[str], Set[str], Set[str]]],
    ) -> Dict[str, float]:
        num_docs = len(processed_docs)
        df_counts = Counter()
        for tokens, _, _ in processed_docs:
            for token in set(tokens):
                df_counts[token] += 1
        return {
            token: math.log((1.0 + num_docs) / (1.0 + count)) + 1.0
            for token, count in df_counts.items()
        }

    def _weighted_cosine(self, tokens_a: List[str], tokens_b: List[str], idf: Dict[str, float]) -> float:
        if not tokens_a or not tokens_b:
            return 0.0
        tf_a = Counter(tokens_a)
        tf_b = Counter(tokens_b)
        vec_a = {token: (1.0 + math.log(count)) * idf.get(token, 4.0) for token, count in tf_a.items()}
        vec_b = {token: (1.0 + math.log(count)) * idf.get(token, 4.0) for token, count in tf_b.items()}
        common = set(vec_a) & set(vec_b)
        if not common:
            return 0.0
        dot_product = sum(vec_a[token] * vec_b[token] for token in common)
        norm_a = math.sqrt(sum(value * value for value in vec_a.values()))
        norm_b = math.sqrt(sum(value * value for value in vec_b.values()))
        return 0.0 if norm_a == 0 or norm_b == 0 else dot_product / (norm_a * norm_b)

    @staticmethod
    def _jaccard_or_containment(set_a: Set[str], set_b: Set[str]) -> float:
        if not set_a or not set_b:
            return 0.0
        intersection = len(set_a & set_b)
        if intersection == 0:
            return 0.0
        union = len(set_a | set_b)
        min_len = min(len(set_a), len(set_b))
        return 0.5 * (intersection / union) + 0.5 * (intersection / min_len)

    def find_matching_incidents(
        self,
        description: str,
        incidents: Iterable[dict],
        short_description: Optional[str] = None,
        threshold: float = 0.45,
    ) -> List[dict]:
        incident_list = list(incidents)
        if not incident_list:
            return []

        q_tokens, q_entities, q_bigrams = self.clean_and_extract(short_description, description)
        if not q_tokens and not q_entities:
            return []

        processed_candidates = [
            self.clean_and_extract(incident.get("short_description"), incident.get("description"))
            for incident in incident_list
        ]
        idf = self._build_idf(processed_candidates + [(q_tokens, q_entities, q_bigrams)])
        hits = []
        q_norm_exact = " ".join(q_tokens)

        for index, incident in enumerate(incident_list):
            c_tokens, c_entities, c_bigrams = processed_candidates[index]
            if not c_tokens and not c_entities:
                continue

            if q_norm_exact and q_norm_exact == " ".join(c_tokens):
                score = 1.0
            else:
                tfidf_sim = self._weighted_cosine(q_tokens, c_tokens, idf)
                entity_sim = self._jaccard_or_containment(q_entities, c_entities)
                bigram_sim = self._jaccard_or_containment(q_bigrams, c_bigrams)
                if q_entities and c_entities:
                    score = (0.50 * tfidf_sim) + (0.35 * entity_sim) + (0.15 * bigram_sim)
                else:
                    score = (0.75 * tfidf_sim) + (0.25 * bigram_sim)

            if score >= threshold:
                incident_copy = dict(incident)
                incident_copy["_score"] = round(score, 4)
                incident_copy["_matched_entities"] = list(q_entities & c_entities)
                hits.append(incident_copy)
        hits.sort(key=lambda item: item.get("_score", 0.0), reverse=True)
        logger.info(
            "completed hybrid incident matching",
            extra={
                "query_entities": list(q_entities),
                "candidate_count": len(hits),
                "threshold": threshold,
            },
        )
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

    def find_shift_roster_candidates(
        self,
        employees: Iterable[dict],
        business_service: str,
        now_dt: datetime,
    ) -> list:
        current_local = self.normalize_datetime(now_dt)
        current_time = current_local.time()
        shift_windows = {
            "M": (dtime(9, 30), dtime(18, 30)),
            "A": (dtime(12, 30), dtime(21, 30)),
        }
        normalized_business_service = str(business_service or "").strip().casefold()
        if not normalized_business_service:
            logger.warning("business service is required to filter shift roster candidates")
            return []

        candidates = []

        for employee in employees:
            sgid = str(employee.get("sgid") or "").strip()
            if not re.fullmatch(r"[A-Za-z0-9]+", sgid):
                continue
            services = employee.get("businessServices")
            if not isinstance(services, list) or not any(
                isinstance(service, str) and service.strip().casefold() == normalized_business_service
                for service in services
            ):
                continue

            shift = str(employee.get("shift") or "").strip().upper()
            shift_window = shift_windows.get(shift)
            is_present = str(employee.get("availabilityStatus") or "").strip().upper() == "PRESENT"
            is_within_shift = shift_window is not None and self.is_within_shift(current_time, *shift_window)
            if self.ENFORCE_SHIFT_ROSTER_ELIGIBILITY and not (is_present and is_within_shift):
                continue

            candidates.append(
                {
                    "Employee_Name": sgid,
                    "SG_ID": sgid,
                    "projectCode": employee.get("projectCode"),
                    "businessServices": services,
                    "shift": shift,
                    "shiftLabel": employee.get("shiftLabel"),
                    "availabilityStatus": employee.get("availabilityStatus"),
                }
            )

        logger.info(
            "filtered shift roster API candidates",
            extra={
                "business_service": business_service,
                "local_time": current_time.isoformat(),
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
