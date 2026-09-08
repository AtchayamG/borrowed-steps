import React, { useState, useEffect } from "react";
import type {
  EquipmentKind,
  CreateRequestBody,
  IntakeDraft,
} from "../types/api";
import {
  isoToLocalInput,
  localInputToIso,
  getDefaultDueDate,
} from "../utils/dateTime";

interface RequestFormProps {
  onSubmit: (body: CreateRequestBody) => Promise<void>;
  isSubmitting?: boolean;
  draft?: IntakeDraft | null;
  onClearDraft?: () => void;
}

const DEFAULT_PICKUP =
  "Velachery Community Room, 12 Cross Road, Velachery, Chennai";

export const RequestForm: React.FC<RequestFormProps> = ({
  onSubmit,
  isSubmitting = false,
  draft = null,
  onClearDraft,
}) => {
  const [borrowerLabel, setBorrowerLabel] = useState(
    "Ananya R. (Velachery Resident)",
  );
  const [equipmentKind, setEquipmentKind] = useState<EquipmentKind | "">(
    "WHEELCHAIR",
  );
  const [pickupLocation, setPickupLocation] = useState(DEFAULT_PICKUP);
  const [dueDateTime, setDueDateTime] = useState(getDefaultDueDate());
  const [validationError, setValidationError] = useState<string | null>(null);
  const [isLocalSubmitting, setIsLocalSubmitting] = useState(false);
  const submittingRef = React.useRef(false);

  // When a draft is provided via deliberate 'Use draft', populate the form fields.
  // Missing/null fields remain empty strings so volunteer must complete them.
  useEffect(() => {
    if (draft) {
      setBorrowerLabel(draft.borrower_label ?? "");
      setEquipmentKind(draft.equipment_kind ?? "");
      setPickupLocation(draft.pickup_location ?? "");
      setDueDateTime(draft.due_at ? isoToLocalInput(draft.due_at) : "");
      setValidationError(null);
    }
  }, [draft]);

  const isPending = isSubmitting || isLocalSubmitting;

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (isPending || submittingRef.current) {
      return;
    }
    setValidationError(null);

    const trimmedLabel = borrowerLabel.trim();
    if (!trimmedLabel || trimmedLabel.length > 60) {
      setValidationError("Borrower label must be between 1 and 60 characters.");
      return;
    }

    if (!equipmentKind) {
      setValidationError("Please select an equipment kind.");
      return;
    }

    const trimmedLocation = pickupLocation.trim();
    if (!trimmedLocation || trimmedLocation.length > 120) {
      setValidationError(
        "Pickup location must be between 1 and 120 characters.",
      );
      return;
    }

    const dueAtIso = localInputToIso(dueDateTime);
    if (!dueAtIso) {
      setValidationError("Please specify a valid due date and time.");
      return;
    }

    const dueDate = new Date(dueDateTime);
    const now = new Date();

    if (dueDate <= now) {
      setValidationError("Due date must be in the future.");
      return;
    }

    const maxDue = new Date();
    maxDue.setDate(maxDue.getDate() + 30);
    if (dueDate > maxDue) {
      setValidationError("Due date cannot be more than 30 days ahead.");
      return;
    }

    submittingRef.current = true;
    setIsLocalSubmitting(true);
    try {
      await onSubmit({
        borrower_label: trimmedLabel,
        equipment_kind: equipmentKind,
        pickup_location: trimmedLocation,
        due_at: dueAtIso,
      });
      onClearDraft?.();
    } finally {
      submittingRef.current = false;
      setIsLocalSubmitting(false);
    }
  };

  return (
    <div className="card" aria-labelledby="request-form-heading">
      <h2 id="request-form-heading" className="card-title">
        Intake New Request
      </h2>
      <form onSubmit={handleSubmit} noValidate>
        {validationError && (
          <div
            className="banner banner-error"
            role="alert"
            style={{ marginBottom: "1rem" }}
          >
            {validationError}
          </div>
        )}

        <div className="form-group">
          <label htmlFor="borrower-label" className="form-label">
            Synthetic Borrower Label (1–60 chars)
          </label>
          <input
            id="borrower-label"
            type="text"
            className="form-input"
            value={borrowerLabel}
            maxLength={60}
            onChange={(e) => setBorrowerLabel(e.target.value)}
            disabled={isPending}
            required
            aria-describedby="borrower-help"
          />
          <span id="borrower-help" className="form-help">
            Synthetic identifier for testing only; no real medical or contact
            details.
          </span>
        </div>

        <div className="form-group">
          <label htmlFor="equipment-kind" className="form-label">
            Requested Equipment Kind
          </label>
          <select
            id="equipment-kind"
            className="form-select"
            value={equipmentKind}
            onChange={(e) => setEquipmentKind(e.target.value as EquipmentKind)}
            disabled={isPending}
          >
            <option value="">Select equipment kind...</option>
            <option value="WHEELCHAIR">WHEELCHAIR</option>
            <option value="WALKER">WALKER</option>
            <option value="CRUTCHES">CRUTCHES</option>
          </select>
        </div>

        <div className="form-group">
          <label htmlFor="pickup-location" className="form-label">
            Pickup Location (1–120 chars)
          </label>
          <input
            id="pickup-location"
            type="text"
            className="form-input"
            value={pickupLocation}
            maxLength={120}
            onChange={(e) => setPickupLocation(e.target.value)}
            disabled={isPending}
            required
          />
        </div>

        <div className="form-group">
          <label htmlFor="due-datetime" className="form-label">
            Requested Return Due Date & Time (Within 30 Days)
          </label>
          <input
            id="due-datetime"
            type="datetime-local"
            step="1"
            className="form-input"
            value={dueDateTime}
            onChange={(e) => setDueDateTime(e.target.value)}
            disabled={isPending}
            required
          />
        </div>

        <button
          type="submit"
          className="btn btn-primary"
          disabled={isPending}
          style={{ width: "100%" }}
        >
          {isPending ? "Registering Request..." : "Register Structured Request"}
        </button>
      </form>
    </div>
  );
};
