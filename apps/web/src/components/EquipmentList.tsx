import React from "react";
import type { Equipment } from "../types/api";

interface EquipmentListProps {
  equipment: Equipment[];
  onSelectInspect: (item: Equipment) => void;
  isLoading?: boolean;
}

export const EquipmentList: React.FC<EquipmentListProps> = ({
  equipment,
  onSelectInspect,
  isLoading = false,
}) => {
  const getTagClass = (state: string) => {
    switch (state) {
      case "AVAILABLE":
        return "tag tag-available";
      case "AWAITING_INSPECTION":
        return "tag tag-awaiting_inspection";
      case "REPAIR":
        return "tag tag-repair";
      case "QUARANTINED":
        return "tag tag-quarantined";
      case "RESERVED":
        return "tag tag-reserved";
      case "ON_LOAN":
        return "tag tag-on_loan";
      default:
        return "tag tag-closed";
    }
  };

  const isInspectable = (state: string) => {
    return ["AWAITING_INSPECTION", "REPAIR", "QUARANTINED"].includes(state);
  };

  return (
    <div className="card" aria-labelledby="equipment-heading">
      <h2 id="equipment-heading" className="card-title">
        Equipment Inventory ({equipment.length})
      </h2>
      {isLoading && equipment.length === 0 ? (
        <div className="empty-state">Loading equipment items...</div>
      ) : equipment.length === 0 ? (
        <div className="empty-state">No equipment registered in workspace.</div>
      ) : (
        <div role="list">
          {equipment.map((item) => (
            <div key={item.id} role="listitem" className="item-row">
              <div className="item-info">
                <div className="item-title">{item.label}</div>
                <div className="item-meta">
                  <span>
                    Kind: <strong>{item.kind}</strong>
                  </span>
                  <span>&bull;</span>
                  <span>
                    ID: <code>{item.id}</code>
                  </span>
                  <span>&bull;</span>
                  <span>v{item.version}</span>
                </div>
              </div>
              <div className="item-actions">
                <span className={getTagClass(item.state)}>
                  {item.state.replace("_", " ")}
                </span>
                {isInspectable(item.state) && (
                  <button
                    type="button"
                    className="btn btn-secondary"
                    onClick={() => onSelectInspect(item)}
                    aria-label={`Inspect ${item.label}`}
                  >
                    Inspect Equipment
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
