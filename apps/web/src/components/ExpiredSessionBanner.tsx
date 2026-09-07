import React from "react";

interface ExpiredSessionBannerProps {
  onStartWorkspace: () => void;
  isStarting?: boolean;
}

export const ExpiredSessionBanner: React.FC<ExpiredSessionBannerProps> = ({
  onStartWorkspace,
  isStarting = false,
}) => {
  return (
    <div className="banner banner-session" role="alert">
      <div>
        <strong>No Active Community Session (401):</strong>
        <div>
          Your session has expired or no workspace has been initialized for this
          browser. Start a synthetic workspace to seed the Velachery equipment
          inventory and coordinate requests.
        </div>
      </div>
      <button
        type="button"
        className="btn btn-primary"
        onClick={onStartWorkspace}
        disabled={isStarting}
      >
        {isStarting ? "Starting Workspace..." : "Start Synthetic Workspace"}
      </button>
    </div>
  );
};
