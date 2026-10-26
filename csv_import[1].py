"""CSV parsing, templates and the import page. SQLite owns final validation."""
import csv
import hashlib
import io
import re
import zipfile
from uuid import NAMESPACE_URL, uuid5

import pandas as pd
from service import TABLE_COLUMNS, ENUMS

LABELS = {"workers": "Worker safety", "incidents": "Incidents", "equipment": "Equipment",
          "maintenance": "Maintenance", "risks": "Risk register", "readings": "Sensor readings"}
DERIVED = {"availability_pct", "score", "band", "temperature_status", "vibration_status",
           "fatigue_status", "overall_condition", "next_service_date", "service_overdue"}
EXAMPLES = {
    "workers": dict(id="W-CSV01", name="Example worker", department="Engineering", shift="Day",
                    hours_worked=8, ppe_compliant=1, training_current=1, last_check_date="2026-10-01"),
    "equipment": dict(id="E-CSV01", name="Example pump", department="Engineering", kind="Pump",
                      operating_hours=120, downtime_hours=5, status="Operating", last_service_date="2026-10-01",
                      service_interval_days=30, brake_condition="Good", tyre_condition="Good", engine_condition="Good"),
    "incidents": dict(id="I-CSV01", date="2026-10-01", department="Engineering", shift="Day", worker_id="W-CSV01",
                      kind="Near miss", severity="Low", status="Open", description="Example only - replace with your record"),
    "maintenance": dict(id="M-CSV01", equipment_id="E-CSV01", date="2026-10-01", due_date="2026-10-15",
                        completed_date="", status="Scheduled", description="Example pump inspection"),
    "risks": dict(id="K-CSV01", date="2026-10-01", department="Engineering", hazard="Example slip hazard",
                  likelihood=2, consequence=2, control="Keep walkway clear", owner="Example supervisor", status="Open"),
    "readings": dict(id="R-CSV01", equipment_id="E-CSV01", date="2026-10-01", temperature_c=65, vibration_mm_s=2.5),
}


def template_csv(table, example=False):
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=TABLE_COLUMNS[table])
    writer.writeheader()
    if example:
        writer.writerow(EXAMPLES[table])
    return output.getvalue().encode("utf-8-sig")


def template_bundle(tables):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for table in tables:
            archive.writestr(f"{table}.csv", template_csv(table))
            archive.writestr(f"examples/{table}_example.csv", template_csv(table, True))
    return output.getvalue()


def parse_csv(table, content):
    """Read string values, reject ambiguous headers and preserve identifiers."""
    if table not in TABLE_COLUMNS:
        raise ValueError("Unknown dataset.")
    if not content or len(content) > 5 * 1024 * 1024:
        raise ValueError("Choose a nonempty CSV no larger than 5 MB.")
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = content.decode("cp1252")
    if "\x00" in text:
        raise ValueError("The file is not a text CSV. Save it as CSV UTF-8 first.")
    try:
        dialect = csv.Sniffer().sniff(text[:8192], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(text, newline=""), dialect, strict=True)
    try:
        raw_header = next(reader)
    except (StopIteration, csv.Error):
        raise ValueError("The CSV needs a header row.") from None
    header = [re.sub(r"\s+", "_", name.strip().lower()) for name in raw_header]
    if len(header) != len(set(header)) or any(not name for name in header):
        raise ValueError("Column headings must be nonempty and unique.")
    unknown = set(header) - set(TABLE_COLUMNS[table]) - DERIVED
    if unknown:
        raise ValueError("Unrecognised columns: " + ", ".join(sorted(unknown)) + ". Download the matching template.")
    defaults = {"id": ""}
    if table == "incidents":
        defaults.update(worker_id="", shift="Day")
    if table == "maintenance":
        defaults["completed_date"] = ""
    if table == "equipment":
        defaults.update(brake_condition="Good", tyre_condition="Good", engine_condition="Good")
    missing = set(TABLE_COLUMNS[table]) - set(header) - set(defaults)
    if missing:
        raise ValueError("Missing columns: " + ", ".join(sorted(missing)))
    rows = []
    digest = hashlib.sha256(content).hexdigest()
    try:
        for cells in reader:
            if not any(cell.strip() for cell in cells):
                continue
            if len(cells) != len(header):
                raise ValueError(f"CSV line {reader.line_num}: expected {len(header)} values, found {len(cells)}. Check commas and quotes.")
            row = {**defaults, **dict(zip(header, (cell.strip() for cell in cells)))}
            row = {key: value for key, value in row.items() if key in TABLE_COLUMNS[table]}
            if not row["id"]:
                row["id"] = f"CSV-{uuid5(NAMESPACE_URL, f'{table}:{digest}:{len(rows)}').hex[:24]}"
            for key in ("ppe_compliant", "training_current"):
                if key in row:
                    row[key] = {"true": "1", "yes": "1", "false": "0", "no": "0"}.get(row[key].lower(), row[key])
            rows.append(row)
            if len(rows) > 10000:
                raise ValueError("Import at most 10,000 records at a time.")
    except csv.Error as error:
        raise ValueError(f"CSV line {reader.line_num}: {error}") from None
    if not rows:
        raise ValueError("The CSV has headings but no data rows.")
    return rows, sorted(set(header) & DERIVED)


