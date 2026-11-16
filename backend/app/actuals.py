"""Actual generation import from CSV (and XLSX) — parse, validate, persist.

Integrity rules:

- Timestamps are persisted in **UTC**. Naive local timestamps are converted
  with the site timezone (default Europe/Podgorica); DST ambiguities are
  explicitly warned about (fold=0 = first occurrence) and nonexistent local
  times (spring-forward gap) are hard errors — never silently shifted.
- Energy (MWh per interval) and power (interval-average MW) are distinguished;
  conversions use the interval duration.
- Duplicate interval starts (within the file or against the DB) are surfaced;
  overwriting existing actual rows happens only when the caller explicitly
  chooses `replace`.
- Import provenance (file, mapping, warnings) is stored alongside the rows.
"""

from __future__ import annotations

import csv
import io
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import db as dbm

VALUE_KINDS = ("energy_mwh", "power_mw")
TIMESTAMP_KINDS = ("auto", "naive-local", "iso-offset")

_FALLBACK_FORMATS = (
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d %H:%M:%S",
    "%d.%m.%Y %H:%M",
    "%d.%m.%Y %H:%M:%S",
    "%d/%m/%Y %H:%M",
    "%d/%m/%Y %H:%M:%S",
    "%Y/%m/%d %H:%M",
)


class ActualsError(ValueError):
    """Structural problem with the request (bad mapping, unreadable file)."""


@dataclass
class ActualRow:
    interval_start_utc: str
    interval_end_utc: str
    energy_mwh: float | None
    power_mw: float | None


@dataclass
class ParseResult:
    rows: list[ActualRow] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    fieldnames: list[str] = field(default_factory=list)
    rows_total: int = 0
    interval_hours: float | None = None


# --- file reading ------------------------------------------------------------


def read_table(filename: str, data: bytes) -> tuple[list[str], list[dict]]:
    """Read a CSV or XLSX into (fieldnames, row-dicts), skipping empty rows."""
    name = (filename or "").lower()
    if name.endswith((".xlsx", ".xlsm")):
        return _read_xlsx(data)
    return _read_csv(data)


def _read_csv(data: bytes) -> tuple[list[str], list[dict]]:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ActualsError(f"CSV is not valid UTF-8: {exc}") from exc
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel  # plain comma default
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    if not reader.fieldnames:
        raise ActualsError("CSV has no header row")
    fieldnames = [f.strip() for f in reader.fieldnames]
    rows: list[dict] = []
    for raw in reader:
        cleaned = {(k or "").strip(): (v.strip() if isinstance(v, str) else v) for k, v in raw.items()}
        if any(v not in (None, "") for v in cleaned.values()):
            rows.append(cleaned)
    return fieldnames, rows


def _read_xlsx(data: bytes) -> tuple[list[str], list[dict]]:
    try:
        from openpyxl import load_workbook
    except ImportError as exc:  # pragma: no cover
        raise ActualsError("XLSX support requires the 'openpyxl' package") from exc
    try:
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:
        raise ActualsError(f"cannot read XLSX: {exc}") from exc
    ws = wb.active
    if ws is None:
        raise ActualsError("XLSX has no sheets")
    rows_iter = ws.iter_rows(values_only=True)
    header = next(rows_iter, None)
    if header is None:
        raise ActualsError("XLSX sheet is empty")
    fieldnames = [str(c).strip() if c is not None else f"col{i}" for i, c in enumerate(header)]
    rows: list[dict] = []
    for raw in rows_iter:
        if raw is None or all(c is None for c in raw):
            continue
        rows.append({fieldnames[i]: raw[i] for i in range(min(len(fieldnames), len(raw)))})
    return fieldnames, rows


# --- value / timestamp parsing ------------------------------------------------


def parse_number(raw) -> tuple[float | None, str | None]:
    """Float parse with European decimal-comma support. Returns (value, warning).

    Non-finite values (NaN/inf) are rejected as unparseable — they must never
    enter the stored actuals or be silently aggregated into a scored hour.
    """
    import math

    if raw is None or raw == "":
        return None, None
    if isinstance(raw, (int, float)):
        v = float(raw)
        return (v, None) if math.isfinite(v) else (None, "non-finite number (NaN/inf) rejected")
    s = str(raw).strip().replace(" ", "").replace("\u00a0", "")
    if not s:
        return None, None
    warning = None
    if "," in s and "." not in s:
        s = s.replace(",", ".")
        warning = f"decimal comma interpreted: {raw!r} -> {s}"
    try:
        v = float(s)
    except ValueError:
        return None, f"unparseable number: {raw!r}"
    if not math.isfinite(v):
        return None, "non-finite number (NaN/inf) rejected"
    return v, warning


