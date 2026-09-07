import { it, expect, vi, afterEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import App from "../App";
import { MockBackendServer } from "./mocks/mockBackend";

afterEach(() => vi.unstubAllGlobals());

it("review R1: unreadable 200 mutation body preserves safe retry", async () => {
  const server = new MockBackendServer();
  server.reset(true);
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const response = await server.handleFetch(input, init);
      return input === "/api/requests"
        ? new Response("{", { status: 200 })
        : response;
    }),
  );
  render(<App />);
  await userEvent.click(
    await screen.findByRole("button", { name: /Register Structured Request/i }),
  );
  await screen.findByRole("alert");
  expect(server.getSnapshotData().requests).toHaveLength(1);
  expect(
    screen.queryByRole("button", { name: /Retry with Original Key/i }),
  ).not.toBeNull();
});

it("review R1: dismissing uncertainty cannot turn the same request into a duplicate", async () => {
  const server = new MockBackendServer();
  server.reset(true);
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL, init?: RequestInit) =>
      server.handleFetch(input, init),
    ),
  );
  render(<App />);
  const submit = await screen.findByRole("button", {
    name: /Register Structured Request/i,
  });
  server.simulateResponseDropAfterCommit = true;
  await userEvent.click(submit);

  // Assert unsafe dismiss escape hatch is removed
  expect(screen.queryByRole("button", { name: /Dismiss Retry/i })).toBeNull();
  expect(
    screen.getByRole("button", { name: /Retry with Original Key/i }),
  ).toBeDefined();

  // Attempting to submit another request while unresolved retry is pending is blocked
  await userEvent.click(submit);
  expect(server.getSnapshotData().requests).toHaveLength(1);

  // Resolving via retry with original key successfully reconciles without duplicate
  server.simulateResponseDropAfterCommit = false;
  await userEvent.click(
    screen.getByRole("button", { name: /Retry with Original Key/i }),
  );
  expect(server.getSnapshotData().requests).toHaveLength(1);
});

it("review R1: conflict refresh updates inspection version and clears prior approval", async () => {
  const server = new MockBackendServer();
  server.reset(true);
  let conflict = false;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).endsWith("/inspection")) {
        conflict = true;
        return new Response(
          JSON.stringify({
            error: { code: "STATE_CONFLICT", message: "Item changed" },
          }),
          { status: 409 },
        );
      }
      if (input === "/api/snapshot" && conflict) {
        const snap = server.getSnapshotData();
        snap.equipment.find((x) => x.id === "eq-crutches-01")!.version = 2;
        return new Response(JSON.stringify(snap), { status: 200 });
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
  const approval = screen.getByRole("checkbox");
  await userEvent.click(approval);
  await userEvent.click(
    screen.getByRole("button", { name: /Record Inspection/i }),
  );
  await screen.findByText(/conflicted with a concurrent update/i);
  expect(approval).not.toBeChecked();
});
