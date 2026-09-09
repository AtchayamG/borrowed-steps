import type {
  Snapshot,
  Equipment,
  BorrowRequest,
  Loan,
  CoordinationTask,
  DomainEvent,
  CreateRequestBody,
  CreateReservationBody,
  LoanPickupBody,
  LoanReturnBody,
  EquipmentInspectionBody,
  IntakeInterpretResponse,
  AgentMode,
} from "../../types/api";

export function createInitialSnapshot(): Snapshot {
  const initialEquipment: Equipment[] = [
    {
      id: "eq-wheelchair-01",
      label: "Standard Folding Wheelchair #1",
      kind: "WHEELCHAIR",
      state: "AVAILABLE",
      version: 1,
    },
    {
      id: "eq-walker-01",
      label: "Adjustable Aluminum Walker #1",
      kind: "WALKER",
      state: "AVAILABLE",
      version: 1,
    },
    {
      id: "eq-crutches-01",
      label: "Adult Axillary Crutches #1",
      kind: "CRUTCHES",
      state: "QUARANTINED",
      version: 1,
    },
  ];

  return {
    equipment: initialEquipment,
    requests: [],
    loans: [],
    events: [],
    tasks: [],
    agent_mode: "strands_ollama",
  };
}

export class MockBackendServer {
  private hasSession = false;
  private snapshot: Snapshot = createInitialSnapshot();
  public agentMode: AgentMode = "strands_ollama";
  public receivedIdempotencyKeys: string[] = [];
  public lastRequestBody: unknown = null;
  public lastRequestHeaders: Headers | null = null;
  public force409OnAllocation = false;
  public simulateNetworkDropForRoute: string | null = null;
  public simulateResponseDropAfterCommit = false;
  public forceAssistantError: {
    status: number;
    code: string;
    message: string;
  } | null = null;
  public mockInterpretationResponse: IntakeInterpretResponse | null = null;
  public interpretationCallCount = 0;
  public idempotentResponses = new Map<
    string,
    { status: number; body: unknown }
  >();

  public reset(authenticated = true) {
    this.hasSession = authenticated;
    this.snapshot = createInitialSnapshot();
    this.agentMode = "strands_ollama";
    this.receivedIdempotencyKeys = [];
    this.lastRequestBody = null;
    this.lastRequestHeaders = null;
    this.force409OnAllocation = false;
    this.simulateNetworkDropForRoute = null;
    this.simulateResponseDropAfterCommit = false;
    this.forceAssistantError = null;
    this.mockInterpretationResponse = null;
    this.interpretationCallCount = 0;
    this.idempotentResponses.clear();
  }

  public setSession(valid: boolean) {
    this.hasSession = valid;
  }

  public getSnapshotData(): Snapshot {
    return JSON.parse(JSON.stringify(this.snapshot));
  }

  public setTasks(tasks: CoordinationTask[]) {
    this.snapshot.tasks = JSON.parse(JSON.stringify(tasks));
  }

