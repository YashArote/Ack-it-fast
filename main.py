import json
import logging
from datetime import datetime

from fastapi import FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect, BackgroundTasks

from app.assignment_service import AssignmentService
from app.config import ATTACH_NO_AGENT_WORK_NOTE, ATTACH_NO_SIMILAR_WORK_NOTE, APP_API_KEY, SN_INSTANCE, SN_USER, SN_PASSWORD, WORKSPACE_TIMEZONE
from app.models import AssignmentDecision, IncidentPayload, RitmPayload, RitmAssignmentDecision
from app.roster_routes import router as roster_router
from app.service_now_client import ServiceNowClient
from app.websocket_manager import ws_manager

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("ackitfast.api")

app = FastAPI(title="Incident Auto-Assignment Bot")
app.include_router(roster_router)

assignment_service = AssignmentService(timezone_name=WORKSPACE_TIMEZONE)
service_now_client = ServiceNowClient(SN_INSTANCE, SN_USER, SN_PASSWORD)


@app.get("/health")
def health():
    return {"status": "ok", "ws_listeners": ws_manager.active_sg_ids}


@app.websocket("/ws/{sg_id}")
async def websocket_endpoint(websocket: WebSocket, sg_id: str):
    """A frontend client connects here with its SG_ID.

    It will receive a JSON push every time a P1 incident or RITM is
    roster-assigned to the agent with that SG_ID.

    Message types sent to the client:
      { "type": "subscribed",  "sg_id": "P5647570" }
      { "type": "pong" }
      { "type": "assignment",  "sg_id": "P5647570",
        "ticket_type": "incident"|"ritm",
        "ticket_number": "INC0012345",
        "priority": 1,
        "assignment_group": "GDI-SAP_CC-FI-L1",
        "business_service": "BS_GDI_SAP_Holding",
        "agent_name": "John Doe",
        "agent_email": "john.doe@saint-gobain.com",
        "assigned_at": "2026-09-26T14:01:53+05:30" }

    The client MUST send {"type":"ping"} every ~30 seconds.
    Render's reverse proxy kills idle WebSocket connections after 55 s.
    """
    await ws_manager.connect(sg_id, websocket)
    try:
        while True:
            # Reply to keepalive pings; ignore everything else
            text = await websocket.receive_text()
            try:
                msg = json.loads(text)
                if msg.get("type") == "ping":
                    await websocket.send_text(json.dumps({"type": "pong"}))
            except Exception:
                pass  # non-JSON messages are silently ignored
    except WebSocketDisconnect:
        ws_manager.disconnect(sg_id, websocket)


