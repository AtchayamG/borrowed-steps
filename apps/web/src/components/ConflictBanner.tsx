import React from "react";

interface ConflictBannerProps {
  message: string;
  onDismiss: () => void;
}

export const ConflictBanner: React.FC<ConflictBannerProps> = ({
  message,
  onDismiss,
}) => {
  return (
    <div className="banner banner-conflict" role="alert">
      <div>
        <strong>Competing Update Conflict (409):</strong>
        <div>{message}</div>
        <div style={{ fontSize: "0.8rem", marginTop: "0.35rem" }}>
          The equipment inventory has been re-synchronized. Your previously
          entered inputs have been preserved. Please review the updated status
          and retry.
        </div>
      </div>
      <button type="button" className="btn btn-secondary" onClick={onDismiss}>
        Acknowledge
      </button>
    </div>
  );
};
