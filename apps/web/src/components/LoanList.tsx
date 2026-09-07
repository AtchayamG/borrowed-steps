import React, { useState, useEffect, useRef } from "react";
import type {
  Loan,
  Equipment,
  BorrowRequest,
  LoanPickupBody,
  LoanReturnBody,
} from "../types/api";
import { useModalFocusTrap } from "../hooks/useModalFocusTrap";

interface LoanListProps {
  loans: Loan[];
  equipment: Equipment[];
  requests: BorrowRequest[];
  onPickup: (
    loanId: string,
    body: LoanPickupBody,
    onSuccess?: () => void,
  ) => Promise<boolean>;
  onReturn: (
    loanId: string,
    body: LoanReturnBody,
    onSuccess?: () => void,
  ) => Promise<boolean>;
  isLoading?: boolean;
  conflictMessage?: string | null;
  uncertainRetry?: {
    description: string;
    idempotencyKey: string;
    retry: () => Promise<boolean>;
  } | null;
  onRetryUncertain?: () => void;
  isRetryingUncertain?: boolean;
  error?: string | null;
  onClearError?: () => void;
  onActionChange?: (isOpen: boolean) => void;
}

export const LoanList: React.FC<LoanListProps> = ({
  loans,
  equipment,
  requests,
  onPickup,
  onReturn,
  isLoading = false,
  conflictMessage,
  uncertainRetry,
  onRetryUncertain,
  isRetryingUncertain = false,
  error: serverError,
  onClearError,
  onActionChange,
}) => {
  // Store only the active action type and loan ID; derive current entities from props
  const [activeAction, setActiveAction] = useState<{
    type: "PICKUP" | "RETURN";
    loanId: string;
  } | null>(null);

  // Strictly unchecked by default
  const [humanApproved, setHumanApproved] = useState(false);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  const modalRef = useRef<HTMLDivElement>(null);
  const checkboxRef = useRef<HTMLInputElement>(null);
  const submittingRef = useRef(false);
  const prevEquipmentVersionRef = useRef<number | undefined>(undefined);

  // Derive current objects from latest snapshot props
  const activeLoan = activeAction
    ? loans.find((l) => l.id === activeAction.loanId) || null
    : null;
  const activeEquipment = activeLoan
    ? equipment.find((e) => e.id === activeLoan.equipment_id) || null
    : null;

  // Track equipment version updates from snapshot refresh
  useEffect(() => {
    if (activeEquipment) {
      if (
        prevEquipmentVersionRef.current !== undefined &&
        prevEquipmentVersionRef.current !== activeEquipment.version
      ) {
        setHumanApproved(false);
        setActionError(
          `Equipment ${activeEquipment.label} was updated to v${activeEquipment.version}. Please re-affirm approval.`,
        );
      }
      prevEquipmentVersionRef.current = activeEquipment.version;
    }
  }, [activeEquipment]);

  const getEquipment = (id: string) => equipment.find((e) => e.id === id);
  const getRequest = (id: string) => requests.find((r) => r.id === id);

  const getStatusTag = (status: string) => {
    switch (status) {
      case "RESERVED":
        return "tag tag-reserved";
      case "ON_LOAN":
        return "tag tag-on_loan";
      case "RETURNED":
        return "tag tag-returned";
      default:
        return "tag tag-closed";
    }
  };

  const handleOpenAction = (type: "PICKUP" | "RETURN", loanId: string) => {
    setActiveAction({ type, loanId });
    setHumanApproved(false);
    setActionError(null);
    onClearError?.();
    onActionChange?.(true);
    const loan = loans.find((l) => l.id === loanId);
    const item = loan
      ? equipment.find((e) => e.id === loan.equipment_id)
      : null;
    prevEquipmentVersionRef.current = item?.version;
  };

  const handleCloseAction = () => {
    setActiveAction(null);
    setHumanApproved(false);
    setActionError(null);
    onClearError?.();
    onActionChange?.(false);
  };

  // Shared accessible focus trap, initial focus, and Escape key listener
  useModalFocusTrap({
    modalRef,
    initialFocusRef: checkboxRef,
    onClose: handleCloseAction,
    isDisabled: isSubmitting || isRetryingUncertain,
  });

  // Eligibility check
  const isEligible = Boolean(
    activeLoan &&
    activeEquipment &&
    ((activeAction?.type === "PICKUP" &&
      activeLoan.status === "RESERVED" &&
      activeEquipment.state === "RESERVED") ||
      (activeAction?.type === "RETURN" &&
        activeLoan.status === "ON_LOAN" &&
        activeEquipment.state === "ON_LOAN")),
  );

  const handleSubmitAction = async (e: React.FormEvent) => {
    e.preventDefault();
    if (
      !activeAction ||
      !activeLoan ||
      !activeEquipment ||
      isSubmitting ||
      submittingRef.current ||
      !isEligible
    ) {
      return;
    }

    if (!humanApproved) {
      setActionError(
        "Volunteer confirmation is required before updating loan status.",
      );
      return;
    }

    submittingRef.current = true;
    setIsSubmitting(true);
    setActionError(null);
    try {
      let success = false;
      if (activeAction.type === "PICKUP") {
        success = await onPickup(
          activeLoan.id,
          {
            expected_equipment_version: activeEquipment.version,
            human_approved: true,
          },
          () => handleCloseAction(),
        );
      } else {
        success = await onReturn(
          activeLoan.id,
          {
            expected_equipment_version: activeEquipment.version,
            human_approved: true,
          },
          () => handleCloseAction(),
        );
      }

      if (success) {
        handleCloseAction();
      }
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : "Action failed";
      setActionError(message);
    } finally {
      submittingRef.current = false;
      setIsSubmitting(false);
    }
  };

  return (
    <div className="card" aria-labelledby="loans-heading">
      <h2 id="loans-heading" className="card-title">
        Active &amp; Tracked Loans ({loans.length})
      </h2>
      {isLoading && loans.length === 0 ? (
        <div className="empty-state">Loading loan records...</div>
      ) : loans.length === 0 ? (
        <div className="empty-state">No loans currently active.</div>
      ) : (
        <div role="list">
          {loans.map((loan) => {
            const item = getEquipment(loan.equipment_id);
            const req = getRequest(loan.request_id);

            return (
              <div key={loan.id} role="listitem" className="item-row">
                <div className="item-info">
                  <div className="item-title">
                    Loan #{loan.id} &bull;{" "}
                    {item?.label || "Equipment ID: " + loan.equipment_id}
                  </div>
                  <div className="item-meta">
                    <span>
                      Borrower:{" "}
                      <strong>{req?.borrower_label || "Unknown"}</strong>
                    </span>
                    <span>&bull;</span>
                    <span>
                      Item State: {item?.state || "Unknown"} (v
                      {item?.version ?? "?"})
                    </span>
                    <span>&bull;</span>
                    <span>
                      Due: {new Date(loan.due_at).toLocaleDateString()}
                    </span>
                  </div>
                </div>
                <div className="item-actions">
                  <span className={getStatusTag(loan.status)}>
                    {loan.status}
                  </span>
                  {loan.status === "RESERVED" && item && (
                    <button
                      type="button"
                      className="btn btn-primary"
                      onClick={() => handleOpenAction("PICKUP", loan.id)}
                      aria-label={`Confirm pickup for loan ${loan.id}`}
                    >
                      Confirm Pickup
                    </button>
                  )}
                  {loan.status === "ON_LOAN" && item && (
                    <button
                      type="button"
                      className="btn btn-secondary"
                      onClick={() => handleOpenAction("RETURN", loan.id)}
                      aria-label={`Receive return for loan ${loan.id}`}
                    >
                      Receive Return
                    </button>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      )}

      {/* Confirmation Modal for Pickup/Return */}
      {activeAction && activeLoan && activeEquipment && (
        <div
          className="modal-backdrop"
          role="dialog"
          aria-modal="true"
          aria-labelledby="loan-action-title"
          ref={modalRef}
        >
          <div className="modal-content">
            <h3 id="loan-action-title" className="card-title">
              {activeAction.type === "PICKUP"
                ? "Confirm Equipment Pickup"
                : "Receive Equipment Return"}
            </h3>

            <div style={{ marginBottom: "1rem", fontSize: "0.9rem" }}>
              <div>
                <strong>Equipment:</strong> {activeEquipment.label} (v
                {activeEquipment.version})
              </div>
              <div>
                <strong>Loan ID:</strong> {activeLoan.id}
              </div>
              {activeAction.type === "RETURN" && (
                <div
                  style={{
                    marginTop: "0.5rem",
                    color: "var(--amber-tag-text)",
                  }}
                >
                  <em>
                    Note: Receiving this return will place the equipment into{" "}
                    <strong>AWAITING_INSPECTION</strong> until inspected by a
                    volunteer.
                  </em>
                </div>
              )}
            </div>

            {/* Modal-internal conflict recovery */}
            {conflictMessage && (
              <div
                className="banner banner-conflict"
                role="alert"
                style={{ marginBottom: "1rem" }}
              >
                <div>{conflictMessage}</div>
              </div>
            )}

            {/* Modal-internal uncertain retry recovery */}
            {uncertainRetry && (
              <div
                className="banner banner-conflict"
                role="alert"
                style={{ marginBottom: "1rem" }}
              >
                <div>
                  <strong>Uncertain Network Response:</strong>
                  <div>{uncertainRetry.description} interrupted.</div>
                  <div style={{ fontSize: "0.8rem", marginTop: "0.25rem" }}>
                    Retaining original Idempotency-Key{" "}
                    <code>{uncertainRetry.idempotencyKey.slice(0, 16)}...</code>
                    .
                  </div>
                </div>
                {onRetryUncertain && (
                  <button
                    type="button"
                    className="btn btn-primary"
                    onClick={onRetryUncertain}
                    disabled={isRetryingUncertain}
                    style={{ marginTop: "0.5rem" }}
                  >
                    {isRetryingUncertain
                      ? "Retrying..."
                      : "Retry with Original Key"}
                  </button>
                )}
              </div>
            )}

            {!isEligible && (
              <div
                className="banner banner-error"
                role="alert"
                style={{ marginBottom: "1rem" }}
              >
                Loan or equipment state has changed (Loan: {activeLoan.status},
                Item: {activeEquipment.state}). This action is no longer
                eligible.
              </div>
            )}

            {(actionError || serverError) && (
              <div
                className="banner banner-error"
                role="alert"
                style={{ marginBottom: "1rem" }}
              >
                {actionError || serverError}
              </div>
            )}

            <form onSubmit={handleSubmitAction} noValidate>
              <div className="confirmation-box">
                <input
                  id="loan-action-approval"
                  ref={checkboxRef}
                  type="checkbox"
                  className="confirmation-checkbox"
                  checked={humanApproved}
                  onChange={(e) => {
                    setHumanApproved(e.target.checked);
                    setActionError(null);
                    onClearError?.();
                  }}
                  disabled={isSubmitting || !isEligible}
                  required
                />
                <label
                  htmlFor="loan-action-approval"
                  className="confirmation-label"
                >
                  {activeAction.type === "PICKUP"
                    ? `I verify that ${activeEquipment.label} has been handed over in person at Velachery Community Room.`
                    : `I verify that ${activeEquipment.label} has been returned to the equipment room.`}
                </label>
              </div>

              <div
                style={{
                  display: "flex",
                  gap: "0.75rem",
                  justifyContent: "flex-end",
                }}
              >
                <button
                  type="button"
                  className="btn btn-secondary"
                  onClick={handleCloseAction}
                  disabled={isSubmitting}
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  className="btn btn-primary"
                  disabled={isSubmitting || !humanApproved || !isEligible}
                >
                  {isSubmitting
                    ? "Submitting..."
                    : activeAction.type === "PICKUP"
                      ? "Confirm Pickup"
                      : "Receive Return"}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
};
