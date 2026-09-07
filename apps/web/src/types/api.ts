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

export interface Snapshot {
  equipment: Equipment[];
  requests: BorrowRequest[];
  loans: Loan[];
  events: DomainEvent[];
  agent_mode: "not_implemented";
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
  agent_mode: string;
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