def parse_timestamp(raw, timestamp_kind: str, tz: ZoneInfo) -> tuple[datetime | None, str | None, str | None]:
    """Parse one timestamp to aware UTC.

    Returns (utc_dt | None, warning | None, error | None).
    """
    if raw is None or raw == "":
        return None, None, "missing timestamp"
    if isinstance(raw, datetime):
        dt = raw
    else:
        s = str(raw).strip()
        dt = None
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            for fmt in _FALLBACK_FORMATS:
                try:
                    dt = datetime.strptime(s, fmt)
                    break
                except ValueError:
                    continue
        if dt is None:
            return None, None, f"unparseable timestamp: {raw!r}"

    has_offset = dt.tzinfo is not None
    if timestamp_kind == "iso-offset" and not has_offset:
        return None, None, f"timestamp {raw!r} has no UTC offset but mapping expects one"
    if timestamp_kind == "naive-local" and has_offset:
        return dt.astimezone(timezone.utc), None, None  # explicit offset wins
    if has_offset:
        return dt.astimezone(timezone.utc), None, None

    # Naive local time: attach the site timezone (fold=0 = first occurrence).
    aware = dt.replace(tzinfo=tz, fold=0)
    roundtrip = aware.astimezone(timezone.utc).astimezone(tz)
    if roundtrip.replace(tzinfo=None) != dt:
        return None, None, (
            f"local time {raw!r} does not exist in {tz.key} (DST spring-forward gap)"
        )
    fold1 = dt.replace(tzinfo=tz, fold=1)
    warning = None
    if aware.utcoffset() != fold1.utcoffset():
        warning = (
            f"ambiguous local time {raw!r} in {tz.key} (DST fall-back); "
            "interpreted as the FIRST occurrence (fold=0)"
        )
    return aware.astimezone(timezone.utc), warning, None


# --- main parse ---------------------------------------------------------------


def parse_actuals(filename: str, data: bytes, mapping: dict) -> ParseResult:
    """Validate mapping + parse the whole file. Never raises for row-level
    problems — those are collected in `errors` so the user sees every issue."""
    ts_col = mapping.get("timestamp_col")
    value_col = mapping.get("value_col")
    value_kind = mapping.get("value_kind")
    ts_kind = mapping.get("timestamp_kind", "auto")
    tz_name = mapping.get("tz", "Europe/Podgorica")
    interval_hours = mapping.get("interval_hours")

    if not ts_col or not value_col:
        raise ActualsError("mapping requires 'timestamp_col' and 'value_col'")
    if value_kind not in VALUE_KINDS:
        raise ActualsError(f"value_kind must be one of {VALUE_KINDS}")
    if ts_kind not in TIMESTAMP_KINDS:
        raise ActualsError(f"timestamp_kind must be one of {TIMESTAMP_KINDS}")
    if interval_hours is not None and (not isinstance(interval_hours, (int, float)) or interval_hours <= 0):
        raise ActualsError("interval_hours must be a positive number")
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ActualsError(f"unknown timezone: {tz_name!r}") from None

    fieldnames, raw_rows = read_table(filename, data)
    for required in (ts_col, value_col):
        if required not in fieldnames:
            raise ActualsError(f"column {required!r} not found; file columns: {fieldnames}")

    result = ParseResult(fieldnames=fieldnames, rows_total=len(raw_rows))

    parsed: list[tuple[datetime, float | None, str | None]] = []
    for idx, row in enumerate(raw_rows, start=2):  # +1 header, +1 for 1-based
        dt, ts_warn, ts_err = parse_timestamp(row.get(ts_col), ts_kind, tz)
        if ts_err:
            result.errors.append({"row": idx, "field": ts_col, "message": ts_err, "value": str(row.get(ts_col))})
            continue
        if ts_warn:
            result.warnings.append(f"row {idx}: {ts_warn}")
        value, num_warn = parse_number(row.get(value_col))
        if num_warn and "unparseable" in num_warn:
            result.errors.append({"row": idx, "field": value_col, "message": num_warn, "value": str(row.get(value_col))})
            continue
        if num_warn:
            result.warnings.append(f"row {idx}: {num_warn}")
        if value is None:
            result.errors.append({"row": idx, "field": value_col, "message": "missing value", "value": ""})
            continue
        if value < 0:
            result.errors.append({"row": idx, "field": value_col, "message": "negative values are not valid actual generation", "value": str(row.get(value_col))})
            continue
        parsed.append((dt, value, ts_warn))

    if len(parsed) >= 2 and interval_hours is None:
        deltas = sorted(
            (b - a).total_seconds() / 3600.0
            for (a, _, _), (b, _, _) in zip(parsed, parsed[1:])
            if b > a
        )
        if deltas:
            interval_hours = round(statistics.median(deltas), 6)
            result.warnings.append(f"interval duration inferred from data: {interval_hours} h")
        else:
            interval_hours = 1.0
            result.warnings.append("interval duration could not be inferred; using 1 h")
    if interval_hours is None:
        interval_hours = 1.0
        result.warnings.append("interval duration defaulted to 1 h")
    result.interval_hours = float(interval_hours)

    # Duplicate interval starts within the file: keep the LAST value, warn.
    by_start: dict[datetime, tuple[float | None, str | None]] = {}
    for dt, value, ts_warn in parsed:
        if dt in by_start:
            first_value = by_start[dt][0]
            result.warnings.append(
                f"duplicate interval {dt.strftime('%Y-%m-%dT%H:%M:%SZ')} in file; "
                f"kept the later value ({first_value!r} -> {value!r})"
            )
        by_start[dt] = (value, ts_warn)

    for dt in sorted(by_start):
        value = by_start[dt][0]
        start_utc = dt.astimezone(timezone.utc)
        end_utc = start_utc + timedelta(hours=float(interval_hours))
        if value_kind == "energy_mwh":
            energy = round(value, 6)
            power = round(value / float(interval_hours), 6)
        else:
            power = round(value, 6)
            energy = round(value * float(interval_hours), 6)
        result.rows.append(
            ActualRow(
                interval_start_utc=start_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                interval_end_utc=end_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                energy_mwh=energy,
                power_mw=power,
            )
        )
    return result


