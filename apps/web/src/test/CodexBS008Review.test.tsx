import { afterEach, expect, it, vi } from "vitest";
import { render, screen, within, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import App from "../App";
import { CoordinationSection } from "../components/CoordinationSection";
import { validateSnapshot } from "../utils/validation";
import { MockBackendServer, createInitialSnapshot } from "./mocks/mockBackend";
import type { CoordinationTask } from "../types/api";

afterEach(() => vi.unstubAllGlobals());

const task: CoordinationTask = {
  id: "review-task",
  loan_id: "review-loan",
  kind: "RETURN_DUE",
  status: "PENDING",
  due_at: "2020-01-01T00:00:00Z",
  created_at: "2020-01-01T00:00:00Z",
};

it("rejects unsupported agent modes rather than claiming a typed mode", () => {
  expect(() =>
    validateSnapshot({ ...createInitialSnapshot(), agent_mode: "invented" }),
  ).toThrow();
});

it("uses only server status and does not claim why a task was resolved", () => {
  const view = render(
    <CoordinationSection
      tasks={[task]}
      loans={[]}
      requests={[]}
      equipment={[]}
    />,
  );
  expect(screen.getByText("PENDING")).toBeInTheDocument();
  expect(screen.getByText("Return reminder")).toBeInTheDocument();
  view.rerender(
    <CoordinationSection
      tasks={[{ ...task, status: "RESOLVED" }]}
      loans={[]}
      requests={[]}
      equipment={[]}
    />,
  );
  expect(screen.getByText(/Return task resolved/)).toBeInTheDocument();
  expect(
    screen.queryByText(/verified volunteer lifecycle action/i),
  ).not.toBeInTheDocument();
});

it("keeps confirmed tasks and sync time on failure, then adopts server state on retry", async () => {
  const backend = new MockBackendServer();
  backend.reset(true);
  backend.setTasks([task]);
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL, init?: RequestInit) =>
      backend.handleFetch(input, init),
    ),
  );
  render(<App />);
  const row = await screen.findByTestId("task-row-review-task");
  expect(within(row).getByText("PENDING")).toBeInTheDocument();
  const synced = screen.getByTestId("sync-freshness").textContent;

  backend.setTasks([{ ...task, status: "DUE" }]);
  backend.simulateNetworkDropForRoute = "/api/snapshot";
  await userEvent.click(screen.getByRole("button", { name: /Sync Snapshot/i }));
  expect(await screen.findByRole("alert")).toBeInTheDocument();
  expect(within(row).getByText("PENDING")).toBeInTheDocument();
  expect(screen.getByTestId("sync-freshness").textContent).toBe(synced);

  await userEvent.click(
    screen.getByRole("button", { name: /Retry Connection/i }),
  );
  await waitFor(() =>
    expect(
      within(screen.getByTestId("task-row-review-task")).getByText("DUE"),
    ).toBeInTheDocument(),
  );
});
