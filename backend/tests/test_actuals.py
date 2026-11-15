"""Actuals import tests: units, UTC normalization, DST (Europe/Podgorica),
duplicates, conflicts, mixed bad rows, XLSX."""

import io

from app.actuals import parse_actuals, persist_actuals

TZ = "Europe/Podgorica"

# interval pinned for determinism; inference itself is tested separately
BASE_MAPPING = {
    "timestamp_col": "ts",
    "value_col": "mwh",
    "value_kind": "energy_mwh",
    "timestamp_kind": "auto",
    "tz": TZ,
    "interval_hours": 1.0,
}


def _parse(csv_text: str, mapping: dict, filename: str = "actuals.csv"):
    return parse_actuals(filename, csv_text.encode("utf-8"), mapping)


def test_utc_iso_24h_import(client):
    api, _fake, settings = client
    rows = [f"2026-11-14T{h:02d}:00:00Z,{h + 1}.5" for h in range(24)]
    parsed = _parse("ts,mwh\n" + "\n".join(rows), {**BASE_MAPPING, "tz": "UTC"})
    assert parsed.errors == []
    assert len(parsed.rows) == 24
    assert parsed.rows[0].interval_start_utc == "2026-11-14T00:00:00Z"
    assert parsed.rows[0].energy_mwh == 1.5
    assert parsed.rows[0].power_mw == 1.5
    assert parsed.interval_hours == 1.0

    res = persist_actuals(settings.db_path, "krnovo", "a.csv", BASE_MAPPING, parsed, on_conflict="reject")
    assert res["imported"] is True and res["rows_imported"] == 24

    listed = api.get(
        "/api/actuals",
        params={"from_utc": "2026-11-14T00:00:00Z", "to_utc": "2026-11-14T02:00:00Z"},
    ).json()["rows"]
    assert [r["energy_mwh"] for r in listed] == [1.5, 2.5]


def test_power_kind_converts_to_energy():
    rows = ["2026-11-14T00:00:00Z,30", "2026-11-14T01:00:00Z,60"]
    parsed = _parse(
        "ts,mw\n" + "\n".join(rows),
        {**BASE_MAPPING, "value_col": "mw", "value_kind": "power_mw", "tz": "UTC"},
    )
    assert parsed.rows[0].power_mw == 30.0
    assert parsed.rows[0].energy_mwh == 30.0
    assert parsed.rows[1].energy_mwh == 60.0


def test_interval_inference_when_not_given():
    rows = [f"2026-11-14T{h:02d}:00:00Z,{h}" for h in range(4)]
    mapping = {k: v for k, v in BASE_MAPPING.items() if k != "interval_hours"}
    parsed = _parse("ts,mwh\n" + "\n".join(rows), {**mapping, "tz": "UTC"})
    assert parsed.interval_hours == 1.0
    assert any("inferred" in w for w in parsed.warnings)
    # 15-minute data infers 0.25 h
    rows15 = [f"2026-11-14T00:{m:02d}:00Z,1" for m in (0, 15, 30, 45)]
    parsed15 = _parse("ts,mwh\n" + "\n".join(rows15), {**mapping, "tz": "UTC"})
    assert parsed15.interval_hours == 0.25
    assert parsed15.rows[0].power_mw == 4.0  # 1 MWh per 15 min → 4 MW


LOCAL_MAPPING = {**BASE_MAPPING, "timestamp_col": "vrijeme"}


def test_naive_local_podgorica_winter_is_utc_plus_one():
    rows = ["14.11.2026 01:00,10", "14.11.2026 02:00,20"]  # CET = UTC+1
    parsed = _parse("vrijeme,mwh\n" + "\n".join(rows), LOCAL_MAPPING)
    assert parsed.rows[0].interval_start_utc == "2026-11-14T00:00:00Z"
    assert parsed.rows[1].interval_start_utc == "2026-11-14T01:00:00Z"


def test_dst_spring_forward_nonexistent_time_is_error():
    # 2026-03-29: 02:00–03:00 local does not exist in Europe/Podgorica
    rows = [
        "29.03.2026 00:00,10",
        "29.03.2026 01:00,10",
        "29.03.2026 02:30,10",  # nonexistent
        "29.03.2026 04:00,10",
    ]
    parsed = _parse("vrijeme,mwh\n" + "\n".join(rows), LOCAL_MAPPING)
    assert len(parsed.errors) == 1
    assert "does not exist" in parsed.errors[0]["message"]
    assert len(parsed.rows) == 3


def test_dst_fall_back_ambiguous_time_warns_and_keeps_first():
    # 2026-10-25: 02:00 occurs twice (CEST then CET); a naive file can't say which
    rows = [
        "25.10.2026 01:00,10",
        "25.10.2026 02:00,20",
        "25.10.2026 02:00,21",
        "25.10.2026 03:00,30",
    ]
    parsed = _parse("vrijeme,mwh\n" + "\n".join(rows), LOCAL_MAPPING)
    assert any("ambiguous" in w for w in parsed.warnings)
    assert any("duplicate interval" in w for w in parsed.warnings)
    starts = [r.interval_start_utc for r in parsed.rows]
    assert len(starts) == len(set(starts)), "duplicate local times collapse to one UTC interval"
    # both 02:00 rows resolve to 2026-10-25T00:00:00Z (fold=0 = CEST, UTC+2)
    target = [r for r in parsed.rows if r.interval_start_utc == "2026-10-25T00:00:00Z"]
    assert len(target) == 1 and target[0].energy_mwh == 21.0


