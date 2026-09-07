import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import App from "../App";
import { MockBackendServer } from "./mocks/mockBackend";

describe("Codex Review Regressions (BS-002-R2 / BS-002-R3)", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("review R2: renders 422 refusal INSIDE allocation dialog without closing", async () => {
    const server = new MockBackendServer();
    server.reset(true);
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        if (String(input) === "/api/reservations") {
          return new Response(
            JSON.stringify({
              error: {
                code: "VALIDATION_ERROR",
                message: "Equipment cannot be reserved under current policy.",
              },
            }),
            { status: 422 },
          );
        }
        return server.handleFetch(input, init);
      }),
    );

    render(<App />);
    await userEvent.click(
      await screen.findByRole("button", {
        name: /Register Structured Request/i,
      }),
    );
    await userEvent.click(
      await screen.findByRole("button", { name: /Allocate equipment/i }),
    );

    const dialog = screen.getByRole("dialog");
    const checkbox = within(dialog).getByRole("checkbox");
    await userEvent.click(checkbox);
    expect(checkbox).toBeChecked();

    await userEvent.click(
      within(dialog).getByRole("button", { name: /Confirm Allocation/i }),
    );

    // Modal must remain open
    expect(screen.getByRole("dialog")).toBeInTheDocument();

    // Error must be displayed INSIDE the active dialog
    const dialogAlert = await within(dialog).findByRole("alert");
    expect(dialogAlert).toHaveTextContent(
      /Equipment cannot be reserved under current policy/i,
    );
    expect(checkbox).toBeChecked();
  });

  it("review R2: renders 422 refusal INSIDE inspection dialog without closing", async () => {
    const server = new MockBackendServer();
    server.reset(true);
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        if (String(input).endsWith("/inspection")) {
          return new Response(
            JSON.stringify({
              error: {
                code: "VALIDATION_ERROR",
                message: "Inspection determination rejected by policy engine.",
              },
            }),
            { status: 422 },
          );
        }
        return server.handleFetch(input, init);
      }),
    );

    render(<App />);
    await userEvent.click(
      await screen.findByRole("button", {
        name: /Inspect Adult Axillary Crutches/i,
      }),
    );

    const dialog = screen.getByRole("dialog");
    const checkbox = within(dialog).getByRole("checkbox");
    await userEvent.click(checkbox);
    expect(checkbox).toBeChecked();

    await userEvent.click(
      within(dialog).getByRole("button", { name: /Record Inspection/i }),
    );

    // Modal must remain open
    expect(screen.getByRole("dialog")).toBeInTheDocument();

    // Error must be displayed INSIDE the active dialog
    const dialogAlert = await within(dialog).findByRole("alert");
    expect(dialogAlert).toHaveTextContent(
      /Inspection determination rejected by policy engine/i,
    );
    expect(checkbox).toBeChecked();
  });

  it("review R2: renders 422 refusal INSIDE loan pickup dialog without closing", async () => {
    const server = new MockBackendServer();
    server.reset(true);
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        if (String(input).endsWith("/pickup")) {
          return new Response(
            JSON.stringify({
              error: {
                code: "VALIDATION_ERROR",
                message: "Volunteer pickup handover validation failed.",
              },
            }),
            { status: 422 },
          );
        }
        return server.handleFetch(input, init);
      }),
    );

    render(<App />);
    // Allocate to create a loan
    await userEvent.click(
      await screen.findByRole("button", {
        name: /Register Structured Request/i,
      }),
    );
    await userEvent.click(
      await screen.findByRole("button", { name: /Allocate equipment/i }),
    );
    await userEvent.click(screen.getByRole("checkbox"));
    await userEvent.click(
      screen.getByRole("button", { name: /Confirm Allocation/i }),
    );

    // Open pickup dialog
    await userEvent.click(
      await screen.findByRole("button", { name: /Confirm Pickup/i }),
    );
    const dialog = screen.getByRole("dialog");
    const checkbox = within(dialog).getByRole("checkbox");
    await userEvent.click(checkbox);

    // Submit pickup
    await userEvent.click(
      within(dialog).getByRole("button", { name: "Confirm Pickup" }),
    );

    // Modal must remain open
    expect(screen.getByRole("dialog")).toBeInTheDocument();

    // Error must be displayed INSIDE the active dialog
    const dialogAlert = await within(dialog).findByRole("alert");
    expect(dialogAlert).toHaveTextContent(
      /Volunteer pickup handover validation failed/i,
    );
    expect(checkbox).toBeChecked();
  });

  it("review R2: distinguishes structured 500 server error from connection failures", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        if (input === "/api/snapshot") {
          return new Response(
            JSON.stringify({
              error: {
                code: "INTERNAL_ERROR",
                message: "Database connection pool exhausted.",
              },
            }),
            { status: 500, headers: { "Content-Type": "application/json" } },
          );
        }
        return new Response(JSON.stringify({ status: "ok" }), { status: 200 });
      }),
    );

    render(<App />);

    // Should render Server Error banner, NOT Connection Error banner
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(
      /Server Error \(INTERNAL_ERROR\): Database connection pool exhausted/i,
    );
    expect(alert).not.toHaveTextContent(/Connection Error/i);
    expect(alert).not.toHaveTextContent(
      /Backend unavailable or proxy connection failed/i,
    );
    expect(
      within(alert).getByRole("button", {
        name: /Retry Snapshot|Sync Snapshot/i,
      }),
    ).toBeInTheDocument();
  });
});
