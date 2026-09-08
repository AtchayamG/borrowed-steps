import { ApiClientError } from "../api/client";
import type {
  IntakeInterpretResponse,
  EquipmentKind,
  MissingField,
} from "../types/api";

const VALID_EQUIPMENT_KINDS: ReadonlySet<string> = new Set([
  "WHEELCHAIR",
  "WALKER",
  "CRUTCHES",
]);

const ALLOWED_ROOT_KEYS: ReadonlySet<string> = new Set([
  "draft",
  "missing_fields",
  "provenance",
]);

const ALLOWED_DRAFT_KEYS: ReadonlySet<string> = new Set([
  "borrower_label",
  "equipment_kind",
  "pickup_location",
  "due_at",
]);

const ALLOWED_PROVENANCE_KEYS: ReadonlySet<string> = new Set([
  "framework",
  "provider",
  "model",
  "inventory_tool_calls",
  "completed_at",
]);

function isLeapYear(year: number): boolean {
  return (year % 4 === 0 && year % 100 !== 0) || year % 400 === 0;
}

function daysInMonth(year: number, month: number): number {
  switch (month) {
    case 1:
    case 3:
    case 5:
    case 7:
    case 8:
    case 10:
    case 12:
      return 31;
    case 4:
    case 6:
    case 9:
    case 11:
      return 30;
    case 2:
      return isLeapYear(year) ? 29 : 28;
    default:
      return 0;
  }
}

const ISO_DATETIME_REGEX =
  /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?(Z|[+-]\d{2}(?::?\d{2})?)$/i;

/**
 * Strictly validates an ISO-8601 datetime timestamp including full calendar sanity.
 * @param str The timestamp string to validate.
 * @param requireUtc If true, the timezone must strictly represent UTC ('Z' or '+00:00'/'-00:00').
 */
export function isValidIsoTimestamp(str: unknown, requireUtc = false): boolean {
  if (typeof str !== "string") return false;

  const match = str.match(ISO_DATETIME_REGEX);
  if (
    !match ||
    match[1] === undefined ||
    match[2] === undefined ||
    match[3] === undefined ||
    match[4] === undefined ||
    match[5] === undefined ||
    match[6] === undefined ||
    match[8] === undefined
  ) {
    return false;
  }

  const year = parseInt(match[1], 10);
  const month = parseInt(match[2], 10);
  const day = parseInt(match[3], 10);
  const hour = parseInt(match[4], 10);
  const minute = parseInt(match[5], 10);
  const second = parseInt(match[6], 10);
  const tz = match[8].toUpperCase();

  // Calendar validation
  if (month < 1 || month > 12) return false;
  const maxDays = daysInMonth(year, month);
  if (day < 1 || day > maxDays) return false;

  // Time validation
  if (hour < 0 || hour > 23) return false;
  if (minute < 0 || minute > 59) return false;
  if (second < 0 || second > 59) return false;

  // Timezone validation
  if (tz === "Z") {
    // Valid UTC
  } else {
    const tzMatch = tz.match(/^([+-])(\d{2})(?::?(\d{2}))?$/);
    if (!tzMatch || tzMatch[2] === undefined) return false;
    const tzHour = parseInt(tzMatch[2], 10);
    const tzMin = tzMatch[3] !== undefined ? parseInt(tzMatch[3], 10) : 0;
    if (tzHour > 14 || tzMin > 59) return false;
    if (tzHour === 14 && tzMin > 0) return false;

    if (requireUtc && (tzHour !== 0 || tzMin !== 0)) {
      return false;
    }
  }

  // Final JS Date parse check to ensure runtime engine acceptance
  const epoch = Date.parse(str);
  if (isNaN(epoch)) return false;

  return true;
}

/**
 * Strictly validates an intake interpretation response payload against the frozen M2A contract.
 * Throws ApiClientError(502, "ASSISTANT_INVALID_OUTPUT") on any contract discrepancy.
 */
