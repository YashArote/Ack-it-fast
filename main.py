import logging
from datetime import datetime

from fastapi import FastAPI, Header, HTTPException

from app.assignment_service import AssignmentService
from app.config import APP_API_KEY, SN_INSTANCE, SN_USER, SN_PASSWORD, WORKSPACE_TIMEZONE
from app.email_service import send_incident_match_email
from app.models import AssignmentDecision, IncidentPayload
from app.roster_routes import router as roster_router
from app.service_now_client import ServiceNowClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("ackitfast.api")

app = FastAPI(title="Incident Auto-Assignment Bot")
app.include_router(roster_router)

assignment_service = AssignmentService(timezone_name=WORKSPACE_TIMEZONE)
service_now_client = ServiceNowClient(SN_INSTANCE, SN_USER, SN_PASSWORD)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/assign", response_model=AssignmentDecision)
def assign(payload: IncidentPayload, x_api_key: str = Header(None)):
    if x_api_key != APP_API_KEY:
        logger.warning("invalid API key attempt", extra={"incident_number": payload.number})
        raise HTTPException(status_code=401, detail="invalid api key")

    logger.info("assignment request received", extra={"incident_number": payload.number, "assignment_group": payload.assignment_group, "business_service": payload.business_service})
    df = assignment_service.load_roster()
    now_dt = datetime.utcnow()
    if payload.opened_at:
        try:
            parsed = datetime.strptime(payload.opened_at, "%Y-%m-%d %H:%M:%S")
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=assignment_service.timezone)
            now_dt = parsed.astimezone(assignment_service.timezone)
        except ValueError:
            pass

    assignment_group = payload.assignment_group or payload.business_service or ""
    candidates = assignment_service.find_candidates(df, assignment_group, now_dt)
    if not candidates:
        logger.warning("no candidates found for assignment", extra={"assignment_group": assignment_group, "incident_number": payload.number})
        return AssignmentDecision(
            assigned=False,
            reason="no available agent on shift for this assignment group",
            incident=payload.number,
        )

    for candidate in candidates:
        email = assignment_service.email_for_agent(candidate)
        load = service_now_client.get_assignment_load_for_user(email) if email else {"current_incidents": 0, "high_priority_incidents": 0}
        candidate["current_incidents"] = load.get("current_incidents", 0)
        candidate["high_priority_incidents"] = load.get("high_priority_incidents", 0)
        logger.info("candidate workload loaded", extra={"employee_name": candidate.get("Employee_Name"), "current_incidents": load.get("current_incidents", 0), "high_priority_incidents": load.get("high_priority_incidents", 0)})

    agent = assignment_service.pick_agent(candidates, assignment_group or "default", payload.priority)
    if agent is None:
        logger.warning("assignment ranking returned no agent", extra={"incident_number": payload.number})
        return AssignmentDecision(
            assigned=False,
            reason="no agent available after ranking",
            incident=payload.number,
        )

    email = assignment_service.email_for_agent(agent)
    logger.info("selected agent", extra={"incident_number": payload.number, "agent_name": agent.get("Employee_Name"), "agent_email": email, "priority": payload.priority})
    if SN_INSTANCE and SN_USER and SN_PASSWORD:
        agent_sys_id = assignment_service.get_sys_id_for_email(email, service_now_client)
        if not agent_sys_id:
            return AssignmentDecision(
                assigned=False,
                reason=f"no ServiceNow user found for {email}",
                incident=payload.number,
            )
        service_now_client.assign_incident(payload.sys_id, agent_sys_id)

    open_incidents = service_now_client.list_open_incidents(exclude_sys_id=payload.sys_id, similar_to=payload.short_description or payload.business_service or "")
    matching_incidents = assignment_service.find_matching_incidents(
        payload.short_description or payload.business_service or "",
        open_incidents,
        threshold=0.55,
    )
    logger.info("similar incidents matched", extra={"incident_number": payload.number, "match_count": len(matching_incidents)})

    if email:
        send_incident_match_email(email, payload.number, matching_incidents)

    return AssignmentDecision(
        assigned=True,
        agent_name=agent.get("Employee_Name"),
        agent_email=email,
        incident=payload.number,
        matching_incidents=matching_incidents[:5],
    )
