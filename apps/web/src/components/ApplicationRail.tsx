import { FilePlus2 } from "lucide-react";

import { ApplicationRecord } from "../api";

type ApplicationRailProps = {
  applications: ApplicationRecord[];
  selectedId: string | null;
  onAdd: () => void;
  onSelect: (applicationId: string) => void;
  statusLabel: (status: string) => string;
};

export default function ApplicationRail({
  applications,
  selectedId,
  onAdd,
  onSelect,
  statusLabel
}: ApplicationRailProps) {
  return (
    <aside className="rail" aria-label="Applications">
      <div className="rail__header">
        <h1>CareerOps</h1>
        <button type="button" onClick={onAdd}>
          <FilePlus2 aria-hidden="true" size={16} /> Add application
        </button>
      </div>
      {applications.length === 0 ? (
        <p className="empty">No applications yet. Add a JD to start a packet.</p>
      ) : (
        <ul className="application-list">
          {applications.map((application) => (
            <li key={application.id}>
              <button
                type="button"
                className={
                  application.id === selectedId
                    ? "application-list__item is-selected"
                    : "application-list__item"
                }
                aria-pressed={application.id === selectedId}
                onClick={() => onSelect(application.id)}
              >
                <strong>{application.company}</strong>
                <span>{application.role_title}</span>
                <small>{statusLabel(application.status)}</small>
              </button>
            </li>
          ))}
        </ul>
      )}
    </aside>
  );
}