export function validateIntakeResponse(data: unknown): IntakeInterpretResponse {
  if (!data || typeof data !== "object" || Array.isArray(data)) {
    throw new ApiClientError(
      502,
      "ASSISTANT_INVALID_OUTPUT",
      "Invalid model response: root payload must be an object.",
      true,
    );
  }

  const res = data as Record<string, unknown>;

  // Check for unknown root keys
  for (const key of Object.keys(res)) {
    if (!ALLOWED_ROOT_KEYS.has(key)) {
      throw new ApiClientError(
        502,
        "ASSISTANT_INVALID_OUTPUT",
        `Invalid model response: unexpected root field '${key}'.`,
        true,
      );
    }
  }

  // 1. Validate draft object
  if (!res.draft || typeof res.draft !== "object" || Array.isArray(res.draft)) {
    throw new ApiClientError(
      502,
      "ASSISTANT_INVALID_OUTPUT",
      "Invalid model response: missing draft object.",
      true,
    );
  }

  const draft = res.draft as Record<string, unknown>;

  for (const key of Object.keys(draft)) {
    if (!ALLOWED_DRAFT_KEYS.has(key)) {
      throw new ApiClientError(
        502,
        "ASSISTANT_INVALID_OUTPUT",
        `Invalid model response: unexpected draft field '${key}'.`,
        true,
      );
    }
  }

  if (
    !("borrower_label" in draft) ||
    !("equipment_kind" in draft) ||
    !("pickup_location" in draft) ||
    !("due_at" in draft)
  ) {
    throw new ApiClientError(
      502,
      "ASSISTANT_INVALID_OUTPUT",
      "Invalid model response: draft missing required field(s).",
      true,
    );
  }

  // borrower_label: string (1..60) or null
  if (
    draft.borrower_label !== null &&
    (typeof draft.borrower_label !== "string" ||
      draft.borrower_label.trim().length === 0 ||
      draft.borrower_label.length > 60)
  ) {
    throw new ApiClientError(
      502,
      "ASSISTANT_INVALID_OUTPUT",
      "Invalid model response: borrower_label must be null or 1-60 characters.",
      true,
    );
  }

  // equipment_kind: "WHEELCHAIR" | "WALKER" | "CRUTCHES" | null
  if (
    draft.equipment_kind !== null &&
    (typeof draft.equipment_kind !== "string" ||
      !VALID_EQUIPMENT_KINDS.has(draft.equipment_kind))
  ) {
    throw new ApiClientError(
      502,
      "ASSISTANT_INVALID_OUTPUT",
      "Invalid model response: equipment_kind must be WHEELCHAIR, WALKER, CRUTCHES, or null.",
      true,
    );
  }

  // pickup_location: string (1..120) or null
  if (
    draft.pickup_location !== null &&
    (typeof draft.pickup_location !== "string" ||
      draft.pickup_location.trim().length === 0 ||
      draft.pickup_location.length > 120)
  ) {
    throw new ApiClientError(
      502,
      "ASSISTANT_INVALID_OUTPUT",
      "Invalid model response: pickup_location must be null or 1-120 characters.",
      true,
    );
  }

  // due_at: full timezone-aware ISO string with valid calendar fields, or null
  if (draft.due_at !== null && !isValidIsoTimestamp(draft.due_at, false)) {
    throw new ApiClientError(
      502,
      "ASSISTANT_INVALID_OUTPUT",
      "Invalid model response: due_at must be null or a valid ISO timestamp with full timezone and valid calendar fields.",
      true,
    );
  }

  // 2. Validate missing_fields
  if (!Array.isArray(res.missing_fields)) {
    throw new ApiClientError(
      502,
      "ASSISTANT_INVALID_OUTPUT",
      "Invalid model response: missing_fields must be an array.",
      true,
    );
  }

  const expectedMissing: string[] = [];
  if (draft.borrower_label === null) expectedMissing.push("borrower_label");
  if (draft.equipment_kind === null) expectedMissing.push("equipment_kind");
  if (draft.pickup_location === null) expectedMissing.push("pickup_location");
  if (draft.due_at === null) expectedMissing.push("due_at");

  if (
    res.missing_fields.length !== expectedMissing.length ||
    !res.missing_fields.every((item, idx) => item === expectedMissing[idx])
  ) {
    throw new ApiClientError(
      502,
      "ASSISTANT_INVALID_OUTPUT",
      "Invalid model response: missing_fields does not match null draft fields in canonical order.",
      true,
    );
  }

  // 3. Validate provenance
  if (
    !res.provenance ||
    typeof res.provenance !== "object" ||
    Array.isArray(res.provenance)
  ) {
    throw new ApiClientError(
      502,
      "ASSISTANT_INVALID_OUTPUT",
      "Invalid model response: missing provenance object.",
      true,
    );
  }

  const prov = res.provenance as Record<string, unknown>;

  for (const key of Object.keys(prov)) {
    if (!ALLOWED_PROVENANCE_KEYS.has(key)) {
      throw new ApiClientError(
        502,
        "ASSISTANT_INVALID_OUTPUT",
        `Invalid model response: unexpected provenance field '${key}'.`,
        true,
      );
    }
  }

  if (prov.framework !== "strands") {
    throw new ApiClientError(
      502,
      "ASSISTANT_INVALID_OUTPUT",
      "Invalid model response: provenance framework must be 'strands'.",
      true,
    );
  }

  if (prov.provider !== "ollama") {
    throw new ApiClientError(
      502,
      "ASSISTANT_INVALID_OUTPUT",
      "Invalid model response: provenance provider must be 'ollama'.",
      true,
    );
  }

  if (prov.model !== "llama3.2:3b") {
    throw new ApiClientError(
      502,
      "ASSISTANT_INVALID_OUTPUT",
      "Invalid model response: provenance model must be 'llama3.2:3b'.",
      true,
    );
  }

  if (
    typeof prov.inventory_tool_calls !== "number" ||
    !Number.isInteger(prov.inventory_tool_calls) ||
    prov.inventory_tool_calls < 1 ||
    prov.inventory_tool_calls > 2
  ) {
    throw new ApiClientError(
      502,
      "ASSISTANT_INVALID_OUTPUT",
      "Invalid model response: inventory_tool_calls must be an integer between 1 and 2.",
      true,
    );
  }

  if (!isValidIsoTimestamp(prov.completed_at, true)) {
    throw new ApiClientError(
      502,
      "ASSISTANT_INVALID_OUTPUT",
      "Invalid model response: provenance completed_at must be a full UTC ISO timestamp with valid calendar fields.",
      true,
    );
  }

  return {
    draft: {
      borrower_label: draft.borrower_label as string | null,
      equipment_kind: draft.equipment_kind as EquipmentKind | null,
      pickup_location: draft.pickup_location as string | null,
      due_at: draft.due_at as string | null,
    },
    missing_fields: res.missing_fields as MissingField[],
    provenance: {
      framework: "strands",
      provider: "ollama",
      model: "llama3.2:3b",
      inventory_tool_calls: prov.inventory_tool_calls as number,
      completed_at: prov.completed_at as string,
    },
  };
}
