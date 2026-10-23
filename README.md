# Ack-it-fast User Guide

## What Ack-it-fast does

Ack-it-fast helps assign ServiceNow incidents to the right support person. It checks the incident priority, team, working hours, availability, and current workload. It then assigns the incident and records useful related-incident information in Work notes.

The service runs in the background. Users continue working in ServiceNow as usual.

## Terms used in this guide

### Incident
A support ticket in ServiceNow.

### Priority
How urgently an incident needs attention. P1 is the highest priority. P2, P3, P4, and P5 are lower priorities.

### Assignment group
The support team responsible for the incident, for example `GDI-SAP_CC-FI-L1`.

### Shift roster
The shift roster API supplies SGIDs, business-service membership, availability, and shift data for P1 assignments.

### Workload
The number of active incidents currently assigned to a person.

### High-priority incidents
The number of active P1 and P2 incidents currently assigned to a person.

## Assignment rules

### P1 incidents

P1 incidents use the shift roster API only. A person can be selected when:

- The API returns them for the incident's Assignment group and the current date in India.
- Their `businessServices` include the incident's business service.
- Their availability status is `PRESENT`.
- Their shift covers the current time in India: M is 09:30–18:30 and A is 12:30–21:30, including both endpoints.
- Their SGID resolves to an active ServiceNow user (`user_name`) when ServiceNow credentials are configured.

The incident's original opening time is not used. The check uses the current time when the assignment is made.
Set `SHIFT_ROSTER_API_KEY` to the roster API key. If the API request fails, P1 assignment stops without falling back to Excel.

To test the live roster API without reading or changing a ServiceNow ticket, run:

```text
python check_shift_roster_api.py
```

The script defaults to assignment group `GDI-SAP_CC-Authorizations` and business service `BS_GDI_SAP_E2000`. It uses the current date and time in `WORKSPACE_TIMEZONE`, reads the API key from the process environment or local `.env`, and does not print the key or employee SGIDs. To verify the deployed environment, run the script in that environment; running it locally only verifies local configuration.

Workload is not considered for P1 incidents. If several people meet all conditions, assignments rotate between those eligible people.

### Non-P1 incidents

P2 to P5 incidents do not use the roster. Ack-it-fast checks active members of the incident's Assignment group and compares their workload.

The person with the lowest score is preferred. If several people have the same lowest score, assignments rotate between them.

## Workload score

For non-P1 incidents:

```text
Workload score = active incidents
                 + high-priority incidents
                 + priority adjustment
```

The priority adjustment for non-P1 incidents is `2`.

Example:

| Person | Active incidents | High-priority incidents | Adjustment | Score |
|---|---:|---:|---:|---:|
| Asha | 4 | 1 | 2 | 7 |
| Ravi | 2 | 0 | 2 | 4 |
| Neha | 2 | 0 | 2 | 4 |

Ravi and Neha have the lowest score, so assignments rotate between them. Asha is not selected because her score is higher.

All eligible people's workload is checked together so the result does not depend on checking people one by one.

## Work notes

Ack-it-fast looks for active incidents with similar descriptions. The best matches are added to the current incident's Work notes.

If no similar incident is found, this note is added when enabled:

```text
Assignment bot found no similar open incidents for INC123456.
```

If no eligible person is available, this note is added when enabled:

```text
[AssignmentBot] Assignment bot could not find an eligible agent for INC123456.
```

## Testing an assignment

### 1. Wake up the service

Open:

```text
https://incident-assignment-bot.onrender.com/health
```

Wait until the page shows:

```json
{"status":"ok"}
```

The first request may take longer if the service has been inactive.

### 2. Open an incident

Sign in to ServiceNow using your normal approved account.
```test
Url:- dev405393.service-now.com
username:- admin
password:- 6^%oNrNFxW1k
```

Open the in-progress incident list:

```text
https://dev405393.service-now.com/incident_list.do?sysparm_query=state%3D2%5EORDERBYDESCsys_updated_on&sysparm_first_row=1&sysparm_view=
```

Open an incident and check its Priority, Assignment group, Assigned to, Impact, and Urgency.

### 3. Prepare the test

Make sure the Assignment group is filled in.

For a P1 test, download the current roster and confirm that at least one person:

- belongs to the exact Assignment group
- is marked Available
- is currently within their working hours in India

For a non-P1 test, confirm that the expected people are active members of the ServiceNow Assignment group. The roster is not used for this path.

### 4. Start the test

1. Clear `Assigned to` if a person is already selected.
2. Change Impact and Urgency if you want to test a different priority.
3. If the priority is already correct, change another harmless field such as Category.
4. Save the incident with `Ctrl+S`.
5. Wait for the assignment to finish.

Avoid saving repeatedly while waiting. Wait for the previous attempt to complete before trying again.

### 5. Check the result

For a successful assignment:

- `Assigned to` contains the selected person.
- The person belongs to the correct team.
- For P1, the person is currently within their working hours.
- Work notes contain similar incidents or the no-similar message.

When no eligible person is found:

- `Assigned to` remains empty.
- A no-agent message appears in Work notes if enabled.
- To retry after someone becomes available, change a meaningful field such as Assignment group or Priority and save.

## Legacy Excel roster

The old Excel upload/download page remains available for compatibility, but uploaded files are no longer used to make assignments. Configure `SHIFT_ROSTER_API_URL` and `SHIFT_ROSTER_API_KEY` for the shift roster API instead.

Legacy page (no longer needed for assignments):

```text
https://incident-assignment-bot.onrender.com/roster/upload
```

Excel changes do not affect assignments. Non-P1 assignments continue to use ServiceNow assignment-group members and workload.