def test_duplicate_timestamps_in_file_warn_and_keep_last():
    rows = [
        "2026-11-14T00:00:00Z,10",
        "2026-11-14T00:00:00Z,12",
        "2026-11-14T01:00:00Z,20",
    ]
    parsed = _parse("ts,mwh\n" + "\n".join(rows), {**BASE_MAPPING, "tz": "UTC"})
    assert any("duplicate interval" in w for w in parsed.warnings)
    assert parsed.rows[0].energy_mwh == 12.0


def test_decimal_comma_and_bad_rows():
    rows = [
        "2026-11-14T00:00:00Z,3",  # comma-decimal CSV would split columns; value is 3
        '2026-11-14T01:00:00Z,"3,5"',  # quoted decimal comma → 3.5
        "2026-11-14T02:00:00Z,abc",
        "2026-11-14T03:00:00Z,-2",
        "2026-11-14T04:00:00Z,",  # missing value
    ]
    parsed = _parse("ts,mwh\n" + "\n".join(rows), {**BASE_MAPPING, "tz": "UTC"})
    assert parsed.rows[0].energy_mwh == 3.0
    assert parsed.rows[1].energy_mwh == 3.5
    assert any("decimal comma" in w for w in parsed.warnings)
    assert any("unparseable number" in e["message"] for e in parsed.errors)
    assert any("negative" in e["message"] for e in parsed.errors)
    assert any("missing value" in e["message"] for e in parsed.errors)
    assert len(parsed.rows) == 2


def test_conflicts_reject_then_replace(client):
    api, _fake, settings = client
    csv_text = "ts,mwh\n2026-11-14T00:00:00Z,10\n2026-11-14T01:00:00Z,20\n"
    parsed = _parse(csv_text, {**BASE_MAPPING, "tz": "UTC"})
    res1 = persist_actuals(settings.db_path, "krnovo", "a.csv", BASE_MAPPING, parsed, on_conflict="reject")
    assert res1["imported"] is True

    # same intervals again, reject → not imported, conflicts listed
    parsed2 = _parse("ts,mwh\n2026-11-14T01:00:00Z,99\n", {**BASE_MAPPING, "tz": "UTC"})
    res2 = persist_actuals(settings.db_path, "krnovo", "b.csv", BASE_MAPPING, parsed2, on_conflict="reject")
    assert res2["imported"] is False and res2["n_conflicts"] == 1

    # explicit replace → overwritten, provenance updated
    res3 = persist_actuals(settings.db_path, "krnovo", "b.csv", BASE_MAPPING, parsed2, on_conflict="replace")
    assert res3["imported"] is True and res3["rows_imported"] == 1

    rows = api.get(
        "/api/actuals",
        params={"from_utc": "2026-11-14T00:00:00Z", "to_utc": "2026-11-14T02:00:00Z"},
    ).json()["rows"]
    assert {r["interval_start_utc"]: r["energy_mwh"] for r in rows}["2026-11-14T01:00:00Z"] == 99.0


def test_api_import_and_preview(client):
    api, _fake, _settings = client
    csv_bytes = b"ts,mwh\n2026-11-14T00:00:00Z,1\n2026-11-14T01:00:00Z,2\n"
    mapping = '{"timestamp_col":"ts","value_col":"mwh","value_kind":"energy_mwh","tz":"UTC","interval_hours":1.0}'

    preview = api.post(
        "/api/actuals/preview",
        files={"file": ("a.csv", io.BytesIO(csv_bytes), "text/csv")},
        data={"mapping": mapping},
    ).json()
    assert preview["rows_parsed"] == 2
    assert preview["n_conflicts_with_db"] == 0

    result = api.post(
        "/api/actuals/import",
        files={"file": ("a.csv", io.BytesIO(csv_bytes), "text/csv")},
        data={"mapping": mapping, "on_conflict": "reject"},
    ).json()
    assert result["imported"] is True

    # re-import same file with reject → conflicts
    again = api.post(
        "/api/actuals/import",
        files={"file": ("a.csv", io.BytesIO(csv_bytes), "text/csv")},
        data={"mapping": mapping, "on_conflict": "reject"},
    ).json()
    assert again["imported"] is False and again["n_conflicts"] == 2

    imports = api.get("/api/actuals/imports").json()["imports"]
    assert len(imports) >= 1


def test_api_import_rejects_bad_mapping(client):
    api, _fake, _settings = client
    resp = api.post(
        "/api/actuals/preview",
        files={"file": ("a.csv", io.BytesIO(b"ts,mwh\n2026-11-14T00:00:00Z,1\n"), "text/csv")},
        data={"mapping": '{"timestamp_col":"nope","value_col":"mwh","value_kind":"energy_mwh"}'},
    )
    assert resp.status_code == 400


def test_xlsx_import(client):
    api, _fake, _settings = client
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(["ts", "mwh"])
    ws.append(["2026-11-14T00:00:00Z", 5])
    ws.append(["2026-11-14T01:00:00Z", 7])
    buf = io.BytesIO()
    wb.save(buf)
    mapping = '{"timestamp_col":"ts","value_col":"mwh","value_kind":"energy_mwh","tz":"UTC","interval_hours":1.0}'

    preview = api.post(
        "/api/actuals/preview",
        files={"file": ("actuals.xlsx", io.BytesIO(buf.getvalue()), "application/octet-stream")},
        data={"mapping": mapping},
    ).json()
    assert preview["rows_parsed"] == 2

    result = api.post(
        "/api/actuals/import",
        files={"file": ("actuals.xlsx", io.BytesIO(buf.getvalue()), "application/octet-stream")},
        data={"mapping": mapping, "on_conflict": "reject"},
    ).json()
    assert result["imported"] is True and result["rows_imported"] == 2
