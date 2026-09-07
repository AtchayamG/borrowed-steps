import React from "react";

interface HeaderProps {
  agentMode?: string;
  milestone?: string;
}

export const Header: React.FC<HeaderProps> = ({
  agentMode = "not_implemented",
  milestone = "M1",
}) => {
  return (
    <header className="app-header">
      <div className="header-content">
        <div>
          <div className="header-title-row">
            <h1 className="header-title">Borrowed Steps</h1>
            <span className="header-badge">{milestone} Local Workflow</span>
          </div>
          <p className="header-sub">
            Velachery Community Equipment Room &bull; 12 Cross Road, Velachery,
            Chennai
          </p>
        </div>
        <div className="header-status">
          <span>
            Agent Mode: <strong>{agentMode}</strong>
          </span>
          <span>&bull;</span>
          <span>Human-in-the-Loop</span>
        </div>
      </div>
    </header>
  );
};
