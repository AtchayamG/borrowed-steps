import React, { useState, useRef, useEffect } from "react";
import type { IntakeDraft, IntakeProvenance, AgentMode } from "../types/api";
import { api, ApiClientError } from "../api/client";
import { isoToLocalInput } from "../utils/dateTime";
import { validateIntakeResponse } from "../utils/validation";

interface IntakeAssistantProps {
  onApplyDraft: (draft: IntakeDraft) => void;
  agentMode?: AgentMode | string;
  workspaceId?: string;
  onSessionExpired?: () => void;
  onStartWorkspace?: () => void;
}

interface SuggestionState {
  sourceText: string;
  draft: IntakeDraft;
  missing_fields: string[];
  provenance: IntakeProvenance;
}

interface IntakeErrorState {
  status: number;
  code: string;
  message: string;
}

export const IntakeAssistant: React.FC<IntakeAssistantProps> = ({
  onApplyDraft,
  agentMode = "not_implemented",
  workspaceId,
  onSessionExpired,
  onStartWorkspace,
}) => {
  const [inputText, setInputText] = useState(
    "Velachery resident Ananya R. requires a wheelchair pickup at Velachery Community Room by 2026-09-20T10:00:00Z",
  );
  const [isInterpreting, setIsInterpreting] = useState(false);
  const [intakeError, setIntakeError] = useState<IntakeErrorState | null>(null);
  const [suggestion, setSuggestion] = useState<SuggestionState | null>(null);
  const [appliedNotice, setAppliedNotice] = useState<string | null>(null);

  // Synchronous mutex lock to guard against duplicate submits
  const interpretingLockRef = useRef(false);
  // Stale response tracking: monotonic request ID counter and abort controller
  const requestIdRef = useRef(0);
  const abortControllerRef = useRef<AbortController | null>(null);
  const workspaceIdRef = useRef(workspaceId);
  const prevWorkspaceIdRef = useRef(workspaceId);

  // Handle workspaceId changes: invalidate prior suggestion/error/notice,
  // abort in-flight requests, and reset lock without wiping user input text.
  useEffect(() => {
    if (prevWorkspaceIdRef.current !== workspaceId) {
      prevWorkspaceIdRef.current = workspaceId;
      workspaceIdRef.current = workspaceId;
      if (abortControllerRef.current) {
        abortControllerRef.current.abort();
      }
      requestIdRef.current++;
      interpretingLockRef.current = false;
      setIsInterpreting(false);
      setSuggestion(null);
      setAppliedNotice(null);
      setIntakeError(null);
    }
  }, [workspaceId]);

  // Clean up in-flight requests on unmount
  useEffect(() => {
    return () => {
      if (abortControllerRef.current) {
        abortControllerRef.current.abort();
      }
    };
  }, []);

  const handleInterpret = async () => {
    const trimmed = inputText.trim();
    if (!trimmed) {
      setIntakeError({
        status: 422,
        code: "VALIDATION_ERROR",
        message: "Intake text must not be empty.",
      });
      return;
    }
    if (trimmed.length > 2000) {
      setIntakeError({
        status: 422,
        code: "VALIDATION_ERROR",
        message: "Intake text exceeds maximum length of 2000 characters.",
      });
      return;
    }

    if (interpretingLockRef.current || isInterpreting) {
      return;
    }

    // Cancel prior in-flight request if any
    if (abortControllerRef.current) {
      abortControllerRef.current.abort();
    }
    const controller = new AbortController();
    abortControllerRef.current = controller;

    const currentRequestId = ++requestIdRef.current;
    const capturedWorkspace = workspaceIdRef.current;

    interpretingLockRef.current = true;
    setIsInterpreting(true);
    setIntakeError(null);
    setAppliedNotice(null);
    setSuggestion(null);

    try {
      const rawResponse = await api.interpretIntake(
        { text: trimmed },
        controller.signal,
      );

      // Check for stale response: newer request triggered or workspace changed
      if (
        currentRequestId !== requestIdRef.current ||
        capturedWorkspace !== workspaceIdRef.current
      ) {
        return;
      }

      // Strictly validate response against frozen M2A contract
      const response = validateIntakeResponse(rawResponse);

      setSuggestion({
        sourceText: trimmed,
        draft: response.draft,
        missing_fields: response.missing_fields || [],
        provenance: response.provenance,
      });
    } catch (err: unknown) {
      // If cancelled via AbortController, ignore
      if (err instanceof ApiClientError && err.code === "ABORT_ERROR") {
        return;
      }

      // If stale, drop
      if (
        currentRequestId !== requestIdRef.current ||
        capturedWorkspace !== workspaceIdRef.current
      ) {
        return;
      }

      setSuggestion(null);
      setAppliedNotice(null);

      if (err instanceof ApiClientError) {
        if (err.status === 401) {
          onSessionExpired?.();
          setIntakeError({
            status: 401,
            code: "SESSION_REQUIRED",
            message:
              "Session Required (401): Active session expired. Your session has expired or no workspace has been initialized for this browser. Start a synthetic workspace to seed the Velachery equipment inventory and coordinate requests.",
          });
          return;
        }

        if (err.status === 429) {
          setIntakeError({
            status: 429,
            code: "ASSISTANT_BUSY",
            message:
              "Assistant Busy (429 ASSISTANT_BUSY): Another inference request is currently in progress. Please retry shortly.",
          });
          return;
        }

        if (err.status === 503) {
          setIntakeError({
            status: 503,
            code: err.code || "ASSISTANT_UNAVAILABLE",
            message: `Assistant Unavailable (503 ${err.code}): Local assistant service is unavailable or disabled. Manual request entry is available below.`,
          });
          return;
        }

        if (err.status === 502) {
          setIntakeError({
            status: 502,
            code: "ASSISTANT_INVALID_OUTPUT",
            message: `Assistant Invalid Output (502 ASSISTANT_INVALID_OUTPUT): ${err.message}`,
          });
          return;
        }

        if (err.status === 504) {
          setIntakeError({
            status: 504,
            code: "ASSISTANT_TIMEOUT",
            message:
              "Assistant Timeout (504 ASSISTANT_TIMEOUT): Model inference exceeded time limit (120s). Please retry or enter request manually.",
          });
          return;
        }

        if (err.status === 422) {
          setIntakeError({
            status: 422,
            code: "VALIDATION_ERROR",
            message: `Validation Error (422 VALIDATION_ERROR): ${err.message}`,
          });
          return;
        }

        if (err.status === 403) {
          setIntakeError({
            status: 403,
            code: "ORIGIN_FORBIDDEN",
            message:
              "Access Forbidden (403 ORIGIN_FORBIDDEN): Request origin rejected.",
          });
          return;
        }

        if (err.status === 0 || err.code === "CONNECTION_ERROR") {
          setIntakeError({
            status: 0,
            code: "CONNECTION_ERROR",
            message:
              "Connection Error: Backend unreachable or offline. Ensure backend is running on 127.0.0.1:8000.",
          });
          return;
        }

        setIntakeError({
          status: err.status,
          code: err.code,
          message: `Server Error (${err.code}): ${err.message}`,
        });
      } else {
        setIntakeError({
          status: 0,
          code: "UNKNOWN_ERROR",
          message:
            err instanceof Error
              ? err.message
              : "Failed to communicate with assistant.",
        });
      }
    } finally {
      if (
        currentRequestId === requestIdRef.current &&
        capturedWorkspace === workspaceIdRef.current
      ) {
        interpretingLockRef.current = false;
        setIsInterpreting(false);
      }
    }
  };

  const handleUseDraft = () => {
    if (!suggestion) return;
    onApplyDraft(suggestion.draft);
    setAppliedNotice(
      "Draft applied to request form below. Missing fields remain blank for volunteer review and completion.",
    );
  };

  const handleDiscardDraft = () => {
    setSuggestion(null);
    setAppliedNotice(null);
  };

  const handleClearError = () => {
    setIntakeError(null);
  };

  return (
    <div className="card" aria-labelledby="intake-assistant-heading">
      <div className="card-title">
        <h2
          id="intake-assistant-heading"
          style={{ fontSize: "inherit", margin: 0 }}
        >
          Synthetic Intake Assistant
        </h2>
        <span
          className="header-badge"
          style={{ backgroundColor: "var(--indigo-700)" }}
        >
          M2A Strands Assistant
        </span>
      </div>

      <div
        style={{
          marginBottom: "0.75rem",
          fontSize: "0.85rem",
          color: "var(--text-secondary)",
        }}
      >
        <span>
          Configured Mode: <strong>{agentMode}</strong> &bull; Read-only
          suggestion adapter
        </span>
      </div>

      {intakeError && (
        <div
          className="banner banner-error"
          role="alert"
          style={{ marginBottom: "1rem" }}
        >
          <div>
            <strong>
              {intakeError.status === 401
                ? "Session Required (401)"
                : `Error (${intakeError.code})`}
              :
            </strong>{" "}
            {intakeError.message}
          </div>
          <div
            style={{
              display: "flex",
              gap: "0.5rem",
              flexWrap: "wrap",
              marginTop: "0.5rem",
            }}
          >
            {intakeError.status === 401 && onStartWorkspace ? (
              <button
                type="button"
                className="btn btn-primary"
                onClick={onStartWorkspace}
                style={{ minHeight: "32px", padding: "0.25rem 0.75rem" }}
              >
                Start Synthetic Workspace
              </button>
            ) : (
              <button
                type="button"
                className="btn btn-secondary"
                onClick={handleInterpret}
                disabled={isInterpreting}
                style={{ minHeight: "32px", padding: "0.25rem 0.75rem" }}
              >
                Retry Interpretation
              </button>
            )}
            <button
              type="button"
              className="btn btn-secondary"
              onClick={handleClearError}
              style={{ minHeight: "32px", padding: "0.25rem 0.75rem" }}
            >
              Manual Entry
            </button>
          </div>
        </div>
      )}

      {appliedNotice && (
        <div
          className="banner banner-success"
          role="status"
          style={{ marginBottom: "1rem" }}
        >
          <div>{appliedNotice}</div>
          <button
            type="button"
            className="btn btn-secondary"
            onClick={() => setAppliedNotice(null)}
            style={{ padding: "0.25rem 0.5rem", minHeight: "auto" }}
          >
            &times;
          </button>
        </div>
      )}

      <div className="form-group">
        <label htmlFor="synthetic-intake-text" className="form-label">
          Synthetic Intake Notes (1–2000 characters)
        </label>
        <textarea
          id="synthetic-intake-text"
          className="form-input"
          rows={4}
          maxLength={2000}
          value={inputText}
          onChange={(e) => {
            setInputText(e.target.value);
            if (suggestion) {
              setSuggestion(null);
            }
            if (appliedNotice) {
              setAppliedNotice(null);
            }
          }}
          disabled={isInterpreting}
          placeholder="Enter synthetic intake details... e.g.: Ananya R. requires a wheelchair pickup at Velachery Community Room by 2026-09-20T10:00:00Z"
          aria-describedby="intake-help"
          style={{ resize: "vertical", minHeight: "90px" }}
        />
        <div
          id="intake-help"
          className="form-help"
          style={{
            display: "flex",
            flexDirection: "column",
            gap: "0.25rem",
            marginTop: "0.35rem",
          }}
        >
          <span>
            &bull; <strong>Full ISO Datetime Required:</strong> Must provide a
            complete timezone-aware datetime (e.g.{" "}
            <code>2026-09-20T10:00:00Z</code>). Relative dates (e.g. 'next
            week', 'tomorrow') cannot be resolved in this slice and require
            human clarification.
          </span>
          <span>
            &bull; <strong>Synthetic Data Only:</strong> Testing purposes only;
            no real medical diagnosis, suitability claims, or contact details.
          </span>
        </div>
      </div>

      <div
        style={{
          display: "flex",
          gap: "0.75rem",
          alignItems: "center",
          flexWrap: "wrap",
        }}
      >
        <button
          type="button"
          className="btn btn-primary"
          onClick={handleInterpret}
          disabled={isInterpreting || !inputText.trim()}
          aria-busy={isInterpreting}
        >
          {isInterpreting
            ? "Interpreting with Assistant..."
            : "Interpret Intake"}
        </button>
        {isInterpreting && (
          <span
            role="status"
            aria-live="polite"
            style={{
              fontSize: "0.85rem",
              color: "var(--indigo-700)",
              fontWeight: 600,
            }}
          >
            Analyzing intake and verifying inventory...
          </span>
        )}
      </div>

      {/* Unsaved Suggestion Review Panel */}
      {suggestion && (
        <div
          className="intake-suggestion-box"
          aria-labelledby="suggestion-heading"
          style={{ marginTop: "1.25rem" }}
        >
          <div className="suggestion-header">
            <h3 id="suggestion-heading" className="suggestion-title">
              Unsaved Intake Suggestion
            </h3>
            <span className="tag tag-awaiting_inspection">
              Unsaved Ephemeral Draft
            </span>
          </div>

          <p className="suggestion-disclaimer">
            This suggestion is an ephemeral advisory draft and has NOT created a
            request or reservation. Review all extracted values before
            deliberately applying to the form.
          </p>

          <div className="suggestion-section">
            <div className="suggestion-label">Source Text:</div>
            <div className="suggestion-source-text">
              {suggestion.sourceText}
            </div>
          </div>

          <div className="suggestion-grid">
            <div className="suggestion-field">
              <span className="suggestion-field-name">Borrower Label:</span>
              {suggestion.draft.borrower_label ? (
                <span className="suggestion-field-value">
                  <strong>{suggestion.draft.borrower_label}</strong>
                </span>
              ) : (
                <span className="tag tag-repair" role="alert">
                  Clarification needed: Borrower label not identified
                </span>
              )}
            </div>

            <div className="suggestion-field">
              <span className="suggestion-field-name">Equipment Kind:</span>
              {suggestion.draft.equipment_kind ? (
                <span className="tag tag-available">
                  {suggestion.draft.equipment_kind}
                </span>
              ) : (
                <span className="tag tag-repair" role="alert">
                  Clarification needed: No explicit equipment kind found
                  (wheelchair, walker, crutches)
                </span>
              )}
            </div>

            <div className="suggestion-field">
              <span className="suggestion-field-name">Pickup Location:</span>
              {suggestion.draft.pickup_location ? (
                <span className="suggestion-field-value">
                  {suggestion.draft.pickup_location}
                </span>
              ) : (
                <span className="tag tag-repair" role="alert">
                  Clarification needed: Pickup location not identified
                </span>
              )}
            </div>

            <div className="suggestion-field">
              <span className="suggestion-field-name">Return Due Date:</span>
              {suggestion.draft.due_at ? (
                <span className="suggestion-field-value">
                  {suggestion.draft.due_at}
                  <span
                    style={{
                      fontSize: "0.8rem",
                      color: "var(--text-muted)",
                      marginLeft: "0.5rem",
                    }}
                  >
                    (Local:{" "}
                    {isoToLocalInput(suggestion.draft.due_at).replace("T", " ")}
                    )
                  </span>
                </span>
              ) : (
                <span className="tag tag-repair" role="alert">
                  Clarification needed: Full ISO datetime required (relative
                  dates like 'tomorrow' are unsupported)
                </span>
              )}
            </div>
          </div>

          {suggestion.missing_fields.length > 0 && (
            <div
              className="clarification-box"
              role="status"
              style={{ marginTop: "0.75rem" }}
            >
              <strong>Human Clarification Required:</strong> Missing fields (
              {suggestion.missing_fields.join(", ")}) will remain empty in the
              form for manual completion.
            </div>
          )}

          {/* Validated Call Provenance Block */}
          <div className="provenance-block" style={{ marginTop: "1rem" }}>
            <div className="provenance-title">
              Assistant Provenance (Validated Call)
            </div>
            <div className="provenance-grid">
              <div>
                <span>Framework:</span>{" "}
                <code>{suggestion.provenance.framework}</code>
              </div>
              <div>
                <span>Provider:</span>{" "}
                <code>{suggestion.provenance.provider}</code>
              </div>
              <div>
                <span>Model:</span> <code>{suggestion.provenance.model}</code>
              </div>
              <div>
                <span>Inventory Tool Calls:</span>{" "}
                <code>{suggestion.provenance.inventory_tool_calls}</code>
              </div>
              <div>
                <span>Completed At:</span>{" "}
                <code>{suggestion.provenance.completed_at}</code>
              </div>
            </div>
          </div>

          <div
            className="suggestion-actions"
            style={{
              marginTop: "1rem",
              display: "flex",
              gap: "0.75rem",
              flexWrap: "wrap",
            }}
          >
            <button
              type="button"
              className="btn btn-primary"
              onClick={handleUseDraft}
            >
              Use Draft (Populate Form)
            </button>
            <button
              type="button"
              className="btn btn-secondary"
              onClick={handleDiscardDraft}
            >
              Discard Draft
            </button>
          </div>
        </div>
      )}
    </div>
  );
};
