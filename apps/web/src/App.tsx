import React, { useState, useEffect, useCallback, useRef } from "react";
import type {
  Snapshot,
  CreateRequestBody,
  CreateReservationBody,
  LoanPickupBody,
  LoanReturnBody,
  EquipmentInspectionBody,
  IntakeDraft,
} from "./types/api";
import { api, ApiClientError, generateIdempotencyKey } from "./api/client";
import { Header } from "./components/Header";
import { OfflineBanner } from "./components/OfflineBanner";
import { ExpiredSessionBanner } from "./components/ExpiredSessionBanner";
import { ConflictBanner } from "./components/ConflictBanner";
import { EquipmentList } from "./components/EquipmentList";
import { IntakeAssistant } from "./components/IntakeAssistant";
import { RequestForm } from "./components/RequestForm";
import { RequestList } from "./components/RequestList";
import { LoanList } from "./components/LoanList";
import { EventHistory } from "./components/EventHistory";
import { AllocationModal } from "./components/AllocationModal";
import { InspectionModal } from "./components/InspectionModal";

interface UncertainRetryState {
  description: string;
  idempotencyKey: string;
  retry: () => Promise<boolean>;
}

interface SnapshotErrorState {
  message: string;
  code?: string;
  isConnectionFailure: boolean;
}

