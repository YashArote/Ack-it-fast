/*
  WHERE TO PASTE THIS
  --------------------
  System Definition > Business Rules > New

  Table:            Incident [incident]
    When to run tab:
        When:           async          <-- important: keeps this off the
                                                                                insert transaction so a slow/
                                                                                down Python server never delays
                                                                                ticket creation for the user
        Insert:         true
        Update:         true           <-- allowed for testing; guards below prevent loops
    Filter condition (recommended): Assigned to  is empty
    Do not require Assignment group to be non-empty; the bot should be
    allowed to write a no-agent work note when the group is missing.

  Advanced tab:
    Advanced:       true
    Script:         paste the function below

  BEFORE USE
  ----------
  1. Create a system property to hold the shared secret instead of
     hardcoding it: System Properties > New
       Name:  x_custom.assignment_bot_api_key
       Value: <same long random string as APP_API_KEY on the Python side>
  2. Replace ASSIGNMENT_BOT_URL below with your deployed URL,
     e.g. https://incident-assignment-bot.onrender.com/assign
  3. Make sure Outbound HTTP is allowed to that domain
     (System Web Services > Outbound > check your instance's
     egress/allow-list policy if one is configured).
*/

(function executeRule(current, previous /*null when async*/) {

    var ASSIGNMENT_BOT_URL = 'https://incident-assignment-bot.onrender.com/assign';

    // Only inspect the journal when it changed in this transaction. An older
    // bot note must not block a later retry after an assignment/group update.
    if (current.work_notes.changes()) {
        var latestJournal = current.work_notes.getJournalEntry(1).toString();
        var newlineIndex = latestJournal.indexOf('\n');
        var latestWorkNote = newlineIndex !== -1
            ? latestJournal.substring(newlineIndex + 1).trim()
            : latestJournal.trim();

        if (latestWorkNote.indexOf('[AssignmentBot]') === 0) {
            gs.info('[AssignmentBot] skipping bot-generated work-note update for ' + current.number);
            return;
        }
    }

    // Do not call the bot again after it has assigned the incident. This is
    // important when this rule is enabled for both insert and update.
    if (!current.assigned_to.nil()) {
        gs.info('[AssignmentBot] skipping already-assigned incident ' + current.number);
        return;
    }

    try {
        var request = new sn_ws.RESTMessageV2();
        request.setHttpMethod('POST');
        request.setEndpoint(ASSIGNMENT_BOT_URL);
        request.setRequestHeader('Content-Type', 'application/json');
        request.setRequestHeader('X-API-Key', 'ackitfast');
        request.setHttpTimeout(10000); // ms

        var payload = {
            sys_id: current.sys_id.toString(),
            number: current.number.toString(),
            short_description: current.short_description.toString(),
            business_service: current.business_service.getDisplayValue(),
            business_service_sys_id: current.business_service.toString(),
            service_offering: current.service_offering.getDisplayValue(),
            service_offering_sys_id: current.service_offering.toString(),
            assignment_group: current.assignment_group.getDisplayValue(),
            assignment_group_sys_id: current.assignment_group.toString(),
            priority: parseInt(current.priority.toString(), 10),
            opened_at: current.opened_at.toString() // "yyyy-MM-dd HH:mm:ss"
        };
        request.setRequestBody(JSON.stringify(payload));

        var response = request.execute();
        var httpStatus = response.getStatusCode();
        var responseBody = response.getBody();

        if (httpStatus == 200) {
            gs.info('[AssignmentBot] ' + current.number + ' -> ' + responseBody);
        } else {
            gs.warn('[AssignmentBot] ' + current.number + ' returned HTTP ' + httpStatus + ': ' + responseBody);
        }

    } catch (ex) {
        // Never let a bot outage break incident creation - just log it.
        gs.error('[AssignmentBot] call failed for ' + current.number + ': ' + ex.message);
    }

})(current, previous);
