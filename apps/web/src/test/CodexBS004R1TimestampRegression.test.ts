import { it, expect, vi, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import {
  validateIntakeResponse,
  isValidIsoTimestamp,
} from "../utils/validation";
import { IntakeAssistant } from "../components/IntakeAssistant";
import { api } from "../api/client";
import type { IntakeInterpretResponse } from "../types/api";

function result(value: string | null) {
  return {
    draft: {
      borrower_label: "Synthetic Borrower",
      equipment_kind: "WHEELCHAIR" as const,
      pickup_location: "Community Center Desk",
      due_at: value,
    },
    missing_fields: value === null ? (["due_at"] as ["due_at"]) : [],
    provenance: {
      framework: "strands" as const,
      provider: "ollama" as const,
      model: "llama3.2:3b" as const,
      inventory_tool_calls: 1,
      completed_at: "2026-09-08T00:00:00Z",
    },
  };
}

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

// 1. Original 5 regressions from review
it.each(["2026-09-20", "2026-09-20T10:00:37", "2026-02-30T10:00:00Z"])(
  "rejects incomplete or impossible due_at %s",
  (value) => {
    expect(() => validateIntakeResponse(result(value))).toThrow();
  },
);

it.each(["2026-09-08", "2026-09-08T10:00:37"])(
  "rejects provenance without full UTC timestamp %s",
  (value) => {
    const data = result("2026-09-20T10:00:37Z");
    data.provenance.completed_at = value;
    expect(() => validateIntakeResponse(data)).toThrow();
  },
);

// 2. Positive acceptance cases: valid ISO timestamps
it.each([
  "2026-09-20T10:00:37Z", // whole-second UTC per contract
  "2026-09-20T10:00:00.000Z", // zero-fraction ISO fixture
  "2024-02-29T12:00:00Z", // leap year Feb 29
  "2000-02-29T23:59:59Z", // century leap year Feb 29 (divisible by 400)
  "2026-09-20T15:30:37+05:30", // valid non-UTC timezone offset for due_at
  "2026-09-20T05:00:00-05:00", // valid negative timezone offset for due_at
  "2026-09-20T10:00:37+00:00", // explicit +00:00 offset
])("accepts valid ISO timestamp for due_at: %s", (value) => {
  const data = result(value);
  const validated = validateIntakeResponse(data);
  expect(validated.draft.due_at).toBe(value);
});

it("accepts null due_at with matching missing_fields", () => {
  const data = result(null);
  const validated = validateIntakeResponse(data);
  expect(validated.draft.due_at).toBeNull();
  expect(validated.missing_fields).toEqual(["due_at"]);
});

it.each([
  "2026-09-08T00:00:00Z",
  "2026-09-08T12:34:56.789Z",
  "2026-09-08T00:00:00+00:00",
  "2026-09-08T00:00:00-00:00",
])("accepts valid UTC provenance completed_at: %s", (value) => {
  const data = result("2026-09-20T10:00:37Z");
  data.provenance.completed_at = value;
  const validated = validateIntakeResponse(data);
  expect(validated.provenance.completed_at).toBe(value);
});

// 3. Negative calendar and boundary cases
it.each([
  "2026-02-29T12:00:00Z", // 2026 is not a leap year
  "1900-02-29T12:00:00Z", // 1900 is divisible by 100 but not 400 (not a leap year)
  "2100-02-29T12:00:00Z", // 2100 is not a leap year
  "2026-04-31T10:00:00Z", // April has 30 days
  "2026-06-31T10:00:00Z", // June has 30 days
  "2026-09-31T10:00:00Z", // September has 30 days
  "2026-11-31T10:00:00Z", // November has 30 days
  "2026-00-10T10:00:00Z", // Month 0 is invalid
  "2026-13-10T10:00:00Z", // Month 13 is invalid
  "2026-01-00T10:00:00Z", // Day 0 is invalid
  "2026-01-32T10:00:00Z", // Day 32 is invalid
  "2026-09-20T24:00:00Z", // Hour 24 is invalid
  "2026-09-20T10:60:00Z", // Minute 60 is invalid
  "2026-09-20T10:00:60Z", // Second 60 is invalid
  "2026-09-20T10:00:00+15:00", // Offset hour 15 is out of bounds
  "2026-09-20T10:00:00+05:60", // Offset minute 60 is invalid
  "tomorrow",
  "not-a-date",
  "",
])("rejects invalid calendar date or malformed timestamp %s", (value) => {
  expect(() => validateIntakeResponse(result(value))).toThrow();
  expect(isValidIsoTimestamp(value)).toBe(false);
});

it.each(["2026-09-08T10:00:00+05:30", "2026-09-08T05:00:00-05:00"])(
  "rejects non-UTC timezone offset for completed_at %s",
  (value) => {
    const data = result("2026-09-20T10:00:37Z");
    data.provenance.completed_at = value;
    expect(() => validateIntakeResponse(data)).toThrow();
    expect(isValidIsoTimestamp(value, true)).toBe(false);
  },
);

import React from "react";

// 4. UI integration tests: IntakeAssistant handles malformed timestamps gracefully
it("UI preserves source text and shows alert when API returns impossible calendar due_at", async () => {
  const malformed = result("2026-02-30T10:00:00Z");
  vi.spyOn(api, "interpretIntake").mockResolvedValue(
    malformed as unknown as IntakeInterpretResponse,
  );

  render(
    React.createElement(IntakeAssistant, {
      onApplyDraft: vi.fn(),
      workspaceId: "test-ws",
    }),
  );

  const input = screen.getByLabelText(/Synthetic Intake Notes/);
  fireEvent.change(input, {
    target: { value: "Borrower needs wheelchair by Feb 30" },
  });

  fireEvent.click(screen.getByRole("button", { name: "Interpret Intake" }));

  await waitFor(() => {
    expect(
      screen.queryByRole("button", { name: "Interpreting with Assistant..." }),
    ).not.toBeInTheDocument();
  });

  // Alert is displayed with invalid output message
  const alert = screen.getByRole("alert");
  expect(alert).toHaveTextContent(/Invalid model response/i);

  // Source text is preserved in the textarea
  expect(screen.getByLabelText(/Synthetic Intake Notes/)).toHaveValue(
    "Borrower needs wheelchair by Feb 30",
  );

  // No draft card, Use Draft button, or provenance is rendered
  expect(
    screen.queryByRole("heading", { name: "Unsaved Intake Suggestion" }),
  ).not.toBeInTheDocument();
  expect(
    screen.queryByRole("button", { name: /Use Draft/ }),
  ).not.toBeInTheDocument();
  expect(screen.queryByText(/Assistant Provenance/i)).not.toBeInTheDocument();
});

it("UI preserves source text and shows alert when API returns non-UTC completed_at", async () => {
  const malformed = result("2026-09-20T10:00:37Z");
  malformed.provenance.completed_at = "2026-09-08T10:00:00+05:30";
  vi.spyOn(api, "interpretIntake").mockResolvedValue(
    malformed as unknown as IntakeInterpretResponse,
  );

  render(
    React.createElement(IntakeAssistant, {
      onApplyDraft: vi.fn(),
      workspaceId: "test-ws",
    }),
  );

  const input = screen.getByLabelText(/Synthetic Intake Notes/);
  fireEvent.change(input, {
    target: { value: "Alice needs crutches tomorrow" },
  });

  fireEvent.click(screen.getByRole("button", { name: "Interpret Intake" }));

  await waitFor(() => {
    expect(
      screen.queryByRole("button", { name: "Interpreting with Assistant..." }),
    ).not.toBeInTheDocument();
  });

  expect(screen.getByRole("alert")).toHaveTextContent(
    /Invalid model response/i,
  );
  expect(screen.getByLabelText(/Synthetic Intake Notes/)).toHaveValue(
    "Alice needs crutches tomorrow",
  );
  expect(
    screen.queryByRole("button", { name: /Use Draft/ }),
  ).not.toBeInTheDocument();
});
