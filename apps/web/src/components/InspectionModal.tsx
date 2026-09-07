import React, { useState, useEffect, useRef } from "react";
import type {
  Equipment,
  InspectionOutcome,
  EquipmentInspectionBody,
} from "../types/api";
import { useModalFocusTrap } from "../hooks/useModalFocusTrap";

interface InspectionModalProps {
  item: Equipment;
  onConfirm: (
    equipmentId: string,
    body: EquipmentInspectionBody,
  ) => Promise<boolean>;
  onClose: () => void;
  isSubmitting?: boolean;
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
}

export const InspectionModal: React.FC<InspectionModalProps> = ({
  item,
  onConfirm,
  onClose,
  isSubmitting = false,
  conflictMessage,
  uncertainRetry,
  onRetryUncertain,
  isRetryingUncertain = false,
  error: serverError,
  onClearError,
}) => {
  const [outcome, setOutcome] = useState<InspectionOutcome>("AVAILABLE");
  // Explicit human confirmation - never prechecked!
  const [humanApproved, setHumanApproved] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const modalRef = useRef<HTMLDivElement>(null);
  const selectRef = useRef<HTMLSelectElement>(null);
  const submittingRef = useRef(false);
  const prevVersionRef = useRef<number>(item.version);

  // Monitor version changes from snapshot refresh (e.g. on 409 conflict)
  useEffect(() => {
    if (prevVersionRef.current !== item.version) {
      setHumanApproved(false);
      prevVersionRef.current = item.version;
    }
  }, [item.version]);

  // Shared accessible focus trap, Escape listener, and focus restoration
  useModalFocusTrap({
    modalRef,
    initialFocusRef: selectRef,
    onClose,
    isDisabled: isSubmitting || isRetryingUncertain,
  });

  const isEligible =
    item.state === "AWAITING_INSPECTION" ||
    item.state === "REPAIR" ||
    item.state === "QUARANTINED";

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (isSubmitting || submittingRef.current || !isEligible) return;
    setError(null);

    if (!humanApproved) {
      setError(
        "Human volunteer affirmation is strictly required for all equipment inspection outcomes.",
      );
      return;
    }

    submittingRef.current = true;
    try {
      const success = await onConfirm(item.id, {
        expected_equipment_version: item.version,
        outcome,
        human_approved: true,
      });
      if (success) {
        onClose();
      }
    } finally {
      submittingRef.current = false;
    }
  };

  return (
    <div
      className="modal-backdrop"
      role="dialog"
      aria-modal="true"
      aria-labelledby="inspect-title"
      ref={modalRef}
    >
      <div className="modal-content">
        <h2 id="inspect-title" className="card-title">
          Equipment Inspection: {item.label}
        </h2>

        <div style={{ marginBottom: "1rem", fontSize: "0.9rem" }}>
          <div>
            <strong>Equipment ID:</strong> <code>{item.id}</code>
          </div>
          <div>
            <strong>Kind:</strong> {item.kind}
          </div>
          <div>
            <strong>Current State:</strong> {item.state} (v{item.version})
          </div>
        </div>

        {/* Modal-internal conflict banner */}
        {conflictMessage && (
          <div
            className="banner banner-conflict"
            role="alert"
            style={{ marginBottom: "1rem" }}
          >
            <strong>Competing Update Conflict (409):</strong>
            <div>{conflictMessage}</div>
            <div style={{ fontSize: "0.8rem", marginTop: "0.25rem" }}>
              Equipment refreshed to v{item.version}. Prior affirmation was
              cleared. Please re-affirm to confirm.
            </div>
          </div>
        )}

        {/* Modal-internal uncertain retry banner */}
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
                <code>{uncertainRetry.idempotencyKey.slice(0, 16)}...</code>.
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
            Equipment is no longer in AWAITING_INSPECTION state (current state:{" "}
            {item.state}). Action is not eligible.
          </div>
        )}

        {(error || serverError) && (
          <div
            className="banner banner-error"
            role="alert"
            style={{ marginBottom: "1rem" }}
          >
            {error || serverError}
          </div>
        )}

        <form onSubmit={handleSubmit} noValidate>
          <div className="form-group">
            <label htmlFor="outcome-select" className="form-label">
              Inspection Determination
            </label>
            <select
              id="outcome-select"
              ref={selectRef}
              className="form-select"
              value={outcome}
              onChange={(e) => {
                setOutcome(e.target.value as InspectionOutcome);
                // Reset approval when determination outcome changes!
                setHumanApproved(false);
                setError(null);
                onClearError?.();
              }}
              disabled={isSubmitting || !isEligible}
            >
              <option value="AVAILABLE">
                AVAILABLE — Mechanically sound and sanitized for loan
              </option>
              <option value="REPAIR">
                REPAIR — Requires maintenance or replacement part
              </option>
              <option value="QUARANTINED">
                QUARANTINED — Unsafe or damaged, withheld from circulation
              </option>
            </select>
          </div>

          <div className="confirmation-box">
            <input
              id="inspect-human-approval"
              type="checkbox"
              className="confirmation-checkbox"
              checked={humanApproved}
              onChange={(e) => {
                setHumanApproved(e.target.checked);
                setError(null);
                onClearError?.();
              }}
              disabled={isSubmitting || !isEligible}
              required
            />
            <label
              htmlFor="inspect-human-approval"
              className="confirmation-label"
            >
              I affirm that I have physically inspected this equipment item and
              manually decided its mechanical readiness outcome:{" "}
              <strong>{outcome}</strong>.
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
              onClick={onClose}
              disabled={isSubmitting}
            >
              Cancel
            </button>
            <button
              type="submit"
              className="btn btn-primary"
              disabled={isSubmitting || !humanApproved || !isEligible}
            >
              {isSubmitting ? "Submitting Inspection..." : "Record Inspection"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
};
