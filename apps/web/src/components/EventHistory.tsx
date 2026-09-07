import React from "react";
import type { DomainEvent } from "../types/api";

interface EventHistoryProps {
  events: DomainEvent[];
  isLoading?: boolean;
}

export const EventHistory: React.FC<EventHistoryProps> = ({
  events,
  isLoading = false,
}) => {
  const formatTime = (iso: string) => {
    try {
      return (
        new Date(iso).toLocaleTimeString() +
        " (" +
        new Date(iso).toLocaleDateString() +
        ")"
      );
    } catch {
      return iso;
    }
  };

  return (
    <div className="card" aria-labelledby="events-heading">
      <h2 id="events-heading" className="card-title">
        Audit Log &amp; Event Stream ({events.length})
      </h2>
      {isLoading && events.length === 0 ? (
        <div className="empty-state">Loading audit events...</div>
      ) : events.length === 0 ? (
        <div className="empty-state">No domain events recorded yet.</div>
      ) : (
        <div role="feed" aria-label="Event Log">
          {events.map((evt) => (
            <article key={evt.id} className="event-item">
              <div
                style={{
                  display: "flex",
                  justifyContent: "space-between",
                  alignItems: "baseline",
                }}
              >
                <span style={{ fontWeight: 700, color: "var(--indigo-900)" }}>
                  {evt.action}
                </span>
                <span className="event-time">{formatTime(evt.at)}</span>
              </div>
              <div style={{ color: "var(--text-muted)", fontSize: "0.8rem" }}>
                Target:{" "}
                <code>
                  {evt.entity_type}:{evt.entity_id}
                </code>{" "}
                &bull; Event ID: <code>{evt.id}</code>
              </div>
            </article>
          ))}
        </div>
      )}
    </div>
  );
};
