import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import {
  render,
  screen,
  waitFor,
  fireEvent,
  within,
} from "@testing-library/react";
import App from "../App";
import { MockBackendServer } from "./mocks/mockBackend";
import type { IntakeInterpretResponse } from "../types/api";

describe("Borrowed Steps M2A — Human-Reviewed Intake Suggestion Tests", () => {
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
  });

  it("Mocked-backend test: Successful draft -> review -> deliberate 'Use draft' -> separate explicit request submission", async () => {
    render(<App />);

    // Wait for snapshot to load
    await waitFor(() => {
      expect(
        screen.getByText(/Standard Folding Wheelchair #1/i),
      ).toBeInTheDocument();
    });

    const intakeHeading = screen.getByText(/Synthetic Intake Assistant/i);
    expect(intakeHeading).toBeInTheDocument();

    const textarea = screen.getByLabelText(/Synthetic Intake Notes/i);
    expect(textarea).toBeInTheDocument();

    // Verify provenance is NOT shown on load
    expect(
      screen.queryByText(/Assistant Provenance \(Validated Call\)/i),
    ).not.toBeInTheDocument();

    // Type bounded synthetic intake text
    fireEvent.change(textarea, {
      target: {
        value:
          "Velachery resident Ananya R. requires a wheelchair pickup at Velachery Community Room by 2026-09-20T10:00:00Z",
      },
    });

    const interpretBtn = screen.getByRole("button", {
      name: /Interpret Intake/i,
    });
    expect(interpretBtn).toBeInTheDocument();
    fireEvent.click(interpretBtn);

    // Verify unsaved draft suggestion card renders
    await waitFor(() => {
      expect(
        screen.getByRole("heading", { name: /Unsaved Intake Suggestion/i }),
      ).toBeInTheDocument();
    });

    expect(screen.getByText(/Unsaved Ephemeral Draft/i)).toBeInTheDocument();
    expect(
      screen.getByText(
        /This suggestion is an ephemeral advisory draft and has NOT created a request or reservation/i,
      ),
    ).toBeInTheDocument();

    // Verify extracted fields in review
    const suggestionBox = screen
      .getByRole("heading", { name: /Unsaved Intake Suggestion/i })
      .closest(".intake-suggestion-box") as HTMLElement;
    expect(within(suggestionBox).getByText("Ananya R.")).toBeInTheDocument();
    expect(within(suggestionBox).getByText("WHEELCHAIR")).toBeInTheDocument();
    expect(
      within(suggestionBox).getByText("Velachery Community Room"),
    ).toBeInTheDocument();
    expect(
      within(suggestionBox).getAllByText(/2026-09-20T10:00:00Z/).length,
    ).toBeGreaterThanOrEqual(1);

    // Verify validated provenance is now displayed
    expect(
      screen.getByText(/Assistant Provenance \(Validated Call\)/i),
    ).toBeInTheDocument();
    expect(screen.getByText("strands")).toBeInTheDocument();
    expect(screen.getByText("ollama")).toBeInTheDocument();
    expect(screen.getByText("llama3.2:3b")).toBeInTheDocument();

    // Verify interpretation was read-only: snapshot requests list is still empty!
    expect(screen.queryByText(/Status: REQUESTED/i)).not.toBeInTheDocument();

    // Deliberate action: click 'Use draft'
    const useDraftBtn = screen.getByRole("button", {
      name: /Use Draft \(Populate Form\)/i,
    });
    fireEvent.click(useDraftBtn);

    // Verify form was populated
    const borrowerInput = screen.getByLabelText(
      /Synthetic Borrower Label/i,
    ) as HTMLInputElement;
    expect(borrowerInput.value).toBe("Ananya R.");

    const equipmentSelect = screen.getByLabelText(
      /Requested Equipment Kind/i,
    ) as HTMLSelectElement;
    expect(equipmentSelect.value).toBe("WHEELCHAIR");

    const locationInput = screen.getByLabelText(
      /Pickup Location/i,
    ) as HTMLInputElement;
    expect(locationInput.value).toBe("Velachery Community Room");

    const dueInput = screen.getByLabelText(
      /Requested Return Due Date & Time/i,
    ) as HTMLInputElement;
    expect(dueInput.value).match(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/);

    // Separate explicit request submission
    const registerBtn = screen.getByRole("button", {
      name: /Register Structured Request/i,
    });
    fireEvent.click(registerBtn);

    // Verify request confirmed on server
    await waitFor(() => {
      expect(
        screen.getByText(
          /Server confirmed: Request registered for Ananya R\./i,
        ),
      ).toBeInTheDocument();
    });
  });

  it("Mocked-backend test: Missing and null fields display clear clarification needed notices", async () => {
    const mockResponse: IntakeInterpretResponse = {
      draft: {
        borrower_label: null,
        equipment_kind: null,
        pickup_location: null,
        due_at: null,
      },
      missing_fields: [
        "borrower_label",
        "equipment_kind",
        "pickup_location",
        "due_at",
      ],
      provenance: {
        framework: "strands",
        provider: "ollama",
        model: "llama3.2:3b",
        inventory_tool_calls: 1,
        completed_at: new Date().toISOString(),
      },
    };
    server.mockInterpretationResponse = mockResponse;

    render(<App />);

    await waitFor(() => {
      expect(
        screen.getByText(/Synthetic Intake Assistant/i),
      ).toBeInTheDocument();
    });

    const textarea = screen.getByLabelText(/Synthetic Intake Notes/i);
    fireEvent.change(textarea, {
      target: {
        value: "Someone NEEDS equipment next Tuesday somewhere in Chennai",
      },
    });

    const interpretBtn = screen.getByRole("button", {
      name: /Interpret Intake/i,
    });
    fireEvent.click(interpretBtn);

    await waitFor(() => {
      expect(
        screen.getByRole("heading", { name: /Unsaved Intake Suggestion/i }),
      ).toBeInTheDocument();
    });

    // Verify clarification notices for missing/null fields
    expect(
      screen.getByText(/Clarification needed: Borrower label not identified/i),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        /Clarification needed: No explicit equipment kind found/i,
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/Clarification needed: Pickup location not identified/i),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        /Clarification needed: Full ISO datetime required \(relative dates like 'tomorrow' are unsupported\)/i,
      ),
    ).toBeInTheDocument();

    // Deliberate 'Use Draft' action
    const useDraftBtn = screen.getByRole("button", {
      name: /Use Draft \(Populate Form\)/i,
    });
    fireEvent.click(useDraftBtn);

    // Form inputs should be empty for missing/null fields
    const borrowerInput = screen.getByLabelText(
      /Synthetic Borrower Label/i,
    ) as HTMLInputElement;
    expect(borrowerInput.value).toBe("");

    const equipmentSelect = screen.getByLabelText(
      /Requested Equipment Kind/i,
    ) as HTMLSelectElement;
    expect(equipmentSelect.value).toBe("");

    // Submitting with empty fields triggers client-side validation
    const registerBtn = screen.getByRole("button", {
      name: /Register Structured Request/i,
    });
    fireEvent.click(registerBtn);

    await waitFor(() => {
      expect(
        screen.getByText(/Borrower label must be between 1 and 60 characters/i),
      ).toBeInTheDocument();
    });
  });

  it("Mocked-backend test: Handles 429 ASSISTANT_BUSY honestly with active-flow alert and preserved input", async () => {
    server.forceAssistantError = {
      status: 429,
      code: "ASSISTANT_BUSY",
      message: "Another inference request is currently in progress.",
    };

    render(<App />);

    await waitFor(() => {
      expect(
        screen.getByText(/Synthetic Intake Assistant/i),
      ).toBeInTheDocument();
    });

    const textarea = screen.getByLabelText(
      /Synthetic Intake Notes/i,
    ) as HTMLTextAreaElement;
    const testText = "Need wheelchair for Suresh K. by 2026-09-25T10:00:00Z";
    fireEvent.change(textarea, { target: { value: testText } });

    const interpretBtn = screen.getByRole("button", {
      name: /Interpret Intake/i,
    });
    fireEvent.click(interpretBtn);

    await waitFor(() => {
      const alert = screen.getByRole("alert");
      expect(alert).toHaveTextContent(/Assistant Busy \(429 ASSISTANT_BUSY\)/i);
    });

    // Verify textarea input preserved
    expect(textarea.value).toBe(testText);

    // Verify deliberate retry button is present
    const retryBtn = screen.getByRole("button", {
      name: /Retry Interpretation/i,
    });
    expect(retryBtn).toBeInTheDocument();

    // Clear backend error and retry
    server.forceAssistantError = null;
    fireEvent.click(retryBtn);

    await waitFor(() => {
      expect(
        screen.getByRole("heading", { name: /Unsaved Intake Suggestion/i }),
      ).toBeInTheDocument();
    });
  });

  it("Mocked-backend test: Handles 503 ASSISTANT_DISABLED / ASSISTANT_UNAVAILABLE honestly with manual entry available", async () => {
    server.forceAssistantError = {
      status: 503,
      code: "ASSISTANT_DISABLED",
      message: "Local strands assistant adapter is disabled in configuration.",
    };

    render(<App />);

    await waitFor(() => {
      expect(
        screen.getByText(/Synthetic Intake Assistant/i),
      ).toBeInTheDocument();
    });

    const interpretBtn = screen.getByRole("button", {
      name: /Interpret Intake/i,
    });
    fireEvent.click(interpretBtn);

    await waitFor(() => {
      const alert = screen.getByRole("alert");
      expect(alert).toHaveTextContent(
        /Assistant Unavailable \(503 ASSISTANT_DISABLED\)/i,
      );
      expect(alert).toHaveTextContent(
        /Manual request entry is available below/i,
      );
    });

    // Manual form entry remains fully functional
    expect(
      screen.getByRole("heading", { name: /Intake New Request/i }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /Register Structured Request/i }),
    ).toBeInTheDocument();
  });

  it("Mocked-backend test: Handles 502 ASSISTANT_INVALID_OUTPUT and 504 ASSISTANT_TIMEOUT", async () => {
    server.forceAssistantError = {
      status: 502,
      code: "ASSISTANT_INVALID_OUTPUT",
      message: "Model output exceeded iteration limits.",
    };

    render(<App />);

    await waitFor(() => {
      expect(
        screen.getByText(/Synthetic Intake Assistant/i),
      ).toBeInTheDocument();
    });

    const interpretBtn = screen.getByRole("button", {
      name: /Interpret Intake/i,
    });
    fireEvent.click(interpretBtn);

    await waitFor(() => {
      const alert = screen.getByRole("alert");
      expect(alert).toHaveTextContent(
        /Assistant Invalid Output \(502 ASSISTANT_INVALID_OUTPUT\)/i,
      );
    });

    // Now test 504
    server.forceAssistantError = {
      status: 504,
      code: "ASSISTANT_TIMEOUT",
      message: "Inference timed out after 120s.",
    };

    const retryBtn = screen.getByRole("button", {
      name: /Retry Interpretation/i,
    });
    fireEvent.click(retryBtn);

    await waitFor(() => {
      const alert = screen.getByRole("alert");
      expect(alert).toHaveTextContent(
        /Assistant Timeout \(504 ASSISTANT_TIMEOUT\)/i,
      );
    });
  });

  it("Mocked-backend test: Handles 401 session expiry without automatic workspace reset", async () => {
    server.forceAssistantError = {
      status: 401,
      code: "SESSION_REQUIRED",
      message: "No valid or active bs_session cookie",
    };

    render(<App />);

    await waitFor(() => {
      expect(
        screen.getByText(/Synthetic Intake Assistant/i),
      ).toBeInTheDocument();
    });

    const textarea = screen.getByLabelText(
      /Synthetic Intake Notes/i,
    ) as HTMLTextAreaElement;
    const testText = "Preserve this intake text across session expiry";
    fireEvent.change(textarea, { target: { value: testText } });

    const interpretBtn = screen.getByRole("button", {
      name: /Interpret Intake/i,
    });
    fireEvent.click(interpretBtn);

    await waitFor(() => {
      expect(
        screen.getByText(/Session Required \(401\): Active session expired/i),
      ).toBeInTheDocument();
    });

    // Does NOT auto-create workspace; shows deliberate prompt
    expect(screen.getByText(/Your session has expired/i)).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /Start Synthetic Workspace/i }),
    ).toBeInTheDocument();
  });

  it("Mocked-backend test: Handles network/offline failure honestly", async () => {
    server.simulateNetworkDropForRoute = "/api/intake/interpret";

    render(<App />);

    await waitFor(() => {
      expect(
        screen.getByText(/Synthetic Intake Assistant/i),
      ).toBeInTheDocument();
    });

    const interpretBtn = screen.getByRole("button", {
      name: /Interpret Intake/i,
    });
    fireEvent.click(interpretBtn);

    await waitFor(() => {
      const alert = screen.getByRole("alert");
      expect(alert).toHaveTextContent(
        /Connection Error: Backend unreachable or offline/i,
      );
    });
  });

  it("Mocked-backend test: Client validation guards against empty text and text exceeding 2000 chars", async () => {
    render(<App />);

    await waitFor(() => {
      expect(
        screen.getByText(/Synthetic Intake Assistant/i),
      ).toBeInTheDocument();
    });

    const textarea = screen.getByLabelText(/Synthetic Intake Notes/i);
    fireEvent.change(textarea, { target: { value: "   " } });

    const interpretBtn = screen.getByRole("button", {
      name: /Interpret Intake/i,
    });
    expect(interpretBtn).toBeDisabled();

    // Verify long text validation
    const longText = "a".repeat(2005);
    fireEvent.change(textarea, { target: { value: longText.slice(0, 2000) } });
    expect((textarea as HTMLTextAreaElement).value.length).toBe(2000);
  });
});
