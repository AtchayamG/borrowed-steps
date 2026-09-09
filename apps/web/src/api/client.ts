import type {
  Snapshot,
  WorkspaceCreationResponse,
  BorrowRequest,
  Equipment,
  Loan,
  CreateRequestBody,
  CreateReservationBody,
  LoanPickupBody,
  LoanReturnBody,
  EquipmentInspectionBody,
  ApiErrorResponse,
  IntakeInterpretRequest,
  IntakeInterpretResponse,
} from "../types/api";
import { validateSnapshot } from "../utils/validation";

export class ApiClientError extends Error {
  status: number;
  code: string;
  isStructuredError: boolean;

  constructor(
    status: number,
    code: string,
    message: string,
    isStructuredError: boolean = false,
  ) {
    super(message);
    this.name = "ApiClientError";
    this.status = status;
    this.code = code;
    this.isStructuredError = isStructuredError;
  }
}

export function generateIdempotencyKey(): string {
  if (typeof crypto !== "undefined" && crypto.randomUUID) {
    return crypto.randomUUID();
  }
  // Fallback 16-character pseudo-random string
  return (
    "idemp_" +
    Math.random().toString(36).substring(2, 15) +
    Math.random().toString(36).substring(2, 10)
  );
}

interface RequestOptions {
  method?: string;
  body?: unknown;
  idempotencyKey?: string;
  signal?: AbortSignal;
}

async function request<T>(
  path: string,
  options: RequestOptions = {},
): Promise<T> {
  const { method = "GET", body, idempotencyKey, signal } = options;
  const headers: Record<string, string> = {
    Accept: "application/json",
  };

  if (body !== undefined) {
    headers["Content-Type"] = "application/json";
  }

  if (idempotencyKey) {
    headers["Idempotency-Key"] = idempotencyKey;
  }

  let response: Response;
  try {
    response = await fetch(path, {
      method,
      headers,
      body: body !== undefined ? JSON.stringify(body) : undefined,
      credentials: "same-origin",
      signal,
    });
  } catch (networkErr: unknown) {
    if (networkErr instanceof Error && networkErr.name === "AbortError") {
      throw new ApiClientError(
        0,
        "ABORT_ERROR",
        "Request was cancelled.",
        false,
      );
    }
    const message =
      networkErr instanceof Error
        ? `Backend unreachable at ${path}: ${networkErr.message}. Ensure backend is running on 127.0.0.1:8000.`
        : "Backend offline or network unreachable.";
    throw new ApiClientError(0, "CONNECTION_ERROR", message, false);
  }

  if (!response.ok) {
    let errorCode = `HTTP_${response.status}`;
    let errorMessage = response.statusText || "Request failed";
    let isStructured = false;

    try {
      const errorJson = (await response.json()) as ApiErrorResponse;
      if (errorJson && errorJson.error) {
        errorCode = errorJson.error.code || errorCode;
        errorMessage = errorJson.error.message || errorMessage;
        isStructured = true;
      }
    } catch {
      // response was not json (e.g. empty body on Vite proxy ECONNREFUSED)
      if (response.status === 500) {
        errorCode = "HTTP_500";
        errorMessage =
          "Backend unavailable or proxy connection failed (HTTP 500: Internal Server Error). Ensure backend is running on 127.0.0.1:8000.";
      }
    }

    throw new ApiClientError(
      response.status,
      errorCode,
      errorMessage,
      isStructured,
    );
  }

  try {
    return (await response.json()) as T;
  } catch (parseErr: unknown) {
    const message =
      parseErr instanceof Error
        ? `Response received from ${path} (status ${response.status}) but body was unreadable or interrupted: ${parseErr.message}`
        : `Response received from ${path} (status ${response.status}) but body could not be parsed`;
    throw new ApiClientError(response.status, "RESPONSE_PARSE_ERROR", message);
  }
}

export const api = {
  getHealth: () =>
    request<{ status: string; milestone: string; agent_mode: string }>(
      "/api/health",
    ),

  createWorkspace: async () => {
    const raw = await request<WorkspaceCreationResponse>("/api/workspaces", {
      method: "POST",
      body: {},
    });
    return {
      workspace: raw.workspace,
      snapshot: validateSnapshot(raw.snapshot),
    };
  },

  getSnapshot: async () => {
    const raw = await request<Snapshot>("/api/snapshot");
    return validateSnapshot(raw);
  },

  createRequest: (body: CreateRequestBody, idempotencyKey: string) =>
    request<{ request: BorrowRequest }>("/api/requests", {
      method: "POST",
      body,
      idempotencyKey,
    }),

  createReservation: (body: CreateReservationBody, idempotencyKey: string) =>
    request<{ request: BorrowRequest; equipment: Equipment; loan: Loan }>(
      "/api/reservations",
      {
        method: "POST",
        body,
        idempotencyKey,
      },
    ),

  pickupLoan: (loanId: string, body: LoanPickupBody, idempotencyKey: string) =>
    request<{ request: BorrowRequest; equipment: Equipment; loan: Loan }>(
      `/api/loans/${encodeURIComponent(loanId)}/pickup`,
      {
        method: "POST",
        body,
        idempotencyKey,
      },
    ),

  returnLoan: (loanId: string, body: LoanReturnBody, idempotencyKey: string) =>
    request<{ request: BorrowRequest; equipment: Equipment; loan: Loan }>(
      `/api/loans/${encodeURIComponent(loanId)}/return`,
      {
        method: "POST",
        body,
        idempotencyKey,
      },
    ),

  inspectEquipment: (
    equipmentId: string,
    body: EquipmentInspectionBody,
    idempotencyKey: string,
  ) =>
    request<{
      equipment: Equipment;
      request: BorrowRequest | null;
      loan: Loan | null;
    }>(`/api/equipment/${encodeURIComponent(equipmentId)}/inspection`, {
      method: "POST",
      body,
      idempotencyKey,
    }),

  interpretIntake: (body: IntakeInterpretRequest, signal?: AbortSignal) =>
    request<IntakeInterpretResponse>("/api/intake/interpret", {
      method: "POST",
      body,
      signal,
    }),
};
