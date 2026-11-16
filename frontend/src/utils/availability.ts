// Availability screen helpers — pure, tested functions.

/** Convert a naive local wall-clock time in an IANA timezone to UTC, using the
 *  browser's ICU database (correct DST offsets for the specific instant).
 *
 *  - Nonexistent local times (spring-forward gap): reported via `error`.
 *  - Ambiguous local times (fall-back): resolved to the FIRST occurrence with
 *    a `warning`, matching the backend importer semantics.
 */
export function localToUtc(local: string, tz: string): { utc?: string; warning?: string; error?: string } {
  try {
    // throws RangeError for an unknown IANA zone
    new Intl.DateTimeFormat("en-GB", { timeZone: tz }).format(new Date());
  } catch {
    return { error: `unknown timezone: ${JSON.stringify(tz)}` };
  }
  const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})$/.exec(local.trim());
  if (!m) return { error: `invalid local time ${JSON.stringify(local)}` };
  const [, y, mo, d, h, mi] = m;

  function utcFor(): string | null {
    // Interpret the wall clock in `tz` by finding the UTC instant whose
    // formatted time in `tz` equals the wall clock (a nonexistent wall clock
    // simply matches no UTC instant).
    const asUtc = new Date(`${y}-${mo}-${d}T${h}:${mi}:00Z`);
    if (Number.isNaN(asUtc.getTime())) return null;
    for (let i = -14; i <= 14; i++) {
      const candidate = new Date(asUtc.getTime() + i * 3_600_000);
      const fmt = new Intl.DateTimeFormat("en-GB", {
        timeZone: tz,
        year: "numeric",
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
        hour12: false,
      }).formatToParts(candidate);
      const get = (type: string) => fmt.find((p) => p.type === type)?.value ?? "";
      const hour24 = get("hour") === "24" ? "00" : get("hour");
      if (
        get("year") === y &&
        get("month") === mo &&
        get("day") === d &&
        hour24 === h &&
        get("minute") === mi
      ) {
        return candidate.toISOString().slice(0, 19) + "Z";
      }
    }
    return null;
  }

  const first = utcFor();
  if (first === null) return { utc: "", error: `local time ${local} does not exist in ${tz} (DST spring-forward gap)` };
  const asUtcFirst = new Date(first.replace("Z", "Z"));
  // second-occurrence check: an hour LATER must render the same wall clock for
  // the time to be ambiguous
  const plusOne = new Date(asUtcFirst.getTime() + 3_600_000);
  const fmt = new Intl.DateTimeFormat("en-GB", {
    timeZone: tz,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).formatToParts(plusOne);
  const get = (type: string) => fmt.find((p) => p.type === type)?.value ?? "";
  const hour24 = get("hour") === "24" ? "00" : get("hour");
  const wallRepeat = `${get("year")}-${get("month")}-${get("day")}T${hour24}:${get("minute")}`;
  const ambiguous = wallRepeat === local;
  return { utc: first, warning: ambiguous ? `ambiguous local time ${local} in ${tz}; interpreted as the FIRST occurrence` : undefined };
}

/** Current wall-clock time in an IANA timezone as `YYYY-MM-DDTHH:MM` — the
 *  dynamic default for the bulk-edit start field. */
export function nowLocalForTz(tz: string): string {
  const parts = new Intl.DateTimeFormat("en-GB", {
    timeZone: tz,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).formatToParts(new Date());
  const get = (type: string) => parts.find((p) => p.type === type)?.value ?? "";
  const hour24 = get("hour") === "24" ? "00" : get("hour");
  return `${get("year")}-${get("month")}-${get("day")}T${hour24}:${get("minute")}`;
}
