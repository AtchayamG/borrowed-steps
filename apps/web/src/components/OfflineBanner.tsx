import React from "react";

interface OfflineBannerProps {
  message: string;
  onRetry: () => void;
  isRetrying?: boolean;
}

export const OfflineBanner: React.FC<OfflineBannerProps> = ({
  message,
  onRetry,
  isRetrying = false,
}) => {
  return (
    <div className="banner banner-offline" role="alert">
      <div>
        <strong>Connection Error:</strong> {message}
        <div style={{ fontSize: "0.8rem", marginTop: "0.25rem" }}>
          Verify backend is running on <code>127.0.0.1:8000</code> with proxy
          configured.
        </div>
      </div>
      <button
        type="button"
        className="btn btn-secondary"
        onClick={onRetry}
        disabled={isRetrying}
      >
        {isRetrying ? "Connecting..." : "Retry Connection"}
      </button>
    </div>
  );
};
