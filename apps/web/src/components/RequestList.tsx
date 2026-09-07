import React from "react";
import type { BorrowRequest } from "../types/api";

interface RequestListProps {
  requests: BorrowRequest[];
  onSelectAllocate: (request: BorrowRequest) => void;
  isLoading?: boolean;
}

export const RequestList: React.FC<RequestListProps> = ({
  requests,
  onSelectAllocate,
  isLoading = false,
}) => {
  const getStatusTag = (status: string) => {
    switch (status) {
      case "REQUESTED":
        return "tag tag-awaiting_inspection";
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

  const formatDate = (isoStr: string) => {
    try {
      return new Date(isoStr).toLocaleString();
    } catch {
      return isoStr;
    }
  };

  return (
    <div className="card" aria-labelledby="requests-heading">
      <h2 id="requests-heading" className="card-title">
        Community Requests ({requests.length})
      </h2>
      {isLoading && requests.length === 0 ? (
        <div className="empty-state">Loading requests...</div>
      ) : requests.length === 0 ? (
        <div className="empty-state">No requests currently recorded.</div>
      ) : (
        <div role="list">
          {requests.map((req) => (
            <div key={req.id} role="listitem" className="item-row">
              <div className="item-info">
                <div className="item-title">{req.borrower_label}</div>
                <div className="item-meta">
                  <span>
                    Kind: <strong>{req.equipment_kind}</strong>
                  </span>
                  <span>&bull;</span>
                  <span>Due: {formatDate(req.due_at)}</span>
                  <span>&bull;</span>
                  <span>Pickup: {req.pickup_location}</span>
                </div>
              </div>
              <div className="item-actions">
                <span className={getStatusTag(req.status)}>{req.status}</span>
                {req.status === "REQUESTED" && (
                  <button
                    type="button"
                    className="btn btn-primary"
                    onClick={() => onSelectAllocate(req)}
                    aria-label={`Allocate equipment for ${req.borrower_label}`}
                  >
                    Allocate Equipment
                  </button>
                )}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
};
