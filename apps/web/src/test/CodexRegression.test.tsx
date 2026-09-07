import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import App from "../App";
import { MockBackendServer } from "./mocks/mockBackend";

describe("Codex Review Regressions (BS-002-R1)", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("review: intake blocks duplicate submission while first request is pending", async () => {
    const server = new MockBackendServer();
    server.reset(true);
    let posts = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
        const urlStr =
          typeof input === "string"
            ? input
            : input instanceof URL
              ? input.toString()
              : input.url;
        const url = new URL(urlStr, "http://localhost");
        if (
          url.pathname === "/api/requests" &&
          (init?.method || "GET").toUpperCase() === "POST"
        ) {
          posts++;
          return new Promise<Response>(() => {});
        }
        return server.handleFetch(input, init);
      }),
    );

    render(<App />);
    const submit = await screen.findByRole("button", {
      name: /Register Structured Request/i,
    });
    await userEvent.dblClick(submit);
    expect(posts).toBe(1);
  });

  it("review: allocation dialog and selected input survive 409", async () => {
    const server = new MockBackendServer();
    server.reset(true);
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL, init?: RequestInit) =>
        server.handleFetch(input, init),
      ),
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
    server.force409OnAllocation = true;
    await userEvent.click(screen.getByRole("checkbox"));
    await userEvent.click(
      screen.getByRole("button", { name: /Confirm Allocation/i }),
    );
    expect(await screen.findByRole("alert")).toHaveTextContent(/409/);
    expect(screen.queryByRole("dialog")).not.toBeNull();
  });

  it("review: changing inspection outcome requires new affirmation", async () => {
    const server = new MockBackendServer();
    server.reset(true);
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL, init?: RequestInit) =>
        server.handleFetch(input, init),
      ),
    );

    render(<App />);
    await userEvent.click(
      await screen.findByRole("button", {
        name: /Inspect Adult Axillary Crutches/i,
      }),
    );
    const approval = screen.getByRole("checkbox");
    await userEvent.click(approval);
    fireEvent.change(screen.getByLabelText(/Inspection Determination/i), {
      target: { value: "QUARANTINED" },
    });
    expect(approval).not.toBeChecked();
  });
});
