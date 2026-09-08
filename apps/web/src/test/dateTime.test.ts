import { describe, it, expect } from "vitest";
import {
  isoToLocalInput,
  localInputToIso,
  getDefaultDueDate,
} from "../utils/dateTime";

describe("DateTime Utilities — Instant Preservation", () => {
  it("converts UTC ISO string to local input format and back preserving the exact instant with whole seconds", () => {
    const utcIso = "2026-09-20T10:00:00.000Z";
    const originalDate = new Date(utcIso);
    const expectedEpoch = originalDate.getTime();

    const localInput = isoToLocalInput(utcIso);
    expect(localInput).match(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$/);

    const convertedIso = localInputToIso(localInput);
    const convertedDate = new Date(convertedIso);

    // Exact epoch assertion, not rounded or floored
    expect(convertedDate.getTime()).toBe(expectedEpoch);
  });

  it("preserves nonzero seconds from UTC ISO string through local roundtrip", () => {
    const nonzeroIso = "2026-09-20T10:00:37.000Z";
    const originalEpoch = new Date(nonzeroIso).getTime();

    const localInput = isoToLocalInput(nonzeroIso);
    expect(localInput).match(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:37$/);

    const convertedIso = localInputToIso(localInput);
    const convertedEpoch = new Date(convertedIso).getTime();

    expect(convertedEpoch).toBe(originalEpoch);
  });

  it("handles nonzero timezone offsets and nonzero seconds without shifting instant", () => {
    const offsetIso = "2026-09-20T15:30:37+05:30";
    const originalEpoch = new Date(offsetIso).getTime();

    const localInput = isoToLocalInput(offsetIso);
    expect(localInput).match(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:37$/);

    const convertedIso = localInputToIso(localInput);
    const convertedEpoch = new Date(convertedIso).getTime();

    // Exact epoch assertion across timezone representation
    expect(convertedEpoch).toBe(originalEpoch);
  });

  it("returns empty string for null, undefined, empty, or invalid input", () => {
    expect(isoToLocalInput(null)).toBe("");
    expect(isoToLocalInput(undefined)).toBe("");
    expect(isoToLocalInput("")).toBe("");
    expect(isoToLocalInput("invalid-date-string")).toBe("");

    expect(localInputToIso(null)).toBe("");
    expect(localInputToIso(undefined)).toBe("");
    expect(localInputToIso("")).toBe("");
    expect(localInputToIso("invalid-date-string")).toBe("");
  });

  it("generates default due date 7 days in the future with second precision", () => {
    const defaultDate = getDefaultDueDate();
    expect(defaultDate).match(/^\d{4}-\d{2}-\d{2}T10:00:00$/);
    const d = new Date(defaultDate);
    const now = new Date();
    const diffDays = Math.round(
      (d.getTime() - now.getTime()) / (1000 * 60 * 60 * 24),
    );
    expect(diffDays).toBeGreaterThanOrEqual(6);
    expect(diffDays).toBeLessThanOrEqual(8);
  });
});