@app.post("/assign", response_model=AssignmentDecision)
def assign(payload: IncidentPayload, background_tasks: BackgroundTasks, x_api_key: str = Header(None)):
    if x_api_key != APP_API_KEY:
        logger.warning("invalid API key attempt", extra={"incident_number": payload.number})
        raise HTTPException(status_code=401, detail="invalid api key")

    logger.info("assignment request received", extra={"incident_number": payload.number, "assignment_group": payload.assignment_group, "assignment_group_sys_id": payload.assignment_group_sys_id, "business_service": payload.business_service, "priority": payload.priority})
    # Shift eligibility is based on the current local time, never on the
    # incident's historical opened_at timestamp.
    now_dt = datetime.now(assignment_service.timezone)
    logger.info("evaluating roster against current local time", extra={"local_time": now_dt.isoformat(), "timezone": assignment_service.timezone_name})

    assignment_group = payload.assignment_group or payload.business_service or ""
    is_p1 = payload.priority == 1
    if is_p1:
        df = assignment_service.load_roster()
        candidates = assignment_service.find_candidates(df, assignment_group, now_dt, payload.business_service or "")
        logger.info("using roster for P1 assignment", extra={"incident_number": payload.number})
    else:
        candidates = service_now_client.list_group_users(assignment_group, payload.assignment_group_sys_id)
        logger.info("using ServiceNow group users for non-P1 assignment", extra={"incident_number": payload.number})
    if not candidates:
        logger.warning("no candidates found for assignment", extra={"assignment_group": assignment_group, "incident_number": payload.number})
        if ATTACH_NO_AGENT_WORK_NOTE and SN_INSTANCE and SN_USER and SN_PASSWORD:
            no_agent_notes = assignment_service.build_no_agent_work_notes(
                payload.number,
                assignment_group,
                payload.priority,
            )
            service_now_client.add_work_notes(payload.sys_id, no_agent_notes)
            logger.info("no-agent work note added", extra={"incident_number": payload.number})
        else:
            logger.info("no-agent work note skipped", extra={"incident_number": payload.number, "enabled": ATTACH_NO_AGENT_WORK_NOTE})
        return AssignmentDecision(
            assigned=False,
            reason="no available agent on shift for this assignment group",
            incident=payload.number,
        )

    if not is_p1:
        user_sys_ids = [candidate.get("sys_id") for candidate in candidates]
        loads = service_now_client.get_assignment_load_for_users(user_sys_ids)
        for candidate in candidates:
            load = loads.get(candidate.get("sys_id"), {"current_incidents": 0, "high_priority_incidents": 0})
            candidate["current_incidents"] = load.get("current_incidents", 0)
            candidate["high_priority_incidents"] = load.get("high_priority_incidents", 0)
            logger.info("candidate workload loaded", extra={"employee_name": candidate.get("name") or candidate.get("Employee_Name"), "current_incidents": load.get("current_incidents", 0), "high_priority_incidents": load.get("high_priority_incidents", 0)})

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
        agent_sys_id = agent.get("sys_id") or assignment_service.get_sys_id_for_email(email, service_now_client)
        if not agent_sys_id:
            return AssignmentDecision(
                assigned=False,
                reason=f"no ServiceNow user found for {email}",
                incident=payload.number,
            )
        service_now_client.assign_incident(payload.sys_id, agent_sys_id)

    # ── WebSocket push (P1 roster assignments only) ──────────────────────
    if is_p1:
        sg_id = str(agent.get("SG_ID") or "").strip()
        if sg_id:
            msg = {
                "type": "assignment",
                "sg_id": sg_id.upper(),
                "ticket_type": "incident",
                "ticket_number": payload.number,
                "priority": payload.priority,
                "assignment_group": payload.assignment_group or "",
                "business_service": payload.business_service or "",
                "agent_name": agent.get("Employee_Name") or "",
                "agent_email": email or "",
                "assigned_at": datetime.now(assignment_service.timezone).isoformat(),
            }
            background_tasks.add_task(ws_manager.notify, sg_id, msg)

    open_incidents = service_now_client.list_open_incidents(
        exclude_sys_id=payload.sys_id,
        similar_to=payload.short_description or payload.business_service or "",
        business_service_sys_id=payload.business_service_sys_id,
        service_offering_sys_id=payload.service_offering_sys_id,
        assignment_group_sys_id=payload.assignment_group_sys_id,
    )
    matching_incidents = assignment_service.find_matching_incidents(
        payload.short_description or payload.business_service or "",
        open_incidents,
        threshold=0.55,
    )
    logger.info("similar incidents matched", extra={"incident_number": payload.number, "match_count": len(matching_incidents)})

    work_notes = assignment_service.build_similar_incident_work_notes(payload.number, matching_incidents)
    if matching_incidents or ATTACH_NO_SIMILAR_WORK_NOTE:
        service_now_client.add_work_notes(payload.sys_id, work_notes)
    else:
        logger.info("skipping no-match work note because toggle is disabled", extra={"incident_number": payload.number})

    return AssignmentDecision(
        assigned=True,
        agent_name=agent.get("Employee_Name"),
        agent_email=email,
        incident=payload.number,
        matching_incidents=matching_incidents[:5],
    )

