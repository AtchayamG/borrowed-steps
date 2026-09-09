import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import App from "../App";
import { CoordinationSection } from "../components/CoordinationSection";
import { validateSnapshot } from "../utils/validation";
import { ApiClientError } from "../api/client";
import { MockBackendServer } from "./mocks/mockBackend";
import type {
  CoordinationTask,
  Loan,
  BorrowRequest,
  Equipment,
} from "../types/api";

describe("M2B Coordination Section & Validation Tests", () => {
  describe("Snapshot & CoordinationTask Validation (validateSnapshot)", () => {
    const baseValidSnapshot = {
      equipment: [],
      requests: [],
      loans: [],
      events: [],
      tasks: [],
      agent_mode: "not_implemented",
    };

    it("accepts valid snapshot with empty tasks array", () => {
      const validated = validateSnapshot(baseValidSnapshot);
      expect(validated.tasks).toEqual([]);
    });

    it("rejects snapshot when tasks property is missing entirely", () => {
      const withoutTasks = { ...baseValidSnapshot } as Record<string, unknown>;
      delete withoutTasks.tasks;
      expect(() => validateSnapshot(withoutTasks)).toThrowError(ApiClientError);
      try {
        validateSnapshot(withoutTasks);
      } catch (err) {
        expect((err as ApiClientError).code).toBe("SNAPSHOT_INVALID_OUTPUT");
        expect((err as ApiClientError).message).toContain("tasks");
      }
    });

    it("rejects snapshot when tasks property is null or not an array", () => {
      expect(() =>
        validateSnapshot({ ...baseValidSnapshot, tasks: null }),
      ).toThrowError(ApiClientError);
      expect(() =>
        validateSnapshot({ ...baseValidSnapshot, tasks: "not-an-array" }),
      ).toThrowError(ApiClientError);
    });

    it("accepts valid tasks with all required fields and UTC ISO timestamps", () => {
      const validTask: CoordinationTask = {
        id: "task-01",
        loan_id: "loan-01",
        kind: "PICKUP_DUE",
        status: "PENDING",
        due_at: "2026-09-09T10:00:00.000Z",
        created_at: "2026-09-09T10:00:00.000Z",
      };
      const validated = validateSnapshot({
        ...baseValidSnapshot,
        tasks: [validTask],
      });
      expect(validated.tasks).toHaveLength(1);
      expect(validated.tasks[0]!.id).toBe("task-01");
      expect(validated.tasks[0]!.due_at).toBe("2026-09-09T10:00:00.000Z");
    });

    it("rejects tasks missing required properties", () => {
      const missingLoanId = {
        id: "task-01",
        kind: "PICKUP_DUE",
        status: "PENDING",
        due_at: "2026-09-09T10:00:00Z",
        created_at: "2026-09-09T10:00:00Z",
      };
      expect(() =>
        validateSnapshot({ ...baseValidSnapshot, tasks: [missingLoanId] }),
      ).toThrowError(ApiClientError);

      const missingKind = {
        id: "task-01",
        loan_id: "loan-01",
        status: "PENDING",
        due_at: "2026-09-09T10:00:00Z",
        created_at: "2026-09-09T10:00:00Z",
      };
      expect(() =>
        validateSnapshot({ ...baseValidSnapshot, tasks: [missingKind] }),
      ).toThrowError(ApiClientError);

      const missingStatus = {
        id: "task-01",
        loan_id: "loan-01",
        kind: "PICKUP_DUE",
        due_at: "2026-09-09T10:00:00Z",
        created_at: "2026-09-09T10:00:00Z",
      };
      expect(() =>
        validateSnapshot({ ...baseValidSnapshot, tasks: [missingStatus] }),
      ).toThrowError(ApiClientError);
    });

    it("rejects forbidden extra properties such as workspace_id", () => {
      const taskWithForbiddenWorkspaceId = {
        id: "task-01",
        loan_id: "loan-01",
        workspace_id: "ws-forbidden-01",
        kind: "PICKUP_DUE",
        status: "PENDING",
        due_at: "2026-09-09T10:00:00Z",
        created_at: "2026-09-09T10:00:00Z",
      };
      expect(() =>
        validateSnapshot({
          ...baseValidSnapshot,
          tasks: [taskWithForbiddenWorkspaceId],
        }),
      ).toThrowError(ApiClientError);
      try {
        validateSnapshot({
          ...baseValidSnapshot,
          tasks: [taskWithForbiddenWorkspaceId],
        });
      } catch (err) {
        expect((err as ApiClientError).code).toBe("SNAPSHOT_INVALID_OUTPUT");
        expect((err as ApiClientError).message).toContain("'workspace_id'");
      }
    });

    it("rejects invalid task kind or status enum values", () => {
      const badKind = {
        id: "task-01",
        loan_id: "loan-01",
        kind: "DISPATCH_DUE",
        status: "PENDING",
        due_at: "2026-09-09T10:00:00Z",
        created_at: "2026-09-09T10:00:00Z",
      };
      expect(() =>
        validateSnapshot({ ...baseValidSnapshot, tasks: [badKind] }),
      ).toThrowError(ApiClientError);

      const badStatus = {
        id: "task-01",
        loan_id: "loan-01",
        kind: "PICKUP_DUE",
        status: "IN_PROGRESS",
        due_at: "2026-09-09T10:00:00Z",
        created_at: "2026-09-09T10:00:00Z",
      };
      expect(() =>
        validateSnapshot({ ...baseValidSnapshot, tasks: [badStatus] }),
      ).toThrowError(ApiClientError);
    });

    it("rejects invalid timestamps for due_at or created_at", () => {
      const badTimestamp = {
        id: "task-01",
        loan_id: "loan-01",
        kind: "RETURN_DUE",
        status: "DUE",
        due_at: "tomorrow at 5pm",
        created_at: "2026-09-09T10:00:00Z",
      };
      expect(() =>
        validateSnapshot({ ...baseValidSnapshot, tasks: [badTimestamp] }),
      ).toThrowError(ApiClientError);
    });
  });

  describe("CoordinationSection Component Presentation", () => {
    const mockEquipment: Equipment[] = [
      {
        id: "eq-walker-01",
        label: "Aluminum Walker #1",
        kind: "WALKER",
        state: "RESERVED",
        version: 1,
      },
    ];

    const mockRequests: BorrowRequest[] = [
      {
        id: "req-01",
        borrower_label: "R. Sharma",
        equipment_kind: "WALKER",
        pickup_location: "Community Center Room 4",
        due_at: "2026-09-25T14:00:00Z",
        status: "RESERVED",
        created_at: "2026-09-09T09:00:00Z",
      },
    ];

    const mockLoans: Loan[] = [
      {
        id: "loan-01",
        request_id: "req-01",
        equipment_id: "eq-walker-01",
        status: "RESERVED",
        due_at: "2026-09-25T14:00:00Z",
        created_at: "2026-09-09T09:05:00Z",
      },
    ];

    it("renders empty state notice when tasks array is empty", () => {
      render(
        <CoordinationSection
          tasks={[]}
          loans={[]}
          requests={[]}
          equipment={[]}
        />,
      );
      expect(screen.getByTestId("coordination-empty")).toHaveTextContent(
        /No coordination tasks in this workspace/i,
      );
    });

    it("renders active PICKUP_DUE task with location, borrower, equipment, and PENDING badge", () => {
      const tasks: CoordinationTask[] = [
        {
          id: "task-pickup-01",
          loan_id: "loan-01",
          kind: "PICKUP_DUE",
          status: "PENDING",
          due_at: "2026-09-09T09:05:00Z",
          created_at: "2026-09-09T09:05:00Z",
        },
      ];

      render(
        <CoordinationSection
          tasks={tasks}
          loans={mockLoans}
          requests={mockRequests}
          equipment={mockEquipment}
        />,
      );

      // Section title and in-app scope notice
      expect(
        screen.getByRole("heading", { name: "Coordination" }),
      ).toBeInTheDocument();
      expect(screen.getByText(/no external email\/sms/i)).toBeInTheDocument();

      // Active task card
      const taskRow = screen.getByTestId("task-row-task-pickup-01");
      expect(taskRow).toBeInTheDocument();
      expect(within(taskRow).getByText("Arrange pickup")).toBeInTheDocument();
      expect(
        within(taskRow).getByText("Community Center Room 4"),
      ).toBeInTheDocument();
      expect(within(taskRow).getByText("R. Sharma")).toBeInTheDocument();
      expect(
        within(taskRow).getByText(/Aluminum Walker #1/),
      ).toBeInTheDocument();

      // PENDING badge
      const statusBadge = screen.getByTestId("task-status-task-pickup-01");
      expect(statusBadge).toHaveTextContent("PENDING");
      expect(statusBadge).toHaveClass("tag-pending");

      // Guidance explaining immediate volunteer action
      expect(
        within(taskRow).getByText(/Pickup is immediately actionable/i),
      ).toBeInTheDocument();
    });

    it("renders active RETURN_DUE task with server status DUE badge and due instant", () => {
      const tasks: CoordinationTask[] = [
        {
          id: "task-return-01",
          loan_id: "loan-01",
          kind: "RETURN_DUE",
          status: "DUE",
          due_at: "2026-09-25T14:00:00Z",
          created_at: "2026-09-09T09:05:00Z",
        },
      ];

      render(
        <CoordinationSection
          tasks={tasks}
          loans={mockLoans}
          requests={mockRequests}
          equipment={mockEquipment}
        />,
      );

      const taskRow = screen.getByTestId("task-row-task-return-01");
      expect(within(taskRow).getByText("Return due")).toBeInTheDocument();
      expect(within(taskRow).getByText("R. Sharma")).toBeInTheDocument();
      expect(
        within(taskRow).getByText("2026-09-25T14:00:00Z"),
      ).toBeInTheDocument();

      const statusBadge = screen.getByTestId("task-status-task-return-01");
      expect(statusBadge).toHaveTextContent("DUE");
      expect(statusBadge).toHaveClass("tag-due");
    });

    it("strictly follows server status without client-side time comparison (PENDING status even if due_at passed)", () => {
      const tasks: CoordinationTask[] = [
        {
          id: "task-return-02",
          loan_id: "loan-01",
          kind: "RETURN_DUE",
          status: "PENDING",
          due_at: "2020-01-01T00:00:00Z",
          created_at: "2020-01-01T00:00:00Z",
        },
      ];

      render(
        <CoordinationSection
          tasks={tasks}
          loans={mockLoans}
          requests={mockRequests}
          equipment={mockEquipment}
        />,
      );

      const statusBadge = screen.getByTestId("task-status-task-return-02");
      expect(statusBadge).toHaveTextContent("PENDING");
      expect(statusBadge).toHaveClass("tag-pending");
    });

    it("renders resolved tasks in inspectable details group with RESOLVED badge", () => {
      const tasks: CoordinationTask[] = [
        {
          id: "task-resolved-01",
          loan_id: "loan-01",
          kind: "PICKUP_DUE",
          status: "RESOLVED",
          due_at: "2026-09-09T09:05:00Z",
          created_at: "2026-09-09T09:05:00Z",
        },
      ];

      render(
        <CoordinationSection
          tasks={tasks}
          loans={mockLoans}
          requests={mockRequests}
          equipment={mockEquipment}
        />,
      );

      expect(screen.getByTestId("resolved-tasks-details")).toBeInTheDocument();
      const statusBadge = screen.getByTestId("task-status-task-resolved-01");
      expect(statusBadge).toHaveTextContent("RESOLVED");
      expect(statusBadge).toHaveClass("tag-resolved");
    });

    it("has zero dummy buttons inside CoordinationSection", () => {
      const tasks: CoordinationTask[] = [
        {
          id: "task-01",
          loan_id: "loan-01",
          kind: "PICKUP_DUE",
          status: "PENDING",
          due_at: "2026-09-09T09:05:00Z",
          created_at: "2026-09-09T09:05:00Z",
        },
      ];

      const { container } = render(
        <CoordinationSection
          tasks={tasks}
          loans={mockLoans}
          requests={mockRequests}
          equipment={mockEquipment}
        />,
      );

      const buttons = container.querySelectorAll("button");
      expect(buttons).toHaveLength(0);
    });
  });

  describe("Integration & Lifecycle in App", () => {
    let mockServer: MockBackendServer;

    beforeEach(() => {
      mockServer = new MockBackendServer();
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

    it("creates a PICKUP_DUE task when reservation is confirmed and updates Coordination view", async () => {
      mockServer.reset(true);
      render(<App />);

      expect(
        await screen.findByText("Standard Folding Wheelchair #1"),
      ).toBeInTheDocument();

      const borrowerInput = screen.getByLabelText(/Synthetic Borrower Label/i);
      await userEvent.clear(borrowerInput);
      await userEvent.type(borrowerInput, "Priya Nair");

      const submitBtn = screen.getByRole("button", {
        name: /Register Structured Request/i,
      });
      await userEvent.click(submitBtn);

      expect(await screen.findByText("Priya Nair")).toBeInTheDocument();

      const allocateBtn = screen.getByRole("button", {
        name: /Allocate equipment for Priya Nair/i,
      });
      await userEvent.click(allocateBtn);

      const modal = await screen.findByRole("dialog");
      const approvalCheckbox = within(modal).getByRole("checkbox", {
        name: /I confirm explicit volunteer decision/i,
      });
      await userEvent.click(approvalCheckbox);

      const confirmBtn = within(modal).getByRole("button", {
        name: /Confirm Allocation/i,
      });
      await userEvent.click(confirmBtn);

      expect(await screen.findByText("Arrange pickup")).toBeInTheDocument();
      const coordSection = screen
        .getByRole("heading", { name: "Coordination" })
        .closest("section")!;
      expect(
        within(coordSection).getByText(/Velachery Community Room/i),
      ).toBeInTheDocument();
      expect(within(coordSection).getByText("PENDING")).toBeInTheDocument();

      const recordPickupBtn = screen.getByRole("button", {
        name: /Confirm Pickup/i,
      });
      await userEvent.click(recordPickupBtn);

      const pickupModal = await screen.findByRole("dialog");
      const pickupApproval = within(pickupModal).getByRole("checkbox", {
        name: /I verify that/i,
      });
      await userEvent.click(pickupApproval);

      const confirmPickupAction = within(pickupModal).getByRole("button", {
        name: /Confirm Pickup/i,
      });
      await userEvent.click(confirmPickupAction);

      expect(await screen.findByText("Return reminder")).toBeInTheDocument();
    });

    it("displays sync freshness timestamp and updates on manual Sync Snapshot", async () => {
      mockServer.reset(true);
      render(<App />);

      expect(
        await screen.findByText("Standard Folding Wheelchair #1"),
      ).toBeInTheDocument();
      const freshness = await screen.findByTestId("sync-freshness");
      expect(freshness).toHaveTextContent(/Last synced:/i);

      const syncBtn = screen.getByRole("button", { name: /Sync Snapshot/i });
      await userEvent.click(syncBtn);

      expect(screen.getByTestId("sync-freshness")).toHaveTextContent(
        /Last synced:/i,
      );
    });

    it("resets coordination tasks cleanly on session restart without stale task leakage", async () => {
      mockServer.reset(true);
      mockServer.setTasks([
        {
          id: "stale-task-99",
          loan_id: "stale-loan-99",
          kind: "PICKUP_DUE",
          status: "PENDING",
          due_at: "2026-09-09T08:00:00Z",
          created_at: "2026-09-09T08:00:00Z",
        },
      ]);

      render(<App />);
      expect(await screen.findByText("Arrange pickup")).toBeInTheDocument();

      mockServer.setSession(false);
      const syncBtn = screen.getByRole("button", { name: /Sync Snapshot/i });
      await userEvent.click(syncBtn);

      expect(await screen.findByRole("alert")).toHaveTextContent(
        /No Active Community Session \(401\)/i,
      );

      const startBtn = screen.getByRole("button", {
        name: /Start Synthetic Workspace/i,
      });
      await userEvent.click(startBtn);

      expect(
        await screen.findByTestId("coordination-empty"),
      ).toBeInTheDocument();
      expect(screen.queryByText("stale-task-99")).not.toBeInTheDocument();
    });

    it("displays structured error banner and preserves UI when server returns malformed snapshot", async () => {
      mockServer.reset(true);
      vi.stubGlobal(
        "fetch",
        vi.fn().mockImplementation((input: RequestInfo | URL) => {
          const urlStr =
            typeof input === "string"
              ? input
              : input instanceof URL
                ? input.toString()
                : input.url;
          if (urlStr.includes("/api/health")) {
            return Promise.resolve(
              new Response(
                JSON.stringify({
                  status: "ok",
                  milestone: "M2B",
                  agent_mode: "not_implemented",
                }),
                {
                  status: 200,
                  headers: { "Content-Type": "application/json" },
                },
              ),
            );
          }
          return Promise.resolve(
            new Response(
              JSON.stringify({
                equipment: [],
                requests: [],
                loans: [],
                events: [],
                agent_mode: "not_implemented",
                // tasks omitted
              }),
              { status: 200, headers: { "Content-Type": "application/json" } },
            ),
          );
        }),
      );

      render(<App />);

      const errorBanner = await screen.findByRole("alert");
      expect(errorBanner).toHaveTextContent(
        /Server Error \(SNAPSHOT_INVALID_OUTPUT\)/i,
      );
      expect(errorBanner).toHaveTextContent(/required field 'tasks'/i);
    });
  });
});
