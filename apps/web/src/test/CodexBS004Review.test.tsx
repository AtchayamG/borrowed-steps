import { it, expect, vi, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { IntakeAssistant } from "../components/IntakeAssistant";
import { api } from "../api/client";
import { isoToLocalInput, localInputToIso } from "../utils/dateTime";
import App from "../App";
import { MockBackendServer } from "./mocks/mockBackend";
import type { IntakeInterpretResponse } from "../types/api";

const valid = () => ({
  draft: {
    borrower_label: "Synthetic A",
    equipment_kind: "WHEELCHAIR" as const,
    pickup_location: "Room A",
    due_at: "2026-09-20T10:00:37Z",
  },
  missing_fields: [],
  provenance: {
    framework: "strands" as const,
    provider: "ollama" as const,
    model: "llama3.2:3b" as const,
    inventory_tool_calls: 1,
    completed_at: "2026-09-08T00:00:00Z",
  },
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

async function interpret() {
  fireEvent.click(screen.getByRole("button", { name: "Interpret Intake" }));
  await screen.findByRole("heading", { name: "Unsaved Intake Suggestion" });
}

it("preserves nonzero seconds from valid server draft", () => {
  const source = "2026-09-20T10:00:37Z";
  expect(new Date(localInputToIso(isoToLocalInput(source))).getTime()).toBe(
    new Date(source).getTime(),
  );
});

it("rejects wrong provider and out-of-contract tool count before displaying validated provenance", async () => {
  const response = valid();
  Object.assign(response.provenance, {
    provider: "fake",
    inventory_tool_calls: 99,
  });
  vi.spyOn(api, "interpretIntake").mockResolvedValue(
    response as unknown as IntakeInterpretResponse,
  );
  render(<IntakeAssistant onApplyDraft={vi.fn()} workspaceId="A" />);
  fireEvent.click(screen.getByRole("button", { name: "Interpret Intake" }));
  await waitFor(() =>
    expect(
      screen.queryByRole("button", { name: "Interpreting with Assistant..." }),
    ).not.toBeInTheDocument(),
  );
  expect(
    screen.queryByText("Assistant Provenance (Validated Call)"),
  ).not.toBeInTheDocument();
  expect(screen.getByRole("alert")).toHaveTextContent(/invalid/i);
});

it("invalidates previous workspace suggestion", async () => {
  vi.spyOn(api, "interpretIntake").mockResolvedValue(valid());
  const apply = vi.fn();
  const view = render(<IntakeAssistant onApplyDraft={apply} workspaceId="A" />);
  await interpret();
  view.rerender(<IntakeAssistant onApplyDraft={apply} workspaceId="B" />);
  expect(
    screen.queryByRole("button", { name: /Use Draft/ }),
  ).not.toBeInTheDocument();
});

it("invalidates suggestion when source text changes", async () => {
  vi.spyOn(api, "interpretIntake").mockResolvedValue(valid());
  render(<IntakeAssistant onApplyDraft={vi.fn()} workspaceId="A" />);
  await interpret();
  fireEvent.change(screen.getByLabelText(/Synthetic Intake Notes/), {
    target: { value: "Different borrower needs a walker" },
  });
  expect(
    screen.queryByRole("button", { name: /Use Draft/ }),
  ).not.toBeInTheDocument();
});

it("App clears old suggestion after explicit workspace recreation following intake401", async () => {
  const server = new MockBackendServer();
  server.reset(true);
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL, init?: RequestInit) =>
      server.handleFetch(input, init),
    ),
  );
  render(<App />);
  await screen.findByText(/Standard Folding Wheelchair #1/);
  await interpret();
  server.forceAssistantError = {
    status: 401,
    code: "SESSION_REQUIRED",
    message: "expired",
  };
  fireEvent.click(screen.getByRole("button", { name: "Interpret Intake" }));
  await screen.findByRole("button", { name: "Start Synthetic Workspace" });
  fireEvent.click(
    screen.getByRole("button", { name: "Start Synthetic Workspace" }),
  );
  await screen.findByText(
    "Synthetic workspace initialized with seed community inventory.",
  );
  expect(
    screen.queryByRole("button", { name: /Use Draft/ }),
  ).not.toBeInTheDocument();
});