@app.post("/assign-ritm", response_model=RitmAssignmentDecision)
def assign_ritm(payload: RitmPayload, background_tasks: BackgroundTasks, x_api_key: str = Header(None)):
    if x_api_key != APP_API_KEY:
        logger.warning("invalid API key attempt", extra={"ritm_number": payload.number})
        raise HTTPException(status_code=401, detail="invalid api key")

    logger.info("assignment request received", extra={"ritm_number": payload.number, "assignment_group": payload.assignment_group, "assignment_group_sys_id": payload.assignment_group_sys_id, "business_service": payload.business_service, "priority": payload.priority})
    
    now_dt = datetime.now(assignment_service.timezone)
    logger.info("evaluating roster against current local time", extra={"local_time": now_dt.isoformat(), "timezone": assignment_service.timezone_name})

    assignment_group = payload.assignment_group or payload.business_service or ""
    is_p1 = payload.priority == 1
    if is_p1:
        df = assignment_service.load_roster()
        candidates = assignment_service.find_candidates(df, assignment_group, now_dt, payload.business_service or "")
        logger.info("using roster for P1 assignment", extra={"ritm_number": payload.number})
    else:
        candidates = service_now_client.list_group_users(assignment_group, payload.assignment_group_sys_id)
        logger.info("using ServiceNow group users for non-P1 assignment", extra={"ritm_number": payload.number})
    
    if not candidates:
        logger.warning("no candidates found for assignment", extra={"assignment_group": assignment_group, "ritm_number": payload.number})
        if ATTACH_NO_AGENT_WORK_NOTE and SN_INSTANCE and SN_USER and SN_PASSWORD:
            no_agent_notes = assignment_service.build_no_agent_work_notes(
                payload.number,
                assignment_group,
                payload.priority,
            )
            service_now_client.add_ritm_work_notes(payload.sys_id, no_agent_notes)
            logger.info("no-agent work note added", extra={"ritm_number": payload.number})
        else:
            logger.info("no-agent work note skipped", extra={"ritm_number": payload.number, "enabled": ATTACH_NO_AGENT_WORK_NOTE})
        return RitmAssignmentDecision(
            assigned=False,
            reason="no available agent on shift for this assignment group",
            incident=payload.number,
        )

    if not is_p1:
        user_sys_ids = [candidate.get("sys_id") for candidate in candidates]
        loads = service_now_client.get_assignment_load_for_users_ritm(user_sys_ids)
        for candidate in candidates:
            load = loads.get(candidate.get("sys_id"), {"current_incidents": 0, "high_priority_incidents": 0})
            candidate["current_incidents"] = load.get("current_incidents", 0)
            candidate["high_priority_incidents"] = load.get("high_priority_incidents", 0)
            logger.info("candidate workload loaded", extra={"employee_name": candidate.get("name") or candidate.get("Employee_Name"), "current_incidents": load.get("current_incidents", 0), "high_priority_incidents": load.get("high_priority_incidents", 0)})

    agent = assignment_service.pick_agent(candidates, assignment_group or "default", payload.priority)
    if agent is None:
        logger.warning("assignment ranking returned no agent", extra={"ritm_number": payload.number})
        return RitmAssignmentDecision(
            assigned=False,
            reason="no agent available after ranking",
            incident=payload.number,
        )

    email = assignment_service.email_for_agent(agent)
    logger.info("selected agent", extra={"ritm_number": payload.number, "agent_name": agent.get("Employee_Name"), "agent_email": email, "priority": payload.priority})
    if SN_INSTANCE and SN_USER and SN_PASSWORD:
        agent_sys_id = agent.get("sys_id") or assignment_service.get_sys_id_for_email(email, service_now_client)
        if not agent_sys_id:
            return RitmAssignmentDecision(
                assigned=False,
                reason=f"no ServiceNow user found for {email}",
                incident=payload.number,
            )
        service_now_client.assign_ritm(payload.sys_id, agent_sys_id)

    # ── WebSocket push (P1 roster assignments only) ──────────────────────
    if is_p1:
        sg_id = str(agent.get("SG_ID") or "").strip()
        if sg_id:
            msg = {
                "type": "assignment",
                "sg_id": sg_id.upper(),
                "ticket_type": "ritm",
                "ticket_number": payload.number,
                "priority": payload.priority,
                "assignment_group": payload.assignment_group or "",
                "business_service": payload.business_service or "",
                "agent_name": agent.get("Employee_Name") or "",
                "agent_email": email or "",
                "assigned_at": datetime.now(assignment_service.timezone).isoformat(),
            }
            background_tasks.add_task(ws_manager.notify, sg_id, msg)

    open_ritms = service_now_client.list_open_ritms(
        exclude_sys_id=payload.sys_id,
        similar_to=payload.short_description or payload.business_service or "",
        business_service_sys_id=payload.business_service_sys_id,
        service_offering_sys_id=payload.service_offering_sys_id,
        assignment_group_sys_id=payload.assignment_group_sys_id,
    )
    matching_ritms = assignment_service.find_matching_incidents(
        payload.short_description or payload.business_service or "",
        open_ritms,
        threshold=0.55,
    )
    logger.info("similar ritms matched", extra={"ritm_number": payload.number, "match_count": len(matching_ritms)})

    work_notes = assignment_service.build_similar_incident_work_notes(payload.number, matching_ritms)
    if matching_ritms or ATTACH_NO_SIMILAR_WORK_NOTE:
        service_now_client.add_ritm_work_notes(payload.sys_id, work_notes)
    else:
        logger.info("skipping no-match work note because toggle is disabled", extra={"ritm_number": payload.number})

    return RitmAssignmentDecision(
        assigned=True,
        agent_name=agent.get("Employee_Name"),
        agent_email=email,
        incident=payload.number,
        matching_incidents=matching_ritms[:5],
    )
