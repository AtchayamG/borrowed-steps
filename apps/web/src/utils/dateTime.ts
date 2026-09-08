/**
 * Date/time utilities for Borrowed Steps.
 * Preserves the exact instant when converting between UTC ISO strings
 * and local HTML datetime-local input values (YYYY-MM-DDTHH:mm:ss).
 */

const pad = (n: number) => n.toString().padStart(2, "0");

/**
 * Converts a UTC or timezone-aware ISO string to local datetime-local format (YYYY-MM-DDTHH:mm:ss).
 * Returns empty string if input is null, undefined, empty, or invalid.
 * Preserves the exact instant down to whole seconds.
 */
export function isoToLocalInput(isoStr: string | null | undefined): string {
  if (!isoStr || typeof isoStr !== "string") return "";
  const d = new Date(isoStr);
  if (isNaN(d.getTime())) return "";
  const year = d.getFullYear();
  const month = pad(d.getMonth() + 1);
  const day = pad(d.getDate());
  const hours = pad(d.getHours());
  const minutes = pad(d.getMinutes());
  const seconds = pad(d.getSeconds());
  return `${year}-${month}-${day}T${hours}:${minutes}:${seconds}`;
}

/**
 * Converts a local datetime-local input string (YYYY-MM-DDTHH:mm:ss or YYYY-MM-DDTHH:mm) to a UTC ISO string.
 * Returns empty string if input is null, undefined, empty, or invalid.
 * Preserves the exact instant down to whole seconds.
 */
export function localInputToIso(localStr: string | null | undefined): string {
  if (!localStr || typeof localStr !== "string") return "";
  const d = new Date(localStr);
  if (isNaN(d.getTime())) return "";
  return d.toISOString();
}

/**
 * Generates a default local due date 7 days in the future at 10:00:00 AM local time.
 */
export function getDefaultDueDate(): string {
  const d = new Date();
  d.setDate(d.getDate() + 7);
  const month = pad(d.getMonth() + 1);
  const day = pad(d.getDate());
  return `${d.getFullYear()}-${month}-${day}T10:00:00`;
}