# --- persistence ---------------------------------------------------------------


def check_conflicts(db_path, site_id: str, rows: list[ActualRow]) -> list[str]:
    if not rows:
        return []
    starts = [r.interval_start_utc for r in rows]
    with dbm.session(db_path) as conn:
        existing = set()
        for chunk_start in range(0, len(starts), 500):
            chunk = starts[chunk_start : chunk_start + 500]
            q = ",".join("?" * len(chunk))
            found = conn.execute(
                f"SELECT interval_start_utc FROM actual_points WHERE site_id=? AND interval_start_utc IN ({q})",
                [site_id, *chunk],
            ).fetchall()
            existing.update(r["interval_start_utc"] for r in found)
    return sorted(existing)


def persist_actuals(
    db_path,
    site_id: str,
    filename: str,
    mapping: dict,
    parsed: ParseResult,
    on_conflict: str = "reject",
) -> dict:
    """Persist a parsed import. `on_conflict` must be an explicit 'replace' to
    overwrite existing rows; otherwise conflicting rows abort the import."""
    if on_conflict not in ("reject", "replace"):
        raise ActualsError("on_conflict must be 'reject' or 'replace'")
    conflicts = check_conflicts(db_path, site_id, parsed.rows)
    if conflicts and on_conflict == "reject":
        return {
            "imported": False,
            "reason": "conflicting rows exist and on_conflict=reject",
            "conflicts": conflicts,
            "n_conflicts": len(conflicts),
        }

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with dbm.session(db_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO actual_imports (site_id, filename, imported_at_utc, timezone,
                                        mapping_json, rows_total, rows_ok, warnings_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                site_id,
                filename,
                now,
                mapping.get("tz", "Europe/Podgorica"),
                dbm.j(mapping),
                parsed.rows_total,
                len(parsed.rows),
                dbm.j(parsed.warnings[:200]),
            ),
        )
        import_id = int(cur.lastrowid)
        if on_conflict == "replace":
            conn.executemany(
                """
                INSERT INTO actual_points (site_id, interval_start_utc, interval_end_utc,
                                           energy_mwh, power_mw, import_id)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(site_id, interval_start_utc) DO UPDATE SET
                    interval_end_utc=excluded.interval_end_utc,
                    energy_mwh=excluded.energy_mwh,
                    power_mw=excluded.power_mw,
                    import_id=excluded.import_id
                """,
                [
                    (site_id, r.interval_start_utc, r.interval_end_utc, r.energy_mwh, r.power_mw, import_id)
                    for r in parsed.rows
                ],
            )
        else:
            fresh = [r for r in parsed.rows if r.interval_start_utc not in set(conflicts)]
            conn.executemany(
                """
                INSERT INTO actual_points (site_id, interval_start_utc, interval_end_utc,
                                           energy_mwh, power_mw, import_id)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (site_id, r.interval_start_utc, r.interval_end_utc, r.energy_mwh, r.power_mw, import_id)
                    for r in fresh
                ],
            )
    return {
        "imported": True,
        "import_id": import_id,
        "rows_total": parsed.rows_total,
        "rows_imported": len(parsed.rows) if on_conflict == "replace" else len(parsed.rows) - len(conflicts),
        "n_conflicts": len(conflicts),
        "conflicts": conflicts[:50],
        "errors": parsed.errors[:100],
        "warnings": parsed.warnings[:100],
        "interval_hours": parsed.interval_hours,
    }
