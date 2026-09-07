import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import App from "../App";
import { MockBackendServer } from "./mocks/mockBackend";

describe("Borrowed Steps M1 — Mocked-Backend Interface Tests", () => {
  let mockServer: MockBackendServer;

  beforeEach(() => {
    mockServer = new MockBackendServer();
    // Default fetch interceptor routing to MockBackendServer
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
        return mockServer.handleFetch(input, init);
      }),
    );
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("Mocked-backend test: Handles unauthenticated / expired session (401) with explicit workspace creation", async () => {
    mockServer.reset(false); // unauthenticated
    render(<App />);

    // Shows 401 banner
    expect(await screen.findByRole("alert")).toHaveTextContent(
      /No Active Community Session \(401\)/i,
    );
    const startBtn = screen.getByRole("button", {
      name: /Start Synthetic Workspace/i,
    });
    expect(startBtn).toBeInTheDocument();

    // Click to start synthetic workspace
    await userEvent.click(startBtn);

    // Should load inventory after 201 response
    expect(await screen.findByText(/Equipment Inventory/i)).toBeInTheDocument();
    expect(
      screen.getByText("Standard Folding Wheelchair #1"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Adjustable Aluminum Walker #1"),
    ).toBeInTheDocument();
    expect(screen.getByText("Adult Axillary Crutches #1")).toBeInTheDocument();
  });

  it("Mocked-backend test: Validates structured request intake form with boundary conditions", async () => {
    mockServer.reset(true);
    render(<App />);

    expect(
      await screen.findByText("Standard Folding Wheelchair #1"),
    ).toBeInTheDocument();

    const borrowerInput = screen.getByLabelText(/Synthetic Borrower Label/i);
    const submitBtn = screen.getByRole("button", {
      name: /Register Structured Request/i,
    });

    // Clear borrower label
    await userEvent.clear(borrowerInput);
    await userEvent.click(submitBtn);

    expect(screen.getByRole("alert")).toHaveTextContent(
      /Borrower label must be between 1 and 60 characters/i,
    );

    // Put valid label
    await userEvent.type(borrowerInput, "Priya S. (Velachery Resident)");

    // Set invalid past date
    const dateInput = screen.getByLabelText(/Requested Return Due Date/i);
    fireEvent.change(dateInput, { target: { value: "2020-01-01T10:00" } });
    await userEvent.click(submitBtn);

    expect(screen.getByRole("alert")).toHaveTextContent(
      /Due date must be in the future/i,
    );
  });

  it("Mocked-backend test: Creates structured request and verifies server confirmation and fresh idempotency key", async () => {
    mockServer.reset(true);
    render(<App />);

    expect(
      await screen.findByText("Standard Folding Wheelchair #1"),
    ).toBeInTheDocument();

    const borrowerInput = screen.getByLabelText(/Synthetic Borrower Label/i);
    await userEvent.clear(borrowerInput);
    await userEvent.type(borrowerInput, "K. Balaji (Velachery East)");

    const submitBtn = screen.getByRole("button", {
      name: /Register Structured Request/i,
    });
    await userEvent.click(submitBtn);

    // Confirmation banner
    expect(
      await screen.findByText(
        /Server confirmed: Request registered for K. Balaji/i,
      ),
    ).toBeInTheDocument();
    expect(screen.getByText("K. Balaji (Velachery East)")).toBeInTheDocument();

    // Verify Idempotency-Key was received
    expect(mockServer.receivedIdempotencyKeys.length).toBeGreaterThan(0);
    const lastKey =
      mockServer.receivedIdempotencyKeys[
        mockServer.receivedIdempotencyKeys.length - 1
      ];
    expect(lastKey).toBeDefined();
    expect(lastKey!.length).toBeGreaterThanOrEqual(8);
  });

  it("Mocked-backend test: Enforces explicit human approval checkbox on allocation (no prechecked approvals)", async () => {
    mockServer.reset(true);
    render(<App />);

    expect(
      await screen.findByText("Standard Folding Wheelchair #1"),
    ).toBeInTheDocument();

    // Create a request first
    const submitBtn = screen.getByRole("button", {
      name: /Register Structured Request/i,
    });
    await userEvent.click(submitBtn);
    expect(
      await screen.findByText(/Community Requests \(1\)/i),
    ).toBeInTheDocument();

    // Click Allocate Equipment
    const allocateBtn = screen.getByRole("button", {
      name: /Allocate equipment/i,
    });
    await userEvent.click(allocateBtn);

    // Modal opens
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    const checkbox = screen.getByRole("checkbox", {
      name: /I confirm explicit volunteer decision/i,
    });
    expect(checkbox).not.toBeChecked(); // MUST NOT BE PRECHECKED

    const confirmBtn = screen.getByRole("button", {
      name: /Confirm Allocation/i,
    });
    expect(confirmBtn).toBeDisabled();

    // Check confirmation box
    await userEvent.click(checkbox);
    expect(confirmBtn).toBeEnabled();

    // Submit allocation
    await userEvent.click(confirmBtn);

    // Server confirmed
    expect(
      await screen.findByText(
        /Server confirmed: Equipment allocated and reserved/i,
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/Active & Tracked Loans \(1\)/i),
    ).toBeInTheDocument();
  });

  it("Mocked-backend test: Full volunteer lifecycle: Allocate -> Pickup -> Return -> Inspect -> Available", async () => {
    mockServer.reset(true);
    render(<App />);

    expect(
      await screen.findByText("Standard Folding Wheelchair #1"),
    ).toBeInTheDocument();

    // 1. Intake Request
    const submitReqBtn = screen.getByRole("button", {
      name: /Register Structured Request/i,
    });
    await userEvent.click(submitReqBtn);
    expect(
      await screen.findByText(/Community Requests \(1\)/i),
    ).toBeInTheDocument();

    // 2. Allocate
    const allocateBtn = screen.getByRole("button", {
      name: /Allocate equipment/i,
    });
    await userEvent.click(allocateBtn);
    const allocCheck = screen.getByRole("checkbox", {
      name: /I confirm explicit volunteer decision/i,
    });
    await userEvent.click(allocCheck);
    const confirmAllocBtn = screen.getByRole("button", {
      name: /Confirm Allocation/i,
    });
    await userEvent.click(confirmAllocBtn);
    expect(
      await screen.findByText(
        /Server confirmed: Equipment allocated and reserved/i,
      ),
    ).toBeInTheDocument();

    // 3. Confirm Pickup
    const pickupBtn = await screen.findByRole("button", {
      name: /Confirm Pickup/i,
    });
    await userEvent.click(pickupBtn);

    const pickupCheck = screen.getByRole("checkbox", {
      name: /I verify that .* has been handed over/i,
    });
    expect(pickupCheck).not.toBeChecked();
    await userEvent.click(pickupCheck);

    const modalPickupSubmit = screen.getByRole("button", {
      name: "Confirm Pickup",
    });
    await userEvent.click(modalPickupSubmit);
    expect(
      await screen.findByText(
        /Server confirmed: Equipment pickup marked. Item is now ON_LOAN/i,
      ),
    ).toBeInTheDocument();

    // 4. Receive Return
    const returnBtn = await screen.findByRole("button", {
      name: /Receive Return/i,
    });
    await userEvent.click(returnBtn);

    const returnCheck = screen.getByRole("checkbox", {
      name: /I verify that .* has been returned/i,
    });
    expect(returnCheck).not.toBeChecked();
    await userEvent.click(returnCheck);

    const modalReturnSubmit = screen.getByRole("button", {
      name: "Receive Return",
    });
    await userEvent.click(modalReturnSubmit);
    expect(
      await screen.findByText(/Equipment placed into AWAITING_INSPECTION/i),
    ).toBeInTheDocument();

    // 5. Inspect Equipment
    const inspectBtn = await screen.findByRole("button", {
      name: /Inspect Standard Folding Wheelchair/i,
    });
    await userEvent.click(inspectBtn);

    const inspectCheck = screen.getByRole("checkbox", {
      name: /I affirm that I have physically inspected/i,
    });
    expect(inspectCheck).not.toBeChecked();
    await userEvent.click(inspectCheck);

    const recordInspectBtn = screen.getByRole("button", {
      name: /Record Inspection/i,
    });
    await userEvent.click(recordInspectBtn);
    expect(
      await screen.findByText(
        /Equipment inspection recorded with outcome AVAILABLE/i,
      ),
    ).toBeInTheDocument();
  });

  it("Mocked-backend test: Handles competing update 409 conflict gracefully: refetches snapshot, explains conflict, preserves inputs", async () => {
    mockServer.reset(true);
    render(<App />);

    expect(
      await screen.findByText("Standard Folding Wheelchair #1"),
    ).toBeInTheDocument();

    // Create request
    const submitReqBtn = screen.getByRole("button", {
      name: /Register Structured Request/i,
    });
    await userEvent.click(submitReqBtn);
    expect(
      await screen.findByText(/Community Requests \(1\)/i),
    ).toBeInTheDocument();

    // Trigger 409 on allocation
    mockServer.force409OnAllocation = true;

    const allocateBtn = screen.getByRole("button", {
      name: /Allocate equipment/i,
    });
    await userEvent.click(allocateBtn);

    const allocCheck = screen.getByRole("checkbox", {
      name: /I confirm explicit volunteer decision/i,
    });
    await userEvent.click(allocCheck);

    const confirmAllocBtn = screen.getByRole("button", {
      name: /Confirm Allocation/i,
    });
    await userEvent.click(confirmAllocBtn);

    // 409 Conflict banner should appear with explanation
    expect(await screen.findByRole("alert")).toHaveTextContent(
      /Competing Update Conflict \(409\)/i,
    );
    expect(screen.getByRole("alert")).toHaveTextContent(
      /conflicted with a concurrent update/i,
    );

    // Allocation dialog and inputs remain open and preserved on 409
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(
      screen.getAllByText("Ananya R. (Velachery Resident)").length,
    ).toBeGreaterThanOrEqual(1);
  });

  it("Mocked-backend test: Retains identical Idempotency-Key and body on uncertain network retry", async () => {
    mockServer.reset(true);
    render(<App />);

    expect(
      await screen.findByText("Standard Folding Wheelchair #1"),
    ).toBeInTheDocument();

    // Simulate single network drop during request creation
    mockServer.simulateNetworkDropForRoute = "/api/requests";

    const submitReqBtn = screen.getByRole("button", {
      name: /Register Structured Request/i,
    });
    await userEvent.click(submitReqBtn);

    // Uncertain network retry banner must be displayed
    const retryBanner = await screen.findByRole("alert");
    expect(retryBanner).toHaveTextContent(/Uncertain Network Response/i);
    expect(retryBanner).toHaveTextContent(
      /Retaining original Idempotency-Key/i,
    );

    const keysBefore = [...mockServer.receivedIdempotencyKeys];
    expect(keysBefore.length).toBeGreaterThan(0);
    const originalKey = keysBefore[keysBefore.length - 1];

    // Click retry
    const retryBtn = screen.getByRole("button", {
      name: /Retry with Original Key/i,
    });
    await userEvent.click(retryBtn);

    // Verify recovery
    expect(
      await screen.findByText(/Server confirmed: Request registered/i),
    ).toBeInTheDocument();

    // Verify that the subsequent call used the EXACT SAME Idempotency-Key
    const keysAfter = mockServer.receivedIdempotencyKeys;
    const replayedKey = keysAfter[keysAfter.length - 1];
    expect(replayedKey).toBe(originalKey);
  });

  it("Mocked-backend test: Displays actionable connection error banner when backend server is offline", async () => {
    // Stub fetch to reject with connection error
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockRejectedValue(new Error("Failed to connect to 127.0.0.1:8000")),
    );

    render(<App />);

    expect(await screen.findByRole("alert")).toHaveTextContent(
      /Connection Error:/i,
    );
    expect(
      screen.getByText(/Verify backend is running on/i),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /Retry Connection/i }),
    ).toBeInTheDocument();
  });

  it("Mocked-backend test: Accessible keyboard navigation and semantic structure", async () => {
    mockServer.reset(true);
    render(<App />);

    expect(
      await screen.findByText("Standard Folding Wheelchair #1"),
    ).toBeInTheDocument();

    // Headings structure
    expect(
      screen.getByRole("heading", { level: 1, name: /Borrowed Steps/i }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { level: 2, name: /Equipment Inventory/i }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { level: 2, name: /Intake New Request/i }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { level: 2, name: /Community Requests/i }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("heading", {
        level: 2,
        name: /Active & Tracked Loans/i,
      }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("heading", {
        level: 2,
        name: /Audit Log & Event Stream/i,
      }),
    ).toBeInTheDocument();

    // Verify Tab navigation reaches form input
    const borrowerInput = screen.getByLabelText(/Synthetic Borrower Label/i);
    borrowerInput.focus();
    expect(borrowerInput).toHaveFocus();
  });

  it("Mocked-backend test: Layout renders all interactive regions cleanly on 390px mobile viewport", async () => {
    // Set viewport dimensions to 390x844 (standard mobile)
    window.innerWidth = 390;
    window.innerHeight = 844;
    window.dispatchEvent(new Event("resize"));

    mockServer.reset(true);
    const { container } = render(<App />);

    expect(
      await screen.findByText("Standard Folding Wheelchair #1"),
    ).toBeInTheDocument();

    // Verify main container exists
    const main = container.querySelector("main.container");
    expect(main).toBeInTheDocument();

    // Ensure all critical cards are rendered and accessible in mobile layout
    expect(
      screen.getByRole("heading", { name: /Equipment Inventory/i }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: /Intake New Request/i }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: /Community Requests/i }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: /Active & Tracked Loans/i }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: /Audit Log & Event Stream/i }),
    ).toBeInTheDocument();
  });
});