def import_page(service, token):
    import streamlit as st

    st.title("Import CSV data")
    st.caption("Upload one dataset or several together. Preview and validate your files before saving.")
    permitted = [table for table in LABELS if service.can(token, table, "write")]
    if not permitted:
        st.info("Your role can view records but cannot import or update data.")
        return
    st.download_button("Download all CSV templates (ZIP)", template_bundle(permitted),
                       file_name="khwezi_csv_templates.zip", mime="application/zip")
    selected = st.multiselect("Datasets to import", permitted, format_func=LABELS.get, default=[permitted[0]])
    st.write("Dates: YYYY-MM-DD. PPE and training: 1/0 or Yes/No. Keep IDs consistent between linked datasets.")
    st.caption("Import workers with incidents, and equipment with maintenance or readings, when their IDs are new. "
               "Use one CSV per dataset. Dates in the sidebar do not limit imported records.")
    update = st.checkbox("Update records when their IDs already exist", value=False,
                         help="Existing records with matching IDs are replaced by the complete CSV row. No records are deleted.")
    batches, issues = {}, []
    epoch = st.session_state.get("csv_import_epoch", 0)
    for table in selected:
        with st.container(border=True):
            st.subheader(LABELS[table])
            st.download_button("Download blank template", template_csv(table), file_name=f"{table}.csv",
                               mime="text/csv", key=f"csv_template_{table}")
            with st.expander("Column names and example values"):
                st.dataframe(pd.DataFrame([EXAMPLES[table]]), hide_index=True)
                if "department" in TABLE_COLUMNS[table]:
                    st.write("Departments: " + ", ".join(ENUMS["department"]))
                if "shift" in TABLE_COLUMNS[table]:
                    st.write("Shifts: " + ", ".join(ENUMS["shift"]))
                singular = {"incidents": "incident", "equipment": "equipment", "maintenance": "maintenance", "risks": "risk"}.get(table)
                for field in ("kind", "status"):
                    if field in TABLE_COLUMNS[table]:
                        st.write(field.capitalize() + ": " + ", ".join(ENUMS[f"{singular}_{field}"]))
                if table == "equipment":
                    st.write("Component conditions: Good, Needs attention, Critical")
                st.download_button("Download example CSV", template_csv(table, True),
                                   file_name=f"{table}_example.csv", mime="text/csv", key=f"csv_example_{table}")
            upload = st.file_uploader(f"Upload {LABELS[table].lower()} CSV", type=["csv"], key=f"csv_upload_{epoch}_{table}")
            if upload is None:
                issues.append(f"Upload the {LABELS[table].lower()} CSV.")
                continue
            try:
                rows, ignored = parse_csv(table, upload.getvalue())
                batches[table] = rows
                st.caption(f"{len(rows):,} records. Preview shows the first 20.")
                st.dataframe(pd.DataFrame(rows[:20]), hide_index=True)
                if ignored:
                    st.caption("Calculated columns will be recalculated: " + ", ".join(ignored))
            except (ValueError, UnicodeError) as error:
                st.error(str(error))
                issues.append(str(error))
    if not selected:
        st.info("Select at least one dataset to begin.")
        return
    if issues:
        st.info("Choose a valid CSV for every selected dataset to enable validation.")
        return
    try:
        checked = service.import_records(token, batches, update_existing=update, dry_run=True)
    except (ValueError, PermissionError) as error:
        st.error(str(error))
        return
    if checked["errors"]:
        st.error("Fix these errors and upload the corrected files. No records have been saved. Showing up to 100 errors.")
        st.dataframe(pd.DataFrame(checked["errors"]), hide_index=True)
        return
    st.success("Validation passed. Review the totals, then confirm the import.")
    st.dataframe(pd.DataFrame(checked["summary"]), hide_index=True)
    signature = hashlib.sha256((repr(batches) + str(update)).encode()).hexdigest()[:20]
    confirmed = st.checkbox("I have reviewed the preview and want to save these records", key=f"csv_confirm_{signature}")
    if st.button("Import validated records", type="primary", disabled=not confirmed):
        try:
            result = service.import_records(token, batches, update_existing=update, dry_run=False)
        except (ValueError, PermissionError) as error:
            st.error(str(error))
            return
        if result["errors"]:
            st.error("The import could not be saved. No changes were made.")
            st.dataframe(pd.DataFrame(result["errors"]), hide_index=True)
        else:
            count = sum(item["New records"] + item["Updated records"] for item in result["summary"])
            st.session_state["flash_message"] = f"CSV import complete: {count:,} records saved. Open the monitoring pages to review them."
            st.session_state["csv_import_epoch"] = epoch + 1
            st.rerun()