export const App: React.FC = () => {
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [health, setHealth] = useState<{
    status: string;
    milestone: string;
    agent_mode: string;
  } | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [isMutating, setIsMutating] = useState(false);
  const [serverError, setServerError] = useState<string | null>(null);
  const [dialogError, setDialogError] = useState<string | null>(null);
  const [conflictMessage, setConflictMessage] = useState<string | null>(null);
  const [snapshotError, setSnapshotError] = useState<SnapshotErrorState | null>(
    null,
  );
  const [isExpiredSession, setIsExpiredSession] = useState(false);
  const [isStartingWorkspace, setIsStartingWorkspace] = useState(false);
  const [successMessage, setSuccessMessage] = useState<string | null>(null);
  const [draftForForm, setDraftForForm] = useState<IntakeDraft | null>(null);
  const [sessionGeneration, setSessionGeneration] = useState(1);

  // Store selected entity IDs; derive current entity instances from latest snapshot
  const [allocatingRequestId, setAllocatingRequestId] = useState<string | null>(
    null,
  );
  const [inspectingEquipmentId, setInspectingEquipmentId] = useState<
    string | null
  >(null);
  const [isLoanActionOpen, setIsLoanActionOpen] = useState(false);

  // Uncertain network retry state (immutable key/body closure)
  const [uncertainRetry, setUncertainRetry] =
    useState<UncertainRetryState | null>(null);
  const [isRetryingUncertain, setIsRetryingUncertain] = useState(false);

  // Mutex lock to guarantee synchronous rejection of duplicate submissions
  const mutatingLockRef = useRef(false);

  // Derive active dialog entities from latest snapshot
  const allocatingRequest =
    allocatingRequestId && snapshot
      ? snapshot.requests.find((r) => r.id === allocatingRequestId) || null
      : null;

  const inspectingEquipment =
    inspectingEquipmentId && snapshot
      ? snapshot.equipment.find((e) => e.id === inspectingEquipmentId) || null
      : null;

  const fetchHealth = useCallback(async () => {
    try {
      const data = await api.getHealth();
      setHealth(data);
    } catch {
      // Health is non-critical for snapshot rendering
    }
  }, []);

  const fetchSnapshot = useCallback(async () => {
    setIsLoading(true);
    setServerError(null);
    setSnapshotError(null);
    try {
      const data = await api.getSnapshot();
      setSnapshot(data);
      setIsExpiredSession(false);
    } catch (err: unknown) {
      if (err instanceof ApiClientError) {
        if (err.status === 401) {
          setIsExpiredSession(true);
          setAllocatingRequestId(null);
          setInspectingEquipmentId(null);
        } else if (err.isStructuredError) {
          setSnapshotError({
            message: err.message,
            code: err.code,
            isConnectionFailure: false,
          });
        } else if (
          err.status === 0 ||
          err.code === "CONNECTION_ERROR" ||
          err.status === 500
        ) {
          // Vite proxy ECONNREFUSED or upstream unavailable
          setSnapshotError({
            message: err.message,
            isConnectionFailure: true,
          });
        } else {
          setSnapshotError({
            message: err.message,
            code: err.code,
            isConnectionFailure: false,
          });
        }
      } else {
        setSnapshotError({
          message: "Unreachable backend network connection.",
          isConnectionFailure: true,
        });
      }
    } finally {
      setIsLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchHealth();
    fetchSnapshot();
  }, [fetchHealth, fetchSnapshot]);

  // Start synthetic workspace on 401 unauthenticated / expired session
  const handleStartWorkspace = async () => {
    setIsStartingWorkspace(true);
    setServerError(null);
    setConflictMessage(null);
    // Explicitly clear old retry closures and modal references on new session
    setUncertainRetry(null);
    setAllocatingRequestId(null);
    setInspectingEquipmentId(null);
    setDraftForForm(null);
    try {
      const res = await api.createWorkspace();
      setSnapshot(res.snapshot);
      setIsExpiredSession(false);
      setSnapshotError(null);
      setSessionGeneration((prev) => prev + 1);
      setSuccessMessage(
        "Synthetic workspace initialized with seed community inventory.",
      );
      setTimeout(() => setSuccessMessage(null), 5000);
    } catch (err: unknown) {
      if (err instanceof ApiClientError && err.isStructuredError) {
        setSnapshotError({
          message: err.message,
          code: err.code,
          isConnectionFailure: false,
        });
      } else if (
        err instanceof ApiClientError &&
        (err.status === 0 ||
          err.code === "CONNECTION_ERROR" ||
          err.status === 500)
      ) {
        setSnapshotError({
          message: err.message,
          isConnectionFailure: true,
        });
      } else {
        const msg =
          err instanceof Error ? err.message : "Failed to create workspace";
        setServerError(msg);
      }
    } finally {
      setIsStartingWorkspace(false);
    }
  };

  // Helper for executing mutation with conflict, uncertain network & retry handling
  const executeMutation = async (
    actionDescription: string,
    key: string,
    operation: (k: string) => Promise<unknown>,
    successText: string,
    onSuccessCleanup?: () => void,
  ): Promise<boolean> => {
    // 1. Guard against duplicate submissions while a mutation is in flight
    if (mutatingLockRef.current || isMutating) {
      return false;
    }

    // 2. Prevent overwriting an unresolved uncertain retry with a fresh action
    if (uncertainRetry && uncertainRetry.idempotencyKey !== key) {
      setServerError(
        "An unresolved uncertain network retry is pending. Please retry with the original key before submitting new actions.",
      );
      return false;
    }

    mutatingLockRef.current = true;
    setIsMutating(true);
    setServerError(null);
    setDialogError(null);
    setConflictMessage(null);

    let committed = false;
    try {
      await operation(key);
      committed = true;
      setUncertainRetry(null);
    } catch (err: unknown) {
      if (err instanceof ApiClientError) {
        // Definite terminal status: 409 conflict
        if (err.status === 409) {
          setUncertainRetry(null);
          setConflictMessage(
            `Action '${actionDescription}' conflicted with a concurrent update (409 ${err.code}): ${err.message}`,
          );
          try {
            const freshSnapshot = await api.getSnapshot();
            setSnapshot(freshSnapshot);
          } catch {
            // ignore refetch err
          }
          return false;
        }

        // Definite terminal status: 401 session expired
        if (err.status === 401) {
          setUncertainRetry(null);
          setIsExpiredSession(true);
          setAllocatingRequestId(null);
          setInspectingEquipmentId(null);
          setServerError(
            "Your session has expired. Please re-initialize your workspace.",
          );
          return false;
        }

        // Definite terminal status: 400, 403, 404, 422, or structured 500
        if (
          err.status === 422 ||
          err.status === 400 ||
          err.status === 403 ||
          err.status === 404 ||
          (err.status === 500 && err.isStructuredError)
        ) {
          setUncertainRetry(null);
          const errorMsg = `Submission error (${err.code}): ${err.message}`;
          setServerError(errorMsg);
          setDialogError(errorMsg);
          return false;
        }

        // Uncertain state: 0 (network failed), CONNECTION_ERROR, RESPONSE_PARSE_ERROR, or unstructured 500
        if (
          err.status === 0 ||
          err.code === "CONNECTION_ERROR" ||
          err.code === "RESPONSE_PARSE_ERROR" ||
          (err.status === 500 && !err.isStructuredError)
        ) {
          setUncertainRetry({
            description: actionDescription,
            idempotencyKey: key,
            retry: async () => {
              return await executeMutation(
                actionDescription,
                key,
                operation,
                successText,
                onSuccessCleanup,
              );
            },
          });
          return false;
        }

        const errorMsg = `Submission error (${err.code}): ${err.message}`;
        setServerError(errorMsg);
        setDialogError(errorMsg);
        return false;
      } else {
        // Generic error thrown during fetch / network - treat as uncertain to preserve key/body
        setUncertainRetry({
          description: actionDescription,
          idempotencyKey: key,
          retry: async () => {
            return await executeMutation(
              actionDescription,
              key,
              operation,
              successText,
              onSuccessCleanup,
            );
          },
        });
        return false;
      }
    } finally {
      mutatingLockRef.current = false;
      setIsMutating(false);
    }

    // Mutation succeeded and was confirmed committed on the server.
    if (committed) {
      onSuccessCleanup?.();
      try {
        const freshSnapshot = await api.getSnapshot();
        setSnapshot(freshSnapshot);
        setSuccessMessage(successText);
        setTimeout(() => setSuccessMessage(null), 6000);
      } catch (refreshErr: unknown) {
        if (refreshErr instanceof ApiClientError && refreshErr.status === 401) {
          setIsExpiredSession(true);
          setAllocatingRequestId(null);
          setInspectingEquipmentId(null);
          setServerError(
            "Your session has expired. Please re-initialize your workspace.",
          );
        } else {
          setServerError(
            `${successText} (Notice: snapshot view refresh failed. Use 'Sync Snapshot' to reload latest inventory.)`,
          );
        }
      }
      return true;
    }

    return false;
  };

  // Intake structured request
  const handleCreateRequest = async (body: CreateRequestBody) => {
    const key = generateIdempotencyKey();
    await executeMutation(
      `Intake request for ${body.borrower_label}`,
      key,
      (k) => api.createRequest(body, k),
      `Server confirmed: Request registered for ${body.borrower_label}.`,
    );
  };

  // Allocation confirmation
  const handleConfirmAllocation = async (
    body: CreateReservationBody,
  ): Promise<boolean> => {
    const key = generateIdempotencyKey();
    return await executeMutation(
      `Allocate equipment ${body.equipment_id}`,
      key,
      (k) => api.createReservation(body, k),
      "Server confirmed: Equipment allocated and reserved.",
      () => setAllocatingRequestId(null),
    );
  };

  // Mechanical inspection confirmation
  const handleConfirmInspection = async (
    equipmentId: string,
    body: EquipmentInspectionBody,
  ): Promise<boolean> => {
    const key = generateIdempotencyKey();
    return await executeMutation(
      `Inspect equipment ${equipmentId} (${body.outcome})`,
      key,
      (k) => api.inspectEquipment(equipmentId, body, k),
      `Server confirmed: Equipment inspection recorded with outcome ${body.outcome}.`,
      () => setInspectingEquipmentId(null),
    );
  };

  // Loan pickup confirmation
  const handleConfirmPickup = async (
    loanId: string,
    body: LoanPickupBody,
    onSuccessCleanup?: () => void,
  ): Promise<boolean> => {
    const key = generateIdempotencyKey();
    return await executeMutation(
      `Confirm pickup for loan ${loanId}`,
      key,
      (k) => api.pickupLoan(loanId, body, k),
      "Server confirmed: Equipment pickup marked. Item is now ON_LOAN.",
      onSuccessCleanup,
    );
  };

  // Loan return confirmation
  const handleConfirmReturn = async (
    loanId: string,
    body: LoanReturnBody,
    onSuccessCleanup?: () => void,
  ): Promise<boolean> => {
    const key = generateIdempotencyKey();
    return await executeMutation(
      `Receive return for loan ${loanId}`,
      key,
      (k) => api.returnLoan(loanId, body, k),
      "Server confirmed: Return recorded. Equipment placed into AWAITING_INSPECTION.",
      onSuccessCleanup,
    );
  };

  const handleExecuteUncertainRetry = async () => {
    if (!uncertainRetry || isRetryingUncertain) return;
    setIsRetryingUncertain(true);
    try {
      await uncertainRetry.retry();
    } finally {
      setIsRetryingUncertain(false);
    }
  };

  const isModalOpen = Boolean(
    allocatingRequest || inspectingEquipment || isLoanActionOpen,
  );

  return (
    <div className="app-root">
      <Header
        milestone={health?.milestone || "M2A"}
        agentMode={
          health?.agent_mode || snapshot?.agent_mode || "not_implemented"
        }
      />

      <main className="container">
        {/* Diagnostic Snapshot / Connection Failure Banner */}
        {snapshotError &&
          !uncertainRetry &&
          (snapshotError.isConnectionFailure ? (
            <OfflineBanner
              message={snapshotError.message}
              onRetry={fetchSnapshot}
              isRetrying={isLoading}
            />
          ) : (
            <div className="banner banner-error" role="alert">
              <div>
                <strong>
                  Server Error
                  {snapshotError.code ? ` (${snapshotError.code})` : ""}:
                </strong>{" "}
                {snapshotError.message}
              </div>
              <button
                type="button"
                className="btn btn-secondary"
                onClick={fetchSnapshot}
                disabled={isLoading}
              >
                {isLoading ? "Retrying..." : "Retry Snapshot"}
              </button>
            </div>
          ))}

        {/* 401 Missing / Expired Session */}
        {isExpiredSession && (
          <ExpiredSessionBanner
            onStartWorkspace={handleStartWorkspace}
            isStarting={isStartingWorkspace}
          />
        )}

        {/* 409 Competing Update Conflict - Render in page only if no modal is active */}
        {conflictMessage && !isModalOpen && (
          <ConflictBanner
            message={conflictMessage}
            onDismiss={() => setConflictMessage(null)}
          />
        )}

        {/* Uncertain network retry banner - Render in page only if no modal is active */}
        {uncertainRetry && !isModalOpen && (
          <div className="banner banner-conflict" role="alert">
            <div>
              <strong>Uncertain Network Response:</strong>
              <div>
                Operation '{uncertainRetry.description}' may not have reached
                the server or response was interrupted.
              </div>
              <div style={{ fontSize: "0.8rem", marginTop: "0.25rem" }}>
                Retaining original Idempotency-Key{" "}
                <code>{uncertainRetry.idempotencyKey.slice(0, 16)}...</code> to
                prevent duplicate mutations.
              </div>
            </div>
            <div>
              <button
                type="button"
                className="btn btn-primary"
                onClick={handleExecuteUncertainRetry}
                disabled={isRetryingUncertain}
              >
                {isRetryingUncertain
                  ? "Retrying..."
                  : "Retry with Original Key"}
              </button>
            </div>
          </div>
        )}

        {/* Success Confirmation */}
        {successMessage && (
          <div className="banner banner-success" role="status">
            <div>{successMessage}</div>
            <button
              type="button"
              className="btn btn-secondary"
              onClick={() => setSuccessMessage(null)}
              style={{ padding: "0.25rem 0.5rem", minHeight: "auto" }}
            >
              &times;
            </button>
          </div>
        )}

        {/* General Server Error / Snapshot Refresh Notice - Suppressed behind open modal */}
        {serverError && !isModalOpen && (
          <div className="banner banner-error" role="alert">
            <div>{serverError}</div>
            <div style={{ display: "flex", gap: "0.5rem" }}>
              <button
                type="button"
                className="btn btn-secondary"
                onClick={fetchSnapshot}
                disabled={isLoading}
              >
                Sync Snapshot
              </button>
              <button
                type="button"
                className="btn btn-secondary"
                onClick={() => setServerError(null)}
              >
                Dismiss
              </button>
            </div>
          </div>
        )}

        {/* Main Content Dashboard */}
        {snapshot && !isExpiredSession && (
          <>
            <div
              style={{
                marginBottom: "1.5rem",
                display: "flex",
                justifyContent: "space-between",
                alignItems: "center",
                flexWrap: "wrap",
                gap: "0.75rem",
              }}
            >
              <span
                style={{ fontSize: "0.875rem", color: "var(--text-muted)" }}
              >
                Active Session Scoped to Server Cookie &bull; Synthetic
                Evaluation Mode
              </span>
              <button
                type="button"
                className="btn btn-secondary"
                onClick={fetchSnapshot}
                disabled={isLoading || isMutating}
                title="Refresh inventory from server"
              >
                {isLoading ? "Syncing..." : "Sync Snapshot"}
              </button>
            </div>

            <div className="grid-main">
              {/* Top Grid: Equipment Inventory and Request Intake Form */}
              <div className="grid-two">
                <EquipmentList
                  equipment={snapshot.equipment}
                  onSelectInspect={(item) => {
                    setInspectingEquipmentId(item.id);
                    setDialogError(null);
                    setServerError(null);
                  }}
                  isLoading={isLoading || isMutating}
                />
                <div
                  className="intake-column"
                  style={{
                    display: "flex",
                    flexDirection: "column",
                    gap: "1.5rem",
                  }}
                >
                  <IntakeAssistant
                    onApplyDraft={(draft) => setDraftForForm(draft)}
                    agentMode={
                      health?.agent_mode ||
                      snapshot.agent_mode ||
                      "not_implemented"
                    }
                    workspaceId={`session-${sessionGeneration}`}
                    onStartWorkspace={handleStartWorkspace}
                    onSessionExpired={() => {
                      setDraftForForm(null);
                    }}
                  />
                  <RequestForm
                    onSubmit={handleCreateRequest}
                    isSubmitting={isMutating}
                    draft={draftForForm}
                    onClearDraft={() => setDraftForForm(null)}
                  />
                </div>
              </div>

              {/* Middle Grid: Pending Requests & Active Loans */}
              <div className="grid-two">
                <RequestList
                  requests={snapshot.requests}
                  onSelectAllocate={(req) => {
                    setAllocatingRequestId(req.id);
                    setDialogError(null);
                    setServerError(null);
                  }}
                  isLoading={isLoading || isMutating}
                />
                <LoanList
                  loans={snapshot.loans}
                  equipment={snapshot.equipment}
                  requests={snapshot.requests}
                  onPickup={handleConfirmPickup}
                  onReturn={handleConfirmReturn}
                  isLoading={isLoading || isMutating}
                  conflictMessage={conflictMessage}
                  uncertainRetry={uncertainRetry}
                  onRetryUncertain={handleExecuteUncertainRetry}
                  isRetryingUncertain={isRetryingUncertain}
                  error={dialogError}
                  onClearError={() => {
                    setDialogError(null);
                    setServerError(null);
                  }}
                  onActionChange={(isOpen) => {
                    setIsLoanActionOpen(isOpen);
                    if (!isOpen) {
                      setDialogError(null);
                      setServerError(null);
                    }
                  }}
                />
              </div>

              {/* Audit Log / Event Stream */}
              <EventHistory events={snapshot.events} isLoading={isLoading} />
            </div>
          </>
        )}

        {/* Allocation Modal - Gated so it cannot render if session expired */}
        {!isExpiredSession && allocatingRequest && snapshot && (
          <AllocationModal
            request={allocatingRequest}
            availableEquipment={snapshot.equipment}
            onConfirm={handleConfirmAllocation}
            onClose={() => {
              setAllocatingRequestId(null);
              setDialogError(null);
              setServerError(null);
            }}
            isSubmitting={isMutating}
            conflictMessage={conflictMessage}
            uncertainRetry={uncertainRetry}
            onRetryUncertain={handleExecuteUncertainRetry}
            isRetryingUncertain={isRetryingUncertain}
            error={dialogError}
            onClearError={() => {
              setDialogError(null);
              setServerError(null);
            }}
          />
        )}

        {/* Inspection Modal - Gated so it cannot render if session expired */}
        {!isExpiredSession && inspectingEquipment && (
          <InspectionModal
            item={inspectingEquipment}
            onConfirm={handleConfirmInspection}
            onClose={() => {
              setInspectingEquipmentId(null);
              setDialogError(null);
              setServerError(null);
            }}
            isSubmitting={isMutating}
            conflictMessage={conflictMessage}
            uncertainRetry={uncertainRetry}
            onRetryUncertain={handleExecuteUncertainRetry}
            isRetryingUncertain={isRetryingUncertain}
            error={dialogError}
            onClearError={() => {
              setDialogError(null);
              setServerError(null);
            }}
          />
        )}
      </main>
    </div>
  );
};

export default App;