  public async handleFetch(
    input: RequestInfo | URL,
    init?: RequestInit,
  ): Promise<Response> {
    const urlStr =
      typeof input === "string"
        ? input
        : input instanceof URL
          ? input.toString()
          : input.url;
    const url = new URL(urlStr, "http://localhost");
    const path = url.pathname;
    const method = (init?.method || "GET").toUpperCase();
    const headers = new Headers(init?.headers);
    const idempotencyKey = headers.get("Idempotency-Key");

    this.lastRequestHeaders = headers;
    if (init?.body) {
      try {
        this.lastRequestBody = JSON.parse(init.body as string);
      } catch {
        this.lastRequestBody = init.body;
      }
    }

    if (idempotencyKey) {
      this.receivedIdempotencyKeys.push(idempotencyKey);
    }

    // Check simulated network drop for uncertain retry test
    if (
      this.simulateNetworkDropForRoute &&
      path === this.simulateNetworkDropForRoute
    ) {
      this.simulateNetworkDropForRoute = null;
      throw new TypeError("Failed to fetch (simulated network interruption)");
    }

    // Health (public)
    if (path === "/api/health" && method === "GET") {
      return new Response(
        JSON.stringify({
          status: "ok",
          milestone: "M2B",
          agent_mode: this.agentMode,
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      );
    }

    // Workspace creation (public/sets cookie)
    if (path === "/api/workspaces" && method === "POST") {
      this.hasSession = true;
      this.snapshot = createInitialSnapshot();
      return new Response(
        JSON.stringify({
          workspace: { id: "ws-mock-velachery-01" },
          snapshot: this.getSnapshotData(),
        }),
        { status: 201, headers: { "Content-Type": "application/json" } },
      );
    }

    // All subsequent routes require valid session cookie
    if (!this.hasSession) {
      return new Response(
        JSON.stringify({
          error: {
            code: "UNAUTHORIZED",
            message: "No valid or active bs_session cookie",
          },
        }),
        { status: 401, headers: { "Content-Type": "application/json" } },
      );
    }

    // Snapshot
    if (path === "/api/snapshot" && method === "GET") {
      return new Response(JSON.stringify(this.getSnapshotData()), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }

    // POST /api/intake/interpret (M2A Strands Assistant)
    if (path === "/api/intake/interpret" && method === "POST") {
      this.interpretationCallCount++;

      if (this.forceAssistantError) {
        return new Response(
          JSON.stringify({
            error: {
              code: this.forceAssistantError.code,
              message: this.forceAssistantError.message,
            },
          }),
          {
            status: this.forceAssistantError.status,
            headers: { "Content-Type": "application/json" },
          },
        );
      }

      if (this.mockInterpretationResponse) {
        return new Response(JSON.stringify(this.mockInterpretationResponse), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }

      const body = JSON.parse((init?.body as string) || "{}") as {
        text?: string;
      };
      const text = body.text || "";
      const trimmed = text.trim();

      if (!trimmed || trimmed.length > 2000) {
        return new Response(
          JSON.stringify({
            error: {
              code: "VALIDATION_ERROR",
              message: "Intake text must be between 1 and 2000 characters.",
            },
          }),
          {
            status: 422,
            headers: { "Content-Type": "application/json" },
          },
        );
      }

      let equipment_kind: "WHEELCHAIR" | "WALKER" | "CRUTCHES" | null = null;
      if (/wheelchair/i.test(trimmed)) equipment_kind = "WHEELCHAIR";
      else if (/walker/i.test(trimmed)) equipment_kind = "WALKER";
      else if (/crutch/i.test(trimmed)) equipment_kind = "CRUTCHES";

      let borrower_label: string | null = null;
      const borrowerMatch = trimmed.match(
        /(?:resident\s+)?([A-Z][a-z]+(?:\s+[A-Z]\.?)+)/,
      );
      if (borrowerMatch) {
        borrower_label = borrowerMatch[1] || null;
      } else if (trimmed.includes("Ananya R.")) {
        borrower_label = "Ananya R.";
      }

      let pickup_location: string | null = null;
      if (trimmed.toLowerCase().includes("velachery community room")) {
        pickup_location = "Velachery Community Room";
      }

      let due_at: string | null = null;
      const isoMatch = trimmed.match(
        /\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})/,
      );
      if (isoMatch) {
        due_at = isoMatch[0];
      }

      const draft = {
        borrower_label,
        equipment_kind,
        pickup_location,
        due_at,
      };

      const missing_fields: (
        "borrower_label" | "equipment_kind" | "pickup_location" | "due_at"
      )[] = [];
      if (!draft.borrower_label) missing_fields.push("borrower_label");
      if (!draft.equipment_kind) missing_fields.push("equipment_kind");
      if (!draft.pickup_location) missing_fields.push("pickup_location");
      if (!draft.due_at) missing_fields.push("due_at");

      const provenance = {
        framework: "strands" as const,
        provider: "ollama" as const,
        model: "llama3.2:3b" as const,
        inventory_tool_calls: 1,
        completed_at: new Date().toISOString(),
      };

      return new Response(
        JSON.stringify({
          draft,
          missing_fields,
          provenance,
        }),
        {
          status: 200,
          headers: { "Content-Type": "application/json" },
        },
      );
    }

    // Create Request
    if (path === "/api/requests" && method === "POST") {
      if (idempotencyKey && this.idempotentResponses.has(idempotencyKey)) {
        const cached = this.idempotentResponses.get(idempotencyKey)!;
        return new Response(JSON.stringify(cached.body), {
          status: cached.status,
          headers: { "Content-Type": "application/json" },
        });
      }

      const body = JSON.parse(
        (init?.body as string) || "{}",
      ) as CreateRequestBody;
      if (!body.borrower_label || body.borrower_label.length > 60) {
        return new Response(
          JSON.stringify({
            error: {
              code: "VALIDATION_ERROR",
              message: "Invalid borrower_label",
            },
          }),
          { status: 422, headers: { "Content-Type": "application/json" } },
        );
      }

      const newReq: BorrowRequest = {
        id: `req-${Date.now()}`,
        borrower_label: body.borrower_label,
        equipment_kind: body.equipment_kind,
        pickup_location: body.pickup_location,
        due_at: body.due_at,
        status: "REQUESTED",
        created_at: new Date().toISOString(),
      };

      const newEvent: DomainEvent = {
        id: `evt-${Date.now()}-1`,
        entity_type: "REQUEST",
        entity_id: newReq.id,
        action: "REQUEST_CREATED",
        at: new Date().toISOString(),
      };

      this.snapshot.requests.push(newReq);
      this.snapshot.events.unshift(newEvent);

      const responsePayload = { request: newReq };
      if (idempotencyKey) {
        this.idempotentResponses.set(idempotencyKey, {
          status: 200,
          body: responsePayload,
        });
      }

      if (this.simulateResponseDropAfterCommit) {
        this.simulateResponseDropAfterCommit = false;
        throw new TypeError("Failed to fetch (response dropped after commit)");
      }

      return new Response(JSON.stringify(responsePayload), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }

    // Create Reservation
    if (path === "/api/reservations" && method === "POST") {
      const body = JSON.parse(
        (init?.body as string) || "{}",
      ) as CreateReservationBody;
      if (!body.human_approved) {
        return new Response(
          JSON.stringify({
            error: {
              code: "APPROVAL_REQUIRED",
              message: "Human approval is required",
            },
          }),
          { status: 422, headers: { "Content-Type": "application/json" } },
        );
      }

      if (this.force409OnAllocation) {
        return new Response(
          JSON.stringify({
            error: {
              code: "STATE_CONFLICT",
              message:
                "Equipment was reserved by a concurrent volunteer update",
            },
          }),
          { status: 409, headers: { "Content-Type": "application/json" } },
        );
      }

      const item = this.snapshot.equipment.find(
        (e) => e.id === body.equipment_id,
      );
      const req = this.snapshot.requests.find((r) => r.id === body.request_id);

      if (
        !item ||
        item.state !== "AVAILABLE" ||
        item.version !== body.expected_equipment_version
      ) {
        return new Response(
          JSON.stringify({
            error: {
              code: "STATE_CONFLICT",
              message: "Equipment is not available or version changed",
            },
          }),
          { status: 409, headers: { "Content-Type": "application/json" } },
        );
      }

      if (!req || req.status !== "REQUESTED") {
        return new Response(
          JSON.stringify({
            error: {
              code: "STATE_CONFLICT",
              message: "Request is not in REQUESTED state",
            },
          }),
          { status: 409, headers: { "Content-Type": "application/json" } },
        );
      }

      // Update states
      item.state = "RESERVED";
      item.version += 1;
      req.status = "RESERVED";

      const loan: Loan = {
        id: `loan-${Date.now()}`,
        request_id: req.id,
        equipment_id: item.id,
        status: "RESERVED",
        due_at: req.due_at,
        created_at: new Date().toISOString(),
      };

      const pickupTask: CoordinationTask = {
        id: `task-pickup-${loan.id}`,
        loan_id: loan.id,
        kind: "PICKUP_DUE",
        status: "PENDING",
        due_at: loan.created_at,
        created_at: loan.created_at,
      };

      const event: DomainEvent = {
        id: `evt-${Date.now()}-2`,
        entity_type: "LOAN",
        entity_id: loan.id,
        action: "RESERVED",
        at: new Date().toISOString(),
      };

      this.snapshot.loans.push(loan);
      this.snapshot.tasks.push(pickupTask);
      this.snapshot.events.unshift(event);

      return new Response(
        JSON.stringify({ request: req, equipment: item, loan }),
        {
          status: 200,
          headers: { "Content-Type": "application/json" },
        },
      );
    }

    // Pickup
    const pickupMatch = path.match(/^\/api\/loans\/([^/]+)\/pickup$/);
    if (pickupMatch && method === "POST") {
      const loanId = decodeURIComponent(pickupMatch[1]!);
      const body = JSON.parse((init?.body as string) || "{}") as LoanPickupBody;

      if (!body.human_approved) {
        return new Response(
          JSON.stringify({
            error: {
              code: "APPROVAL_REQUIRED",
              message: "Human approval is required",
            },
          }),
          { status: 422, headers: { "Content-Type": "application/json" } },
        );
      }

      const loan = this.snapshot.loans.find((l) => l.id === loanId);
      if (!loan || loan.status !== "RESERVED") {
        return new Response(
          JSON.stringify({
            error: {
              code: "STATE_CONFLICT",
              message: "Loan is not in RESERVED state",
            },
          }),
          { status: 409, headers: { "Content-Type": "application/json" } },
        );
      }

      const item = this.snapshot.equipment.find(
        (e) => e.id === loan.equipment_id,
      );
      const req = this.snapshot.requests.find((r) => r.id === loan.request_id);

      if (!item || item.version !== body.expected_equipment_version) {
        return new Response(
          JSON.stringify({
            error: { code: "STATE_CONFLICT", message: "Item version mismatch" },
          }),
          { status: 409, headers: { "Content-Type": "application/json" } },
        );
      }

      loan.status = "ON_LOAN";
      item.state = "ON_LOAN";
      item.version += 1;
      if (req) req.status = "ON_LOAN";

      // Resolve loan's PICKUP_DUE task and create RETURN_DUE task
      for (const t of this.snapshot.tasks) {
        if (t.loan_id === loan.id && t.kind === "PICKUP_DUE") {
          t.status = "RESOLVED";
        }
      }
      const returnTask: CoordinationTask = {
        id: `task-return-${loan.id}`,
        loan_id: loan.id,
        kind: "RETURN_DUE",
        status: "PENDING",
        due_at: loan.due_at,
        created_at: new Date().toISOString(),
      };
      this.snapshot.tasks.push(returnTask);

      const event: DomainEvent = {
        id: `evt-${Date.now()}-3`,
        entity_type: "LOAN",
        entity_id: loan.id,
        action: "PICKED_UP",
        at: new Date().toISOString(),
      };
      this.snapshot.events.unshift(event);

      return new Response(
        JSON.stringify({ request: req, equipment: item, loan }),
        {
          status: 200,
          headers: { "Content-Type": "application/json" },
        },
      );
    }

    // Return
    const returnMatch = path.match(/^\/api\/loans\/([^/]+)\/return$/);
    if (returnMatch && method === "POST") {
      const loanId = decodeURIComponent(returnMatch[1]!);
      const body = JSON.parse((init?.body as string) || "{}") as LoanReturnBody;

      if (!body.human_approved) {
        return new Response(
          JSON.stringify({
            error: {
              code: "APPROVAL_REQUIRED",
              message: "Human approval is required",
            },
          }),
          { status: 422, headers: { "Content-Type": "application/json" } },
        );
      }

      const loan = this.snapshot.loans.find((l) => l.id === loanId);
      if (!loan || loan.status !== "ON_LOAN") {
        return new Response(
          JSON.stringify({
            error: { code: "STATE_CONFLICT", message: "Loan is not ON_LOAN" },
          }),
          { status: 409, headers: { "Content-Type": "application/json" } },
        );
      }

      const item = this.snapshot.equipment.find(
        (e) => e.id === loan.equipment_id,
      );
      const req = this.snapshot.requests.find((r) => r.id === loan.request_id);

      if (!item || item.version !== body.expected_equipment_version) {
        return new Response(
          JSON.stringify({
            error: { code: "STATE_CONFLICT", message: "Item version mismatch" },
          }),
          { status: 409, headers: { "Content-Type": "application/json" } },
        );
      }

      loan.status = "RETURNED";
      item.state = "AWAITING_INSPECTION";
      item.version += 1;
      if (req) req.status = "RETURNED";

      // Resolve loan's RETURN_DUE task
      for (const t of this.snapshot.tasks) {
        if (t.loan_id === loan.id && t.kind === "RETURN_DUE") {
          t.status = "RESOLVED";
        }
      }

      const event: DomainEvent = {
        id: `evt-${Date.now()}-4`,
        entity_type: "LOAN",
        entity_id: loan.id,
        action: "RETURNED",
        at: new Date().toISOString(),
      };
      this.snapshot.events.unshift(event);

      return new Response(
        JSON.stringify({ request: req, equipment: item, loan }),
        {
          status: 200,
          headers: { "Content-Type": "application/json" },
        },
      );
    }

    // Inspection
    const inspectMatch = path.match(/^\/api\/equipment\/([^/]+)\/inspection$/);
    if (inspectMatch && method === "POST") {
      const equipmentId = decodeURIComponent(inspectMatch[1]!);
      const body = JSON.parse(
        (init?.body as string) || "{}",
      ) as EquipmentInspectionBody;

      if (!body.human_approved) {
        return new Response(
          JSON.stringify({
            error: {
              code: "APPROVAL_REQUIRED",
              message: "Human approval is required",
            },
          }),
          { status: 422, headers: { "Content-Type": "application/json" } },
        );
      }

      const item = this.snapshot.equipment.find((e) => e.id === equipmentId);
      if (
        !item ||
        !["AWAITING_INSPECTION", "REPAIR", "QUARANTINED"].includes(
          item.state,
        ) ||
        item.version !== body.expected_equipment_version
      ) {
        return new Response(
          JSON.stringify({
            error: {
              code: "STATE_CONFLICT",
              message: "Equipment not in inspectable state or version mismatch",
            },
          }),
          { status: 409, headers: { "Content-Type": "application/json" } },
        );
      }

      item.state = body.outcome;
      item.version += 1;

      // Close returned loan/request if present
      const returnedLoan = this.snapshot.loans.find(
        (l) => l.equipment_id === item.id && l.status === "RETURNED",
      );
      let closedReq: BorrowRequest | null = null;
      if (returnedLoan) {
        returnedLoan.status = "CLOSED";
        const req = this.snapshot.requests.find(
          (r) => r.id === returnedLoan.request_id,
        );
        if (req) {
          req.status = "CLOSED";
          closedReq = req;
        }
      }

      const eventAction = `INSPECTED_${body.outcome}`;
      const event: DomainEvent = {
        id: `evt-${Date.now()}-5`,
        entity_type: "EQUIPMENT",
        entity_id: item.id,
        action: eventAction,
        at: new Date().toISOString(),
      };
      this.snapshot.events.unshift(event);

      return new Response(
        JSON.stringify({
          equipment: item,
          request: closedReq,
          loan: returnedLoan || null,
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      );
    }

    return new Response(
      JSON.stringify({
        error: { code: "NOT_FOUND", message: `No route for ${method} ${path}` },
      }),
      { status: 404, headers: { "Content-Type": "application/json" } },
    );
  }
}
