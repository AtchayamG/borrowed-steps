import React, { useState, useEffect, useRef } from "react";
import type {
  BorrowRequest,
  Equipment,
  CreateReservationBody,
} from "../types/api";
import { useModalFocusTrap } from "../hooks/useModalFocusTrap";

interface AllocationModalProps {
  request: BorrowRequest;
  availableEquipment: Equipment[];
  onConfirm: (body: CreateReservationBody) => Promise<boolean>;
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

export const AllocationModal: React.FC<AllocationModalProps> = ({
  request,
  availableEquipment,
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
  // Eligible matching items strictly from latest equipment list
  const matchingItems = availableEquipment.filter(
    (e) => e.kind === request.equipment_kind && e.state === "AVAILABLE",
  );

  const [selectedEquipmentId, setSelectedEquipmentId] = useState(
    matchingItems[0]?.id || "",
  );
  // Strictly unchecked by default - no prechecked approvals!
  const [humanApproved, setHumanApproved] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [invalidatedNotice, setInvalidatedNotice] = useState<string | null>(
    null,
  );

  const modalRef = useRef<HTMLDivElement>(null);
  const selectRef = useRef<HTMLSelectElement>(null);
  const closeButtonRef = useRef<HTMLButtonElement>(null);
  const submittingRef = useRef(false);
  const prevVersionRef = useRef<number | undefined>(undefined);

  // Selected item must resolve strictly within eligible matching items
  const selectedItem = matchingItems.find((e) => e.id === selectedEquipmentId);

  // Monitor equipment validity & version changes on snapshot updates (e.g. 409 recovery)
  useEffect(() => {
    if (selectedItem) {
      if (
        prevVersionRef.current !== undefined &&
        prevVersionRef.current !== selectedItem.version
      ) {
        // Item version changed
        setHumanApproved(false);
        setInvalidatedNotice(
          `Equipment ${selectedItem.label} updated to v${selectedItem.version}. Please re-affirm allocation.`,
        );
      }
      prevVersionRef.current = selectedItem.version;
    } else if (matchingItems.length > 0) {
      // Previously selected item is no longer in matching items (became unavailable)
      setSelectedEquipmentId(matchingItems[0]!.id);
      setHumanApproved(false);
      setInvalidatedNotice(
        "Previously selected equipment is no longer available. Please select another available item and re-affirm approval.",
      );
    } else {
      setHumanApproved(false);
    }
  }, [matchingItems, selectedItem]);

  // Shared accessible focus trap, Escape listener, and focus restoration
  useModalFocusTrap({
    modalRef,
    initialFocusRef: selectRef,
    onClose,
    isDisabled: isSubmitting || isRetryingUncertain,
  });

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (isSubmitting || submittingRef.current) return;
    setError(null);

    if (!selectedItem) {
      setError(
        "Please select an available, eligible equipment item to allocate.",
      );
      return;
    }

    if (!humanApproved) {
      setError(
        "Explicit human volunteer approval is required before allocating community equipment.",
      );
      return;
    }

    submittingRef.current = true;
    try {
      const success = await onConfirm({
        request_id: request.id,
        equipment_id: selectedItem.id,
        expected_equipment_version: selectedItem.version,
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
      aria-labelledby="alloc-title"
      ref={modalRef}
    >
      <div className="modal-content">
        <h2 id="alloc-title" className="card-title">
          Allocate Equipment
        </h2>

        <div style={{ marginBottom: "1rem", fontSize: "0.9rem" }}>
          <div>
            <strong>Borrower:</strong> {request.borrower_label}
          </div>
          <div>
            <strong>Requested Kind:</strong> {request.equipment_kind}
          </div>
          <div>
            <strong>Pickup Location:</strong> {request.pickup_location}
          </div>
        </div>

        {/* Modal-internal conflict recovery */}
        {conflictMessage && (
          <div
            className="banner banner-conflict"
            role="alert"
            style={{ marginBottom: "1rem" }}
          >
            <strong>Competing Update Conflict (409):</strong>
            <div>{conflictMessage}</div>
            <div style={{ fontSize: "0.8rem", marginTop: "0.25rem" }}>
              Snapshot reloaded. Please check the updated items and re-affirm.
            </div>
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

        {(error || serverError) && (
          <div
            className="banner banner-error"
            role="alert"
            style={{ marginBottom: "1rem" }}
          >
            {error || serverError}
          </div>
        )}

        {invalidatedNotice && (
          <div
            className="banner banner-conflict"
            role="alert"
            style={{ marginBottom: "1rem" }}
          >
            {invalidatedNotice}
          </div>
        )}

        {matchingItems.length === 0 ? (
          <div
            className="banner banner-conflict"
            style={{ marginBottom: "1rem" }}
          >
            No ready {request.equipment_kind} items currently marked AVAILABLE.
            Items may be on loan or awaiting inspection.
          </div>
        ) : (
          <form onSubmit={handleSubmit} noValidate>
            <div className="form-group">
              <label htmlFor="equipment-select" className="form-label">
                Select Available {request.equipment_kind}
              </label>
              <select
                id="equipment-select"
                ref={selectRef}
                className="form-select"
                value={selectedEquipmentId}
                onChange={(e) => {
                  setSelectedEquipmentId(e.target.value);
                  setHumanApproved(false); // Reset approval on selection change
                  setInvalidatedNotice(null);
                  setError(null);
                  onClearError?.();
                }}
                disabled={isSubmitting}
              >
                {matchingItems.map((item) => (
                  <option key={item.id} value={item.id}>
                    {item.label} (ID: {item.id}, v{item.version})
                  </option>
                ))}
              </select>
            </div>

            <div className="confirmation-box">
              <input
                id="human-alloc-approval"
                type="checkbox"
                className="confirmation-checkbox"
                checked={humanApproved}
                onChange={(e) => {
                  setHumanApproved(e.target.checked);
                  setError(null);
                  onClearError?.();
                }}
                disabled={isSubmitting}
                required
              />
              <label
                htmlFor="human-alloc-approval"
                className="confirmation-label"
              >
                I confirm explicit volunteer decision to allocate{" "}
                {selectedItem ? selectedItem.label : "equipment"} to{" "}
                {request.borrower_label}. (No automated prescription)
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
                disabled={
                  isSubmitting || !humanApproved || matchingItems.length === 0
                }
              >
                {isSubmitting ? "Confirming..." : "Confirm Allocation"}
              </button>
            </div>
          </form>
        )}

        {matchingItems.length === 0 && (
          <div style={{ display: "flex", justifyContent: "flex-end" }}>
            <button
              type="button"
              ref={closeButtonRef}
              className="btn btn-secondary"
              onClick={onClose}
            >
              Close
            </button>
          </div>
        )}
      </div>
    </div>
  );
};
