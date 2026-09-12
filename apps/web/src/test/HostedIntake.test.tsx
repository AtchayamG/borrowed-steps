import { describe, it, expect, afterEach, vi } from "vitest";
import {
  render,
  screen,
  waitFor,
  fireEvent,
  within,
} from "@testing-library/react";
import { IntakeAssistant } from "../components/IntakeAssistant";
import { validateSnapshot, validateIntakeResponse } from "../utils/validation";
import { api, ApiClientError } from "../api/client";
import type { Snapshot, IntakeInterpretResponse } from "../types/api";

describe("Hosted Intake Validation & UI Tests (BS-028)", () => {
  const baseSnapshot: Snapshot = {
    equipment: [
      {
        id: "eq-1",
        label: "Standard Folding Wheelchair #1",
        kind: "WHEELCHAIR",
        state: "AVAILABLE",
        version: 1,
      },
    ],
    requests: [],
    loans: [],
    events: [],
    agent_mode: "strands_groq",
    tasks: [],
  };

  const validHostedResponse: IntakeInterpretResponse = {
    draft: {
      borrower_label: "Priya S.",
      equipment_kind: "WHEELCHAIR",
      pickup_location: "Velachery Community Room",
      due_at: "2026-09-20T10:00:00Z",
    },
    missing_fields: [],
    provenance: {
      framework: "strands",
      provider: "groq",
      model: "openai/gpt-oss-20b",
      inventory_tool_calls: 1,
      completed_at: "2026-09-12T12:00:00Z",
    },
  };

  afterEach(() => {
    vi.restoreAllMocks();
  });

  describe("validateSnapshot agent_mode acceptance", () => {
    it("accepts agent_mode: 'strands_groq'", () => {
      const validated = validateSnapshot(baseSnapshot);
      expect(validated.agent_mode).toBe("strands_groq");
    });

    it("accepts agent_mode: 'disabled' and 'strands_ollama'", () => {
      expect(
        validateSnapshot({ ...baseSnapshot, agent_mode: "disabled" })
          .agent_mode,
      ).toBe("disabled");
      expect(
        validateSnapshot({ ...baseSnapshot, agent_mode: "strands_ollama" })
          .agent_mode,
      ).toBe("strands_ollama");
    });

    it("rejects unknown agent_mode", () => {
      expect(() =>
        validateSnapshot({ ...baseSnapshot, agent_mode: "agent_core" }),
      ).toThrowError(/Invalid snapshot: missing or invalid 'agent_mode'/);
    });
  });

  describe("validateIntakeResponse provenance tuple acceptance", () => {
    it("accepts documented hosted tuple ('strands', 'groq', 'openai/gpt-oss-20b')", () => {
      const validated = validateIntakeResponse(validHostedResponse);
      expect(validated.provenance.provider).toBe("groq");
      expect(validated.provenance.model).toBe("openai/gpt-oss-20b");
      expect(validated.provenance.framework).toBe("strands");
    });

    it("accepts documented local tuple ('strands', 'ollama', 'llama3.2:3b')", () => {
      const localResponse = {
        ...validHostedResponse,
        provenance: {
          ...validHostedResponse.provenance,
          provider: "ollama",
          model: "llama3.2:3b",
        },
      };
      const validated = validateIntakeResponse(localResponse);
      expect(validated.provenance.provider).toBe("ollama");
      expect(validated.provenance.model).toBe("llama3.2:3b");
    });

    it("rejects mismatched provenance tuple", () => {
      const mismatched = {
        ...validHostedResponse,
        provenance: {
          ...validHostedResponse.provenance,
          provider: "groq",
          model: "llama3.2:3b",
        },
      };
      expect(() => validateIntakeResponse(mismatched)).toThrowError(
        /provenance provider\/model must be/,
      );
    });

    it("rejects undocumented provider", () => {
      const badProvider = {
        ...validHostedResponse,
        provenance: {
          ...validHostedResponse.provenance,
          provider: "anthropic",
          model: "claude-3-5-sonnet",
        },
      };
      expect(() => validateIntakeResponse(badProvider)).toThrowError(
        /provenance provider\/model must be/,
      );
    });
  });

  describe("IntakeAssistant UI Component in strands_groq Mode", () => {
    it("renders Hosted Groq Assistant badge and configured mode", () => {
      render(
        <IntakeAssistant
          onApplyDraft={vi.fn()}
          agentMode="strands_groq"
          workspaceId="ws-test-123"
        />,
      );

      expect(screen.getByText("Hosted Groq Assistant")).toBeInTheDocument();
      expect(screen.getByText(/Configured Mode:/)).toHaveTextContent(
        "strands_groq",
      );
    });

    it("renders grounded suggestion on successful interpretation", async () => {
      vi.spyOn(api, "interpretIntake").mockResolvedValue(validHostedResponse);

      const applyMock = vi.fn();
      render(
        <IntakeAssistant
          onApplyDraft={applyMock}
          agentMode="strands_groq"
          workspaceId="ws-test-123"
        />,
      );

      const interpretBtn = screen.getByRole("button", {
        name: /Interpret Intake/i,
      });
      fireEvent.click(interpretBtn);

      await waitFor(() => {
        expect(
          screen.getByRole("heading", { name: /Unsaved Intake Suggestion/i }),
        ).toBeInTheDocument();
      });

      const box = screen
        .getByRole("heading", { name: /Unsaved Intake Suggestion/i })
        .closest(".intake-suggestion-box") as HTMLElement;

      expect(within(box).getByText("Priya S.")).toBeInTheDocument();
      expect(within(box).getByText("WHEELCHAIR")).toBeInTheDocument();
      expect(
        within(box).getByText("Velachery Community Room"),
      ).toBeInTheDocument();
      expect(within(box).getByText("groq")).toBeInTheDocument();
      expect(within(box).getByText("openai/gpt-oss-20b")).toBeInTheDocument();

      // Click Use Draft
      const useDraftBtn = screen.getByRole("button", { name: /Use Draft/i });
      fireEvent.click(useDraftBtn);
      expect(applyMock).toHaveBeenCalledWith(validHostedResponse.draft);
    });

    it("displays honest disabled state on 503 ASSISTANT_DISABLED", async () => {
      vi.spyOn(api, "interpretIntake").mockRejectedValue(
        new ApiClientError(
          503,
          "ASSISTANT_DISABLED",
          "The intake assistant is not enabled on this server.",
          true,
        ),
      );

      render(
        <IntakeAssistant
          onApplyDraft={vi.fn()}
          agentMode="disabled"
          workspaceId="ws-test-123"
        />,
      );

      fireEvent.click(
        screen.getByRole("button", { name: /Interpret Intake/i }),
      );

      await waitFor(() => {
        const alert = screen.getByRole("alert");
        expect(alert).toHaveTextContent(
          /Assistant Unavailable \(503 ASSISTANT_DISABLED\)/i,
        );
        expect(alert).toHaveTextContent(
          /Manual request entry is available below/i,
        );
      });
    });

    it("displays honest busy state on 429 ASSISTANT_BUSY", async () => {
      vi.spyOn(api, "interpretIntake").mockRejectedValue(
        new ApiClientError(
          429,
          "ASSISTANT_BUSY",
          "Another interpretation is already running. Try again in a moment.",
          true,
        ),
      );

      render(
        <IntakeAssistant
          onApplyDraft={vi.fn()}
          agentMode="strands_groq"
          workspaceId="ws-test-123"
        />,
      );

      fireEvent.click(
        screen.getByRole("button", { name: /Interpret Intake/i }),
      );

      await waitFor(() => {
        const alert = screen.getByRole("alert");
        expect(alert).toHaveTextContent(
          /Assistant Busy \(429 ASSISTANT_BUSY\)/i,
        );
      });
    });

    it("displays honest timeout state on 504 ASSISTANT_TIMEOUT", async () => {
      vi.spyOn(api, "interpretIntake").mockRejectedValue(
        new ApiClientError(
          504,
          "ASSISTANT_TIMEOUT",
          "The interpretation took too long. Please try again or use the form.",
          true,
        ),
      );

      render(
        <IntakeAssistant
          onApplyDraft={vi.fn()}
          agentMode="strands_groq"
          workspaceId="ws-test-123"
        />,
      );

      fireEvent.click(
        screen.getByRole("button", { name: /Interpret Intake/i }),
      );

      await waitFor(() => {
        const alert = screen.getByRole("alert");
        expect(alert).toHaveTextContent(
          /Assistant Timeout \(504 ASSISTANT_TIMEOUT\)/i,
        );
      });
    });

    it("displays honest provider failure on 503 ASSISTANT_UNAVAILABLE", async () => {
      vi.spyOn(api, "interpretIntake").mockRejectedValue(
        new ApiClientError(
          503,
          "ASSISTANT_UNAVAILABLE",
          "The hosted interpretation service is temporarily unavailable.",
          true,
        ),
      );

      render(
        <IntakeAssistant
          onApplyDraft={vi.fn()}
          agentMode="strands_groq"
          workspaceId="ws-test-123"
        />,
      );

      fireEvent.click(
        screen.getByRole("button", { name: /Interpret Intake/i }),
      );

      await waitFor(() => {
        const alert = screen.getByRole("alert");
        expect(alert).toHaveTextContent(
          /Assistant Unavailable \(503 ASSISTANT_UNAVAILABLE\)/i,
        );
      });
    });

    it("displays honest conflict state on 409 STATE_CONFLICT", async () => {
      vi.spyOn(api, "interpretIntake").mockRejectedValue(
        new ApiClientError(
          409,
          "STATE_CONFLICT",
          "State conflict occurred.",
          true,
        ),
      );

      render(
        <IntakeAssistant
          onApplyDraft={vi.fn()}
          agentMode="strands_groq"
          workspaceId="ws-test-123"
        />,
      );

      fireEvent.click(
        screen.getByRole("button", { name: /Interpret Intake/i }),
      );

      await waitFor(() => {
        const alert = screen.getByRole("alert");
        expect(alert).toHaveTextContent(/Conflict \(409 STATE_CONFLICT\)/i);
      });
    });

    it("prevents duplicate submissions while request is pending", async () => {
      let resolvePromise: (val: IntakeInterpretResponse) => void;
      const pendingPromise = new Promise<IntakeInterpretResponse>((resolve) => {
        resolvePromise = resolve;
      });

      const spy = vi
        .spyOn(api, "interpretIntake")
        .mockReturnValue(pendingPromise);

      render(
        <IntakeAssistant
          onApplyDraft={vi.fn()}
          agentMode="strands_groq"
          workspaceId="ws-test-123"
        />,
      );

      const interpretBtn = screen.getByRole("button", {
        name: /Interpret Intake/i,
      });
      fireEvent.click(interpretBtn);

      // Button is now disabled and shows loading text
      expect(interpretBtn).toBeDisabled();
      expect(interpretBtn).toHaveTextContent(/Interpreting with Assistant.../i);

      // Attempt second click while pending
      fireEvent.click(interpretBtn);
      expect(spy).toHaveBeenCalledTimes(1);

      // Resolve pending request
      resolvePromise!(validHostedResponse);

      await waitFor(() => {
        expect(
          screen.getByRole("heading", { name: /Unsaved Intake Suggestion/i }),
        ).toBeInTheDocument();
      });
    });
  });
});
