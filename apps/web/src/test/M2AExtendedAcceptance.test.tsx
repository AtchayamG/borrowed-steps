import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import App from "../App";
import { IntakeAssistant } from "../components/IntakeAssistant";
import { RequestForm } from "../components/RequestForm";
import { api } from "../api/client";
import { MockBackendServer } from "./mocks/mockBackend";
import { validateIntakeResponse } from "../utils/validation";
import type { IntakeInterpretResponse, CreateRequestBody } from "../types/api";

const validResponse = (): IntakeInterpretResponse => ({
  draft: {
    borrower_label: "Ananya R.",
    equipment_kind: "WHEELCHAIR",
    pickup_location: "Velachery Community Room",
    due_at: "2026-09-20T10:00:37Z",
  },
  missing_fields: [],
  provenance: {
    framework: "strands",
    provider: "ollama",
    model: "llama3.2:3b",
    inventory_tool_calls: 1,
    completed_at: "2026-09-08T00:00:00Z",
  },
});

describe("BS-004-R1 Extended Acceptance Coverage", () => {
  let server: MockBackendServer;

  beforeEach(() => {
    server = new MockBackendServer();
    server.reset(true);
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
        return server.handleFetch(input, init);
      }),
    );
  });

  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  // 1. Delayed responses around expiry/recreation
  it("rejects delayed interpretation responses returning after workspace recreation or switch", async () => {
    let resolveDelayed: (val: IntakeInterpretResponse) => void = () => {};
    const delayedPromise = new Promise<IntakeInterpretResponse>((resolve) => {
      resolveDelayed = resolve;
    });

    vi.spyOn(api, "interpretIntake").mockImplementation(() => delayedPromise);

    const applyDraft = vi.fn();
    const { rerender } = render(
      <IntakeAssistant
        onApplyDraft={applyDraft}
        workspaceId="session-1"
        onStartWorkspace={vi.fn()}
      />,
    );

    // Trigger in-flight interpretation in session-1
    fireEvent.click(screen.getByRole("button", { name: "Interpret Intake" }));
    expect(
      screen.getByRole("button", { name: "Interpreting with Assistant..." }),
    ).toBeInTheDocument();

    // Recreate / switch workspace to session-2 before response arrives
    rerender(
      <IntakeAssistant
        onApplyDraft={applyDraft}
        workspaceId="session-2"
        onStartWorkspace={vi.fn()}
      />,
    );

    // Interpret button resets and lock is released for session-2
    expect(
      screen.getByRole("button", { name: "Interpret Intake" }),
    ).not.toBeDisabled();

    // Now delayed response from session-1 resolves
    resolveDelayed(validResponse());
    await new Promise((r) => setTimeout(r, 50));

    // Stale result from session-1 must NOT be displayed in session-2
    expect(
      screen.queryByRole("heading", { name: "Unsaved Intake Suggestion" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /Use Draft/ }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByText("Assistant Provenance (Validated Call)"),
    ).not.toBeInTheDocument();
  });

  // 2. Malformed 200 shapes validation
  it("strictly rejects various malformed 200 response shapes and protects UI rendering", async () => {
    // a. Root unknown field
    const extraFieldResp = {
      ...validResponse(),
      extra_field: "unexpected",
    };
    expect(() => validateIntakeResponse(extraFieldResp)).toThrow(
      /unexpected root field/i,
    );

    // b. Invalid equipment enum
    const invalidEnumResp = validResponse();
    (
      invalidEnumResp.draft as unknown as Record<string, unknown>
    ).equipment_kind = "ELECTRIC_SCOOTER";
    expect(() => validateIntakeResponse(invalidEnumResp)).toThrow(
      /equipment_kind must be WHEELCHAIR, WALKER, CRUTCHES/i,
    );

    // c. Borrower label exceeding 60 chars
    const longLabelResp = validResponse();
    longLabelResp.draft.borrower_label = "A".repeat(61);
    expect(() => validateIntakeResponse(longLabelResp)).toThrow(
      /borrower_label must be null or 1-60 characters/i,
    );

    // d. Invalid date string
    const invalidDateResp = validResponse();
    longLabelResp.draft.borrower_label = "Valid";
    invalidDateResp.draft.due_at = "not-a-valid-date";
    expect(() => validateIntakeResponse(invalidDateResp)).toThrow(
      /due_at must be null or a valid ISO timestamp/i,
    );

    // e. Missing field list inconsistent with null fields
    const inconsistentMissing = validResponse();
    inconsistentMissing.draft.borrower_label = null;
    inconsistentMissing.missing_fields = []; // should contain borrower_label
    expect(() => validateIntakeResponse(inconsistentMissing)).toThrow(
      /missing_fields does not match null draft fields/i,
    );

    // f. Out-of-contract provenance tool count (0)
    const zeroToolCalls = validResponse();
    inconsistentMissing.draft.borrower_label = "Valid";
    inconsistentMissing.missing_fields = [];
    zeroToolCalls.provenance.inventory_tool_calls = 0;
    expect(() => validateIntakeResponse(zeroToolCalls)).toThrow(
      /inventory_tool_calls must be an integer between 1 and 2/i,
    );

    // Verify IntakeAssistant safely renders error alert for malformed 200
    vi.spyOn(api, "interpretIntake").mockResolvedValue(zeroToolCalls);
    render(<IntakeAssistant onApplyDraft={vi.fn()} workspaceId="test-ws" />);
    fireEvent.click(screen.getByRole("button", { name: "Interpret Intake" }));

    await waitFor(() => {
      expect(screen.getByRole("alert")).toHaveTextContent(/invalid/i);
    });
    expect(
      screen.queryByRole("button", { name: /Use Draft/ }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByText("Assistant Provenance (Validated Call)"),
    ).not.toBeInTheDocument();
  });

  // 3. Disabled mode
  it("renders disabled mode banner and ensures manual form remains fully accessible", async () => {
    server.forceAssistantError = {
      status: 503,
      code: "ASSISTANT_DISABLED",
      message: "Assistant adapter is disabled via BS_ASSISTANT_ENABLED=false.",
    };

    render(<App />);
    await screen.findByText(/Standard Folding Wheelchair #1/);

    // Configured mode reflects disabled
    expect(screen.getByText(/Configured Mode:/i)).toBeInTheDocument();

    // Trigger interpretation in disabled mode
    fireEvent.click(screen.getByRole("button", { name: "Interpret Intake" }));

    await screen.findByRole("alert");
    expect(screen.getByRole("alert")).toHaveTextContent(
      /Assistant Unavailable \(503 ASSISTANT_DISABLED\)/i,
    );
    expect(screen.getByRole("alert")).toHaveTextContent(
      /Manual request entry is available below/i,
    );

    // User can still type and submit manually in RequestForm
    const borrowerInput = screen.getByLabelText(/Synthetic Borrower Label/i);
    fireEvent.change(borrowerInput, {
      target: { value: "Direct Volunteer Entry" },
    });

    fireEvent.click(
      screen.getByRole("button", { name: "Register Structured Request" }),
    );

    await screen.findByText(
      /Server confirmed: Request registered for Direct Volunteer Entry/i,
    );
  });

  // 4. Real form second-preserving submission
  it("preserves exact whole seconds from draft into HTML datetime-local input and through POST /api/requests", async () => {
    let capturedRequestBody: CreateRequestBody | null = null;
    const createSpy = vi
      .spyOn(api, "createRequest")
      .mockImplementation(async (body) => {
        capturedRequestBody = body;
        return {
          request: {
            id: "req-1",
            borrower_label: body.borrower_label,
            equipment_kind: body.equipment_kind,
            pickup_location: body.pickup_location,
            due_at: body.due_at,
            status: "REQUESTED",
            created_at: "2026-09-08T00:00:00Z",
          },
        };
      });

    const targetIsoWithSeconds = "2026-09-20T10:00:37Z";
    const draft = {
      borrower_label: "Second Precision Volunteer",
      equipment_kind: "CRUTCHES" as const,
      pickup_location: "Room 101, Velachery",
      due_at: targetIsoWithSeconds,
    };

    render(
      <RequestForm
        onSubmit={async (body) => {
          await api.createRequest(body, "idemp-test");
        }}
        draft={draft}
      />,
    );

    // Input receives value with seconds
    const dueInput = screen.getByLabelText(
      /Requested Return Due Date & Time/i,
    ) as HTMLInputElement;
    expect(dueInput.value).match(/:37(\.000)?$/);

    // Submit form
    fireEvent.click(
      screen.getByRole("button", { name: "Register Structured Request" }),
    );

    await waitFor(() => {
      expect(createSpy).toHaveBeenCalled();
    });

    // Exact epoch time down to second must match original ISO
    expect(capturedRequestBody).not.toBeNull();
    expect(new Date(capturedRequestBody!.due_at).getTime()).toBe(
      new Date(targetIsoWithSeconds).getTime(),
    );
  });

  // 5. No mutation on interpretation
  it("proves interpretation is strictly read-only and causes zero state mutations", async () => {
    const postSpy = vi.spyOn(window, "fetch");

    render(<App />);
    await screen.findByText(/Standard Folding Wheelchair #1/);

    const initialFetchCount = postSpy.mock.calls.length;

    // Trigger interpretation
    fireEvent.click(screen.getByRole("button", { name: "Interpret Intake" }));

    await screen.findByRole("heading", { name: "Unsaved Intake Suggestion" });

    // Verify no POST to /api/requests or /api/reservations occurred during interpretation
    const subsequentCalls = postSpy.mock.calls.slice(initialFetchCount);
    const mutationCalls = subsequentCalls.filter((call) => {
      const url = String(call[0]);
      const init = call[1] as RequestInit | undefined;
      return (
        (url.includes("/api/requests") || url.includes("/api/reservations")) &&
        init?.method === "POST"
      );
    });

    expect(mutationCalls.length).toBe(0);

    // Verify snapshot shows no added requests
    expect(screen.queryByText(/Status: REQUESTED/i)).not.toBeInTheDocument();
  });
});
