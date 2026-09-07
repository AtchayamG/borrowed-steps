import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import App from "../App";
import { MockBackendServer } from "./mocks/mockBackend";
import type { CreateRequestBody, CreateReservationBody } from "../types/api";

describe("BS-002-R1 Extended Correction Verification", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("verifies exact request body, headers and idempotency key format on intake and allocation", async () => {
    const server = new MockBackendServer();
    server.reset(true);
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL, init?: RequestInit) =>
        server.handleFetch(input, init),
      ),
    );

    render(<App />);

    expect(
      await screen.findByText("Standard Folding Wheelchair #1"),
    ).toBeInTheDocument();

    // 1. Submit structured intake request
    const borrowerInput = screen.getByLabelText(/Synthetic Borrower Label/i);
    await userEvent.clear(borrowerInput);
    await userEvent.type(borrowerInput, "S. Sundaram (Velachery)");

    const submitBtn = screen.getByRole("button", {
      name: /Register Structured Request/i,
    });
    await userEvent.click(submitBtn);

    expect(
      await screen.findByText(
        /Server confirmed: Request registered for S. Sundaram/i,
      ),
    ).toBeInTheDocument();

    // Assert exact intake request body and headers
    const intakeBody = server.lastRequestBody as CreateRequestBody;
    expect(intakeBody).toBeDefined();
    expect(intakeBody.borrower_label).toBe("S. Sundaram (Velachery)");
    expect(intakeBody.equipment_kind).toBe("WHEELCHAIR");
    expect(intakeBody.pickup_location).toBe(
      "Velachery Community Room, 12 Cross Road, Velachery, Chennai",
    );
    expect(new Date(intakeBody.due_at).getTime()).toBeGreaterThan(Date.now());

    const lastKey =
      server.receivedIdempotencyKeys[server.receivedIdempotencyKeys.length - 1];
    expect(lastKey).toBeDefined();
    expect(lastKey!.length).toBeGreaterThanOrEqual(8);
    expect(lastKey!.length).toBeLessThanOrEqual(100);

    // 2. Allocate equipment
    const allocateBtn = await screen.findByRole("button", {
      name: /Allocate equipment/i,
    });
    await userEvent.click(allocateBtn);

    const checkbox = screen.getByRole("checkbox", {
      name: /I confirm explicit volunteer decision/i,
    });
    await userEvent.click(checkbox);

    const confirmAllocBtn = screen.getByRole("button", {
      name: /Confirm Allocation/i,
    });
    await userEvent.click(confirmAllocBtn);

    expect(
      await screen.findByText(
        /Server confirmed: Equipment allocated and reserved/i,
      ),
    ).toBeInTheDocument();

    // Assert exact allocation request body
    const allocBody = server.lastRequestBody as CreateReservationBody;
    expect(allocBody).toBeDefined();
    expect(allocBody.equipment_id).toBe("eq-wheelchair-01");
    expect(allocBody.expected_equipment_version).toBe(1);
    expect(allocBody.human_approved).toBe(true);
  });

  it("models response loss after commit: idempotent replay succeeds without duplicate creation", async () => {
    const server = new MockBackendServer();
    server.reset(true);
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL, init?: RequestInit) =>
        server.handleFetch(input, init),
      ),
    );

    render(<App />);
    expect(
      await screen.findByText("Standard Folding Wheelchair #1"),
    ).toBeInTheDocument();

    // Configure server: commit on server but drop HTTP connection while returning response
    server.simulateResponseDropAfterCommit = true;

    const submitBtn = screen.getByRole("button", {
      name: /Register Structured Request/i,
    });
    await userEvent.click(submitBtn);

    // Client detects network drop and displays uncertain retry alert
    expect(await screen.findByRole("alert")).toHaveTextContent(
      /Uncertain Network Response/i,
    );

    // Confirm entity was indeed committed on the server during the first attempt
    expect(server.getSnapshotData().requests.length).toBe(1);
    const committedRequestId = server.getSnapshotData().requests[0]!.id;

    // Retry with original key
    const retryBtn = screen.getByRole("button", {
      name: /Retry with Original Key/i,
    });
    await userEvent.click(retryBtn);

    // Success confirmation appears
    expect(
      await screen.findByText(/Server confirmed: Request registered/i),
    ).toBeInTheDocument();

    // Verify NO duplicate request was created (still exactly 1 request with same ID)
    const finalRequests = server.getSnapshotData().requests;
    expect(finalRequests.length).toBe(1);
    expect(finalRequests[0]!.id).toBe(committedRequestId);
  });

  it("prevents second mutation from overwriting an unresolved uncertain retry", async () => {
    const server = new MockBackendServer();
    server.reset(true);
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL, init?: RequestInit) =>
        server.handleFetch(input, init),
      ),
    );

    render(<App />);
    expect(
      await screen.findByText("Standard Folding Wheelchair #1"),
    ).toBeInTheDocument();

    // Cause uncertain failure
    server.simulateNetworkDropForRoute = "/api/requests";

    const submitBtn = screen.getByRole("button", {
      name: /Register Structured Request/i,
    });
    await userEvent.click(submitBtn);

    expect(
      await screen.findByText(/Uncertain Network Response/i),
    ).toBeInTheDocument();

    // Try to register another request while uncertain retry is unresolved
    const borrowerInput = screen.getByLabelText(/Synthetic Borrower Label/i);
    await userEvent.clear(borrowerInput);
    await userEvent.type(borrowerInput, "Second Attempt (Should Block)");

    await userEvent.click(submitBtn);

    // Warning banner should explain that unresolved retry is pending
    expect(
      await screen.findByText(
        /An unresolved uncertain network retry is pending/i,
      ),
    ).toBeInTheDocument();

    // Original uncertain retry banner is still present and not overwritten
    expect(screen.getByText(/Uncertain Network Response/i)).toBeInTheDocument();
  });

  it("session expiry (401) hides dialogs, and workspace creation clears unresolved retry and stale references", async () => {
    const server = new MockBackendServer();
    server.reset(true);
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL, init?: RequestInit) =>
        server.handleFetch(input, init),
      ),
    );

    render(<App />);
    expect(
      await screen.findByText("Standard Folding Wheelchair #1"),
    ).toBeInTheDocument();

    // Cause uncertain retry
    server.simulateNetworkDropForRoute = "/api/requests";
    const submitBtn = screen.getByRole("button", {
      name: /Register Structured Request/i,
    });
    await userEvent.click(submitBtn);
    expect(
      await screen.findByText(/Uncertain Network Response/i),
    ).toBeInTheDocument();

    // Now session expires on server
    server.setSession(false);

    // Volunteer clicks Sync Snapshot, receiving 401
    const syncBtn = screen.getByRole("button", { name: /Sync Snapshot/i });
    await userEvent.click(syncBtn);

    // 401 Expired banner appears
    expect(
      await screen.findByText(/No Active Community Session \(401\)/i),
    ).toBeInTheDocument();

    // Click Start Synthetic Workspace
    const startWsBtn = screen.getByRole("button", {
      name: /Start Synthetic Workspace/i,
    });
    await userEvent.click(startWsBtn);

    // New workspace initialized: uncertain retry from previous session is CLEARED
    expect(
      await screen.findByText("Standard Folding Wheelchair #1"),
    ).toBeInTheDocument();
    expect(screen.queryByText(/Uncertain Network Response/i)).toBeNull();
  });
});
