import React from "react";
import type {
  CoordinationTask,
  Loan,
  BorrowRequest,
  Equipment,
} from "../types/api";

export interface CoordinationSectionProps {
  tasks: CoordinationTask[];
  loans: Loan[];
  requests: BorrowRequest[];
  equipment: Equipment[];
  lastSyncedAt?: Date | null;
  isLoading?: boolean;
}

export const CoordinationSection: React.FC<CoordinationSectionProps> = ({
  tasks,
  loans,
  requests,
  equipment,
  lastSyncedAt,
  isLoading = false,
}) => {
  const activeTasks = tasks.filter(
    (t) => t.status === "PENDING" || t.status === "DUE",
  );
  const resolvedTasks = tasks.filter((t) => t.status === "RESOLVED");

  const getLoan = (loanId: string): Loan | undefined =>
    loans.find((l) => l.id === loanId);

  const getRequestForLoan = (loan?: Loan): BorrowRequest | undefined => {
    if (!loan) return undefined;
    return requests.find((r) => r.id === loan.request_id);
  };

  const getEquipmentForLoan = (loan?: Loan): Equipment | undefined => {
    if (!loan) return undefined;
    return equipment.find((e) => e.id === loan.equipment_id);
  };

  return (
    <section
      className="card coordination-section"
      aria-labelledby="coordination-heading"
      style={{ opacity: isLoading ? 0.75 : 1 }}
    >
      <div className="card-title">
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: "0.5rem",
            flexWrap: "wrap",
          }}
        >
          <h2
            id="coordination-heading"
            style={{ fontSize: "1.15rem", margin: 0 }}
          >
            Coordination
          </h2>
          <span className="tag tag-reserved" style={{ fontSize: "0.75rem" }}>
            In-App Tasks
          </span>
        </div>
        {lastSyncedAt && (
          <span
            style={{
              fontSize: "0.8125rem",
              color: "var(--text-muted)",
              fontWeight: 400,
            }}
            data-testid="coordination-sync-time"
          >
            Reflects sync at: {lastSyncedAt.toLocaleTimeString()}
          </span>
        )}
      </div>

      <p
        className="card-sub coordination-scope-notice"
        style={{
          fontSize: "0.85rem",
          color: "var(--text-muted)",
          marginBottom: "1rem",
        }}
      >
        In-app operational tasks for volunteers. Reminders and due notices are
        tracked within this workspace only (no external email/SMS).
      </p>

      {tasks.length === 0 ? (
        <div
          className="empty-state"
          data-testid="coordination-empty"
          style={{
            padding: "1.25rem 0",
            color: "var(--text-muted)",
            fontSize: "0.9rem",
          }}
        >
          No coordination tasks in this workspace. Tasks appear automatically
          when equipment is reserved.
        </div>
      ) : (
        <>
          {/* Active Tasks Group */}
          <div style={{ marginBottom: "1.25rem" }}>
            <h3
              id="active-tasks-heading"
              style={{
                fontSize: "0.95rem",
                fontWeight: 600,
                color: "var(--indigo-950)",
                marginBottom: "0.6rem",
              }}
            >
              Active Tasks ({activeTasks.length})
            </h3>

            {activeTasks.length === 0 ? (
              <p
                className="text-muted"
                data-testid="no-active-tasks"
                style={{ fontSize: "0.875rem", padding: "0.5rem 0" }}
              >
                No active coordination tasks. All tasks resolved or none
                currently pending.
              </p>
            ) : (
              <div
                role="list"
                aria-labelledby="active-tasks-heading"
                style={{
                  display: "flex",
                  flexDirection: "column",
                  gap: "0.75rem",
                }}
              >
                {activeTasks.map((task) => {
                  const loan = getLoan(task.loan_id);
                  const req = getRequestForLoan(loan);
                  const equip = getEquipmentForLoan(loan);

                  if (task.kind === "PICKUP_DUE") {
                    return (
                      <div
                        key={task.id}
                        role="listitem"
                        className="item-row coordination-task-active"
                        data-testid={`task-row-${task.id}`}
                      >
                        <div className="item-info">
                          <div
                            className="item-title"
                            style={{
                              display: "flex",
                              alignItems: "center",
                              gap: "0.5rem",
                              flexWrap: "wrap",
                            }}
                          >
                            <span>Arrange pickup</span>
                            <span
                              style={{
                                fontSize: "0.8rem",
                                color: "var(--text-muted)",
                              }}
                            >
                              &bull; Task #{task.id}
                            </span>
                          </div>

                          <div
                            className="item-meta"
                            style={{ marginTop: "0.35rem" }}
                          >
                            <span>
                              Pickup Location:{" "}
                              <strong>
                                {req?.pickup_location || "Location pending"}
                              </strong>
                            </span>
                            <span>&bull;</span>
                            <span>
                              Borrower:{" "}
                              <strong>
                                {req?.borrower_label || "Unknown"}
                              </strong>
                            </span>
                            <span>&bull;</span>
                            <span>
                              Equipment:{" "}
                              <strong>
                                {equip
                                  ? `${equip.label} (${equip.kind})`
                                  : "Equipment pending"}
                              </strong>
                            </span>
                          </div>

                          <div
                            style={{
                              fontSize: "0.8125rem",
                              color: "var(--indigo-900)",
                              marginTop: "0.4rem",
                            }}
                          >
                            {task.status === "PENDING" ? (
                              <span>
                                Pickup is immediately actionable. A volunteer
                                records pickup in person in the{" "}
                                <strong>Loan List</strong> below when borrower
                                arrives.
                              </span>
                            ) : (
                              <span>
                                Arrange pickup with the borrower, then record
                                the handover in the <strong>Loan List</strong>{" "}
                                below.
                              </span>
                            )}
                          </div>
                        </div>

                        <div className="item-actions">
                          <span
                            className={
                              task.status === "DUE"
                                ? "tag tag-due"
                                : "tag tag-pending"
                            }
                            data-testid={`task-status-${task.id}`}
                          >
                            {task.status}
                          </span>
                        </div>
                      </div>
                    );
                  }

                  // RETURN_DUE task
                  const isDue = task.status === "DUE";
                  const taskTitle = isDue ? "Return due" : "Return reminder";

                  return (
                    <div
                      key={task.id}
                      role="listitem"
                      className="item-row coordination-task-active"
                      data-testid={`task-row-${task.id}`}
                    >
                      <div className="item-info">
                        <div
                          className="item-title"
                          style={{
                            display: "flex",
                            alignItems: "center",
                            gap: "0.5rem",
                            flexWrap: "wrap",
                          }}
                        >
                          <span>{taskTitle}</span>
                          <span
                            style={{
                              fontSize: "0.8rem",
                              color: "var(--text-muted)",
                            }}
                          >
                            &bull; Task #{task.id}
                          </span>
                        </div>

                        <div
                          className="item-meta"
                          style={{ marginTop: "0.35rem" }}
                        >
                          <span>
                            Due Instant: <strong>{task.due_at}</strong>
                          </span>
                          <span>&bull;</span>
                          <span>
                            Borrower:{" "}
                            <strong>{req?.borrower_label || "Unknown"}</strong>
                          </span>
                          <span>&bull;</span>
                          <span>
                            Equipment:{" "}
                            <strong>
                              {equip
                                ? `${equip.label} (${equip.kind})`
                                : "Equipment pending"}
                            </strong>
                          </span>
                        </div>

                        <div
                          style={{
                            fontSize: "0.8125rem",
                            color: isDue
                              ? "var(--red-tag-text)"
                              : "var(--indigo-900)",
                            marginTop: "0.4rem",
                          }}
                        >
                          {isDue ? (
                            <span>
                              Equipment return is due. A volunteer will receive
                              return and inspect equipment in the{" "}
                              <strong>Loan List</strong> below.
                            </span>
                          ) : (
                            <span>
                              Upcoming return scheduled for {task.due_at}.
                              Active on-loan item.
                            </span>
                          )}
                        </div>
                      </div>

                      <div className="item-actions">
                        <span
                          className={isDue ? "tag tag-due" : "tag tag-pending"}
                          data-testid={`task-status-${task.id}`}
                        >
                          {task.status}
                        </span>
                      </div>
                    </div>
                  );
                })}
              </div>
            )}
          </div>

          {/* Resolved Tasks Group - Collapsible & Inspectable */}
          <details
            className="coordination-resolved-details"
            data-testid="resolved-tasks-details"
            style={{
              borderTop: "1px solid var(--border-subtle)",
              paddingTop: "0.75rem",
              marginTop: "0.5rem",
            }}
          >
            <summary
              style={{
                cursor: "pointer",
                fontWeight: 600,
                fontSize: "0.9rem",
                color: "var(--indigo-950)",
                userSelect: "none",
              }}
            >
              Resolved Coordination Tasks ({resolvedTasks.length})
            </summary>

            {resolvedTasks.length === 0 ? (
              <p
                className="text-muted"
                style={{ fontSize: "0.85rem", marginTop: "0.5rem" }}
              >
                No resolved tasks in this session.
              </p>
            ) : (
              <div
                role="list"
                aria-label="Resolved coordination tasks"
                style={{
                  display: "flex",
                  flexDirection: "column",
                  gap: "0.5rem",
                  marginTop: "0.75rem",
                }}
              >
                {resolvedTasks.map((task) => {
                  const loan = getLoan(task.loan_id);
                  const req = getRequestForLoan(loan);
                  const equip = getEquipmentForLoan(loan);

                  return (
                    <div
                      key={task.id}
                      role="listitem"
                      className="item-row coordination-task-resolved"
                      style={{ opacity: 0.85 }}
                      data-testid={`task-row-${task.id}`}
                    >
                      <div className="item-info">
                        <div
                          className="item-title"
                          style={{ fontSize: "0.95rem" }}
                        >
                          {task.kind === "PICKUP_DUE"
                            ? "Pickup task resolved"
                            : "Return task resolved"}{" "}
                          &bull; Task #{task.id}
                        </div>
                        <div
                          className="item-meta"
                          style={{ fontSize: "0.8rem" }}
                        >
                          <span>
                            Borrower:{" "}
                            <strong>{req?.borrower_label || "Unknown"}</strong>
                          </span>
                          <span>&bull;</span>
                          <span>Item: {equip?.label || "Equipment"}</span>
                          {task.kind === "PICKUP_DUE" ? (
                            <>
                              <span>&bull;</span>
                              <span>
                                Location: {req?.pickup_location || "N/A"}
                              </span>
                            </>
                          ) : (
                            <>
                              <span>&bull;</span>
                              <span>Due: {task.due_at}</span>
                            </>
                          )}
                          <span>&bull;</span>
                          <span>
                            This task is resolved in the latest snapshot.
                          </span>
                        </div>
                      </div>
                      <div className="item-actions">
                        <span
                          className="tag tag-resolved"
                          data-testid={`task-status-${task.id}`}
                        >
                          RESOLVED
                        </span>
                      </div>
                    </div>
                  );
                })}
              </div>
            )}
          </details>
        </>
      )}
    </section>
  );
};
