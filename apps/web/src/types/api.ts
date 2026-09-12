export type EquipmentKind = "WHEELCHAIR" | "WALKER" | "CRUTCHES";

export type EquipmentState =
  | "AVAILABLE"
  | "RESERVED"
  | "ON_LOAN"
  | "AWAITING_INSPECTION"
  | "REPAIR"
  | "QUARANTINED";

export type RequestStatus =
  "REQUESTED" | "RESERVED" | "ON_LOAN" | "RETURNED" | "CLOSED";

export type LoanStatus = "RESERVED" | "ON_LOAN" | "RETURNED" | "CLOSED";

export type InspectionOutcome = "AVAILABLE" | "REPAIR" | "QUARANTINED";

export interface Equipment {
  id: string;
  label: string;
  kind: EquipmentKind;
  state: EquipmentState;
  version: number;
}

export interface BorrowRequest {
  id: string;
  borrower_label: string;
  equipment_kind: EquipmentKind;
  pickup_location: string;
  due_at: string;
  status: RequestStatus;
  created_at: string;
}

export interface Loan {
  id: string;
  request_id: string;
  equipment_id: string;
  status: LoanStatus;
  due_at: string;
  created_at: string;
}

export interface DomainEvent {
  id: string;
  entity_type: string;
  entity_id: string;
  action: string;
  at: string;
}

export type CoordinationTaskKind = "PICKUP_DUE" | "RETURN_DUE";

export type CoordinationTaskStatus = "PENDING" | "DUE" | "RESOLVED";

export interface CoordinationTask {
  id: string;
  loan_id: string;
  kind: CoordinationTaskKind;
  status: CoordinationTaskStatus;
  due_at: string;
  created_at: string;
}

export type AgentMode =
  "disabled" | "strands_ollama" | "strands_groq" | "not_implemented";

export interface Snapshot {
  equipment: Equipment[];
  requests: BorrowRequest[];
  loans: Loan[];
  events: DomainEvent[];
  agent_mode: AgentMode;
  tasks: CoordinationTask[];
}

export interface WorkspaceCreationResponse {
  workspace: {
    id: string;
  };
  snapshot: Snapshot;
}

export interface HealthResponse {
  status: string;
  milestone: string;
  agent_mode: AgentMode | string;
}

export interface IntakeInterpretRequest {
  text: string;
}

export type MissingField =
  "borrower_label" | "equipment_kind" | "pickup_location" | "due_at";

export interface IntakeDraft {
  borrower_label: string | null;
  equipment_kind: EquipmentKind | null;
  pickup_location: string | null;
  due_at: string | null;
}

export interface IntakeProvenance {
  framework: "strands";
  provider: "ollama" | "groq";
  model: "llama3.2:3b" | "openai/gpt-oss-20b";
  inventory_tool_calls: number;
  completed_at: string;
}

export interface IntakeInterpretResponse {
  draft: IntakeDraft;
  missing_fields: MissingField[];
  provenance: IntakeProvenance;
}

export interface ApiErrorDetail {
  code: string;
  message: string;
}

export interface ApiErrorResponse {
  error: ApiErrorDetail;
}

export interface CreateRequestBody {
  borrower_label: string;
  equipment_kind: EquipmentKind;
  pickup_location: string;
  due_at: string;
}

export interface CreateReservationBody {
  request_id: string;
  equipment_id: string;
  expected_equipment_version: number;
  human_approved: true;
}

export interface LoanPickupBody {
  expected_equipment_version: number;
  human_approved: true;
}

export interface LoanReturnBody {
  expected_equipment_version: number;
  human_approved: true;
}

export interface EquipmentInspectionBody {
  expected_equipment_version: number;
  outcome: InspectionOutcome;
  human_approved: true;
}
