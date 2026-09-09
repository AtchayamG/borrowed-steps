# Local verification guide

This walkthrough uses the real local API and SQLite store with the assistant disabled. It verifies the structured lending workflow, not hosted availability or live inference. Start both services using the [README](../README.md). Use synthetic data.

## Workspace and lending

1. Open the app at `http://127.0.0.1:5173`. Click **Start Synthetic Workspace**. Initial inventory contains an available wheelchair, an available walker and quarantined crutches.
2. Enter a synthetic borrower label, equipment kind, pickup location and future due time. Click **Register Structured Request**. Optionally register a second request for the same kind to inspect competition for the single available item.
3. On the first request, click **Allocate Equipment**. Select the available matching item, review the allocation and check the human approval box. Click **Confirm Allocation**. The equipment and loan become reserved. A competing request cannot allocate that reserved item.
4. On the reserved loan, click **Confirm Pickup**, check the confirmation box, then confirm in the dialog. The item is now on loan.
5. Click **Receive Return**, review and confirm the physical return. The equipment awaits inspection; return alone does not make it available.
6. Use the item's inspection action. Choose `QUARANTINED`, check the human approval box and click **Record Inspection**. The loan/request close while the equipment remains excluded from allocation. `REPAIR` and `AVAILABLE` are other explicit inspection outcomes. Quarantine is not permanent: a later human inspection can change it.
7. Reload the page. With the same session cookie and database, the persisted records remain. Restart the backend from the same directory and database path, then use **Sync Snapshot** to confirm persistence.

## Coordination and audit

Reservation creates a pickup task; pickup resolves it and creates a return task. A pending return task is shown as **Return reminder**, and a due one as **Return due**. Return resolves the return task. Resolution wording does not prove that a due event was emitted beforehand.

The backend runs a due-processing tick on startup and waits 30 seconds between subsequent bounded ticks. With the backend running, tasks can become due while the browser is closed. To observe a due transition, allow its deadline to pass, reopen the same browser session and click **Sync Snapshot**. Timing depends on the stored deadline and runner execution; do not assume an exact second.

Due audit records use entity type `loan` and action `PICKUP_DUE` or `RETURN_DUE`. The task status and event are persisted transactionally. The number of events in a manual run depends on actions and whether deadlines elapsed; do not require an unconditional seven-event total. The [recorded M2B proof](M2B_INTEGRATION.md) documents its particular seven-event run.

The UI shows the last successful sync time. It does not promise continuously fresh data. No email or SMS is sent.

## Failure and session checks

- Stop the backend and request a snapshot. The UI reports a connection error. Restart it, then click **Retry Connection**; recovery requires that action.
- A fresh browser context has no workspace cookie. The session prompt should appear instead of exposing another workspace.
- Stale competing mutations return a conflict instead of silently allocating the same item twice. After uncertainty, preserve the original operation's retry identity; do not create a replacement operation just to bypass it.
- At a narrow viewport, inspect the request, loan and coordination sections for readable content and usable controls. Keyboard-test the confirmation dialogs.

## Assistant boundary

With the assistant disabled, structured intake remains usable. Historical accepted Strands proof does not enable a model service in this walkthrough. If a separately authorized live assistant is available, a successful draft must carry actual tool provenance; **Use Draft (Populate Form)** only fills the form. **Register Structured Request** remains a separate human action. Failure must not fabricate a draft or write records.

No new model calls are authorized here. All 29 historical project probe allocations are closed. The [hosted direction](M3_HOSTED_DIRECTION.md) remains pending; local persistence is not proof of public deployment.
