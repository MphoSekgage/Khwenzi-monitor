"""Khwezi Monitor: a role-aware mining safety and equipment teaching prototype.

Run with ``streamlit run app.py``. Data access and permission enforcement live in
service.py; this module displays the data and collects validated form inputs.
"""

from datetime import date, timedelta
import os
from uuid import uuid4

import pandas as pd
import plotly.express as px
import streamlit as st

from service import MiningService


DEPARTMENTS = ["Extraction", "Processing", "Engineering", "Logistics"]
SHIFTS = ["Day", "Night"]
ROLES = ["Administrator", "Safety Officer", "Mining Engineer", "Maintenance Engineer", "Manager"]
COLORS = ["#087F83", "#E2A33A", "#466AB3", "#C96045", "#7F6BA6"]
TABLE_LABELS = {
    "workers": "Worker safety", "incidents": "Incidents", "equipment": "Equipment",
    "maintenance": "Maintenance", "risks": "Risk register", "readings": "Sensor readings",
}
CSS = """
<style>
.stApp {background: #F5F7FA;}
[data-testid="stSidebar"] {background: #11253C;}
[data-testid="stSidebar"] :is(h1,h2,h3,p,label,span) {color: #F2F6FA;}
[data-testid="stSidebar"] [data-baseweb="input"] input {color: #13263E;}
[data-testid="stSidebar"] [data-testid="stDateInput"] input {color: #13263E;}
[data-testid="stSidebar"] button p {color: #13263E;}
[data-testid="stSidebar"] [data-testid="stRadio"] label {padding: 0.16rem 0;}
.block-container {padding-top: 2rem; padding-bottom: 3rem; max-width: 1440px;}
.hero {background: #132C46; color: #fff; padding: 2rem 2.2rem; border-radius: 16px;
       border-left: 6px solid #25AFAB; margin-bottom: 1.5rem;}
.hero h1 {color: #fff; font-size: 2.3rem; padding-top: 0.5rem; padding-bottom: 0.4rem;}
.hero p {color: #DCE9F2; margin: 0; max-width: 850px;}
.eyebrow {font-size: 0.76rem; letter-spacing: .14em; text-transform: uppercase;
          font-weight: 700; color: #9CE0DA;}
.tag {display:inline-block; color:#FCE4B0; border:1px solid #93723E;
      padding:.2rem .7rem; border-radius:100px; font-size:.73rem; margin-left:.7rem;}
[data-testid="stMetric"] {background: white; border: 1px solid #DCE5ED;
   border-radius: 12px; padding: 1.05rem 1.2rem; border-top: 3px solid #087F83;}
[data-testid="stMetricLabel"] {color: #53657A;}
[data-testid="stMetricValue"] {color: #13263E;}
h2 {font-size: 1.5rem !important;} h3 {font-size: 1.15rem !important;}
[data-testid="stExpander"] {background: #FFFFFF; border-radius:12px;}
[data-testid="stForm"] {background: #FFFFFF; border-radius:12px;}
.notice {font-size:.8rem; color:#52667A; margin-top:1rem; border-top:1px solid #D8E3EC;
         padding-top: .9rem;}
</style>
"""


@st.cache_resource
def get_service(data_dir=None):
    """Create one shared service while allowing isolated test/demo data folders."""
    return MiningService(data_dir=data_dir)


def records(service, token, table, start=None, end=None):
    """Read an authorized table, applying a date range only when requested."""
    return service.list_records(token, table, start_date=start, end_date=end)


def date_value(value, fallback=None):
    """Convert an existing ISO value to a date suitable for a date widget."""
    if value is None or str(value) in ("", "None", "NaT", "nan"):
        return fallback or date.today()
    return pd.Timestamp(value).date()


def choice_index(options, value):
    """Return a safe index for a select box populated from an existing row."""
    return options.index(value) if value in options else 0


def show_table(frame, key=None):
    """Display readable column names and an explicit empty state."""
    if frame.empty:
        st.info("No records match this view. Try a wider period or another filter.")
        return
    display = frame.copy()
    for column in ("ppe_compliant", "training_current"):
        if column in display:
            display[column] = display[column].map({1: "Yes", 0: "No", True: "Yes", False: "No"})
    display.columns = [c.replace("_", " ").title().replace("Ppe", "PPE") for c in display.columns]
    st.dataframe(display, hide_index=True, use_container_width=True, key=key)


def chart(frame, x, y, title, color=None, horizontal=False):
    """Draw a consistent interactive bar chart and handle an empty result."""
    if frame.empty:
        st.info("No data is available for this chart.")
        return
    kwargs = {"x": y, "y": x, "orientation": "h"} if horizontal else {"x": x, "y": y}
    figure = px.bar(frame, **kwargs, color=color, color_discrete_sequence=COLORS,
                    title=title, labels={x: x.replace("_", " ").title(), y: y.replace("_", " ").title()})
    figure.update_layout(paper_bgcolor="white", plot_bgcolor="white", font_color="#13263E",
                         margin=dict(l=18, r=18, t=60, b=24), height=340,
                         legend_title_text="", title_font_size=17)
    figure.update_xaxes(gridcolor="#E5ECF1")
    figure.update_yaxes(gridcolor="#E5ECF1")
    st.plotly_chart(figure, use_container_width=True)


def count_by(frame, column, label="count"):
    """Count categories without losing their labels on recent pandas versions."""
    if frame.empty:
        return pd.DataFrame(columns=[column, label])
    return frame.groupby(column, dropna=False).size().reset_index(name=label)


def flash():
    """Display a successful operation once, after the application reruns."""
    message = st.session_state.pop("flash_message", None)
    if message:
        st.success(message)


def save(service, token, table, values, noun):
    """Persist one record, expose validation errors, and refresh successful saves."""
    try:
        new_record = "id" not in values
        if new_record:
            values["id"] = f"{table[:3].upper()}-{uuid4().hex[:8]}"
        identifier = service.save_record(token, table, values, create_only=new_record)
    except (ValueError, PermissionError) as error:
        st.error(str(error))
        return
    st.session_state["flash_message"] = f"{noun} saved · {identifier}"
    st.rerun()


def edit_selection(frame, noun, label_column):
    """Offer a new record or an existing record and return its editable values."""
    options = ["New record"] + frame["id"].tolist() if not frame.empty else ["New record"]
    labels = {} if frame.empty else dict(zip(frame["id"], frame[label_column].astype(str)))
    selected = st.selectbox(f"Choose {noun}", options,
                            format_func=lambda value: value if value == "New record" else f"{value} · {labels[value]}",
                            key=f"select_{noun}")
    if selected == "New record":
        return {}
    return frame.loc[frame["id"] == selected].iloc[0].to_dict()


def filter_table(frame, text, department="All departments", search_columns=None):
    """Apply local department and literal text filters to authorized records."""
    result = frame.copy()
    if department != "All departments" and "department" in result:
        result = result[result["department"] == department]
    if text and not result.empty:
        columns = search_columns or list(result.columns)
        mask = pd.Series(False, index=result.index)
        for column in columns:
            if column in result:
                mask |= result[column].astype(str).str.contains(text, case=False, regex=False, na=False)
        result = result[mask]
    return result


def login_page(service):
    """Render branded login and authenticate a demo or administrator-created user."""
    st.markdown('<div class="hero"><span class="eyebrow">MINN2020A · 2026</span>'
                '<h1>Khwezi Monitor</h1><p>A clearer view of worker safety, equipment health '
                'and the actions that need attention.</p></div>', unsafe_allow_html=True)
    left, right = st.columns([1.1, 1], gap="large")
    with left:
        st.subheader("Sign in to your workspace")
        with st.form("login_form"):
            username = st.text_input("Username", placeholder="e.g. safety")
            password = st.text_input("Password", type="password")
            submitted = st.form_submit_button("Sign in", type="primary", use_container_width=True)
        if submitted:
            token = service.login(username.strip(), password)
            if token:
                st.session_state["token"] = token
                st.rerun()
            st.error("Username or password is incorrect, or the account is inactive.")
    with right:
        st.subheader("Explore the demonstration")
        st.write("Five roles share one system. Each role sees its permitted information and actions.")
        st.dataframe(pd.DataFrame({"Username": ["admin", "safety", "mining", "maintenance", "manager"],
                                   "Role": ROLES}), hide_index=True, use_container_width=True)
        st.caption("Demo password for every listed account: **MineDemo!2026**")
        st.info("All records are synthetic. This educational prototype is not a live mine monitoring or emergency system.")


def sidebar(service, token, user):
    """Build role-aware navigation and a reporting period shared by dated views."""
    with st.sidebar:
        st.markdown("### K H W E Z I")
        st.caption("SAFETY · EQUIPMENT · INSIGHT")
        st.divider()
        st.write(f"**{user['username']}**")
        st.caption(user["role"])
        pages = {"Dashboard": None}
        for label, table in [("Worker safety", "workers"), ("Incidents", "incidents"),
                             ("Equipment", "equipment"), ("Maintenance", "maintenance"),
                             ("Risk register", "risks")]:
            if service.can(token, table):
                pages[label] = table
        pages.update({"Alerts": None, "Analytics & reports": None})
        if user["role"] == "Administrator":
            pages["Users"] = None
        page = st.radio("Workspace", list(pages), key="navigation")
        st.divider()
        st.caption("REPORTING PERIOD")
        start = st.date_input("Start date", value=date(2026, 7, 1), key="period_start")
        end = st.date_input("End date", value=max(date.today(), date(2026, 9, 9)), key="period_end")
        st.caption("Dates filter incidents, readings and dated records. Equipment and worker checks are current snapshots.")
        st.divider()
        st.caption("SYNTHETIC DEMONSTRATION DATA")
        if st.button("Sign out", use_container_width=True):
            service.logout(token)
            st.session_state.clear()
            st.rerun()
    return page, start, end


def availability_frame(equipment):
    """Calculate availability from recorded operating plus downtime hours."""
    result = equipment.copy()
    total = result["operating_hours"] + result["downtime_hours"]
    result["availability_pct"] = (100 * result["operating_hours"] / total.where(total > 0)).round(1)
    return result


def dashboard_page(service, token, start, end):
    """Present authorized safety and equipment indicators with actionable alerts."""
    st.markdown('<div class="hero"><span class="eyebrow">Operations overview</span>'
                '<span class="tag">SYNTHETIC DATA</span><h1>See the signals. Plan the action.</h1>'
                '<p>Bring worker safety, equipment condition and open risks into one daily view.</p></div>',
                unsafe_allow_html=True)
    st.caption(f"Reporting period: {start:%d %b %Y} – {end:%d %b %Y} · Worker and equipment measures show the current snapshot.")
    alerts = service.alerts(token, start_date=start, end_date=end)
    indicators = []
    incident_data = None
    equipment_data = None
    if service.can(token, "incidents"):
        incident_data = records(service, token, "incidents", start, end)
        indicators.append(("Reported incidents", len(incident_data)))
        indicators.append(("Unresolved incidents", int((incident_data["status"] != "Closed").sum())))
    if service.can(token, "equipment"):
        equipment_data = availability_frame(records(service, token, "equipment"))
        average = equipment_data["availability_pct"].mean()
        indicators.append(("Mean availability", "—" if pd.isna(average) else f"{average:.1f}%"))
    if service.can(token, "workers"):
        workers = records(service, token, "workers")
        rate = workers["ppe_compliant"].mean() * 100
        indicators.append(("PPE compliance", "—" if pd.isna(rate) else f"{rate:.0f}%"))
    indicators.append(("Active alerts", len(alerts)))
    columns = st.columns(len(indicators))
    for column, (label, value) in zip(columns, indicators):
        column.metric(label, value)
    st.write("")
    left, right = st.columns(2, gap="large")
    with left:
        if incident_data is not None:
            grouped = count_by(incident_data, "department")
            chart(grouped, "department", "count", "Incidents by department", "department")
        elif equipment_data is not None:
            chart(equipment_data.sort_values("downtime_hours", ascending=False), "name", "downtime_hours",
                  "Where downtime is concentrated", horizontal=True)
    with right:
        if equipment_data is not None:
            chart(equipment_data.sort_values("availability_pct"), "name", "availability_pct",
                  "Equipment availability (%)", horizontal=True)
        elif service.can(token, "risks"):
            risks = records(service, token, "risks", start, end)
            chart(count_by(risks, "band"), "band", "count", "Risk register by band", "band")
    st.subheader("Attention queue")
    st.caption("Automatic rules flag demonstration thresholds. Validate the cause and apply the mine’s approved procedures.")
    if alerts.empty:
        st.success("No alerts match this period and your role’s data access.")
    else:
        show_table(alerts.head(8))
        st.caption(f"Showing {min(8, len(alerts))} of {len(alerts)} alerts. Open Alerts to inspect the full queue.")


def worker_page(service, token):
    """Review worker checks and capture PPE, training and hours for fatigue flags."""
    st.title("Worker safety")
    st.caption("Current worker snapshot · Fatigue flags use recorded hours; this is not a medical assessment.")
    frame = records(service, token, "workers")
    left, right = st.columns([2, 1])
    text = left.text_input("Search workers", placeholder="Name, identifier or department")
    department = right.selectbox("Department", ["All departments"] + DEPARTMENTS, key="workers_department")
    view = filter_table(frame, text, department)
    if "fatigue_status" not in view:
        view = view.copy()
        view["fatigue_status"] = view["hours_worked"].apply(lambda hours: "Flagged" if hours > service.thresholds["fatigue_hours"] else "Within demo limit")
    show_table(view)
    if not service.can(token, "workers", "write"):
        st.info("Your role can view worker safety records. Editing is restricted to authorized operational roles.")
        return
    with st.expander("Add or update a worker check", expanded=frame.empty):
        row = edit_selection(frame, "worker", "name")
        with st.form(f"worker_form_{row.get('id', 'new')}"):
            a, b = st.columns(2)
            name = a.text_input("Worker name", value=row.get("name", ""))
            department = b.selectbox("Worker department", DEPARTMENTS, index=choice_index(DEPARTMENTS, row.get("department")))
            shift = a.selectbox("Shift", SHIFTS, index=choice_index(SHIFTS, row.get("shift")))
            hours = b.number_input("Hours worked in the monitored shift", min_value=0.0, max_value=24.0,
                                   value=float(row.get("hours_worked", 8.0)), step=0.5)
            ppe = a.checkbox("PPE compliant", value=bool(row.get("ppe_compliant", True)))
            training = b.checkbox("Training current", value=bool(row.get("training_current", True)))
            check_date = a.date_input("Check date", value=date_value(row.get("last_check_date")))
            submitted = st.form_submit_button("Save worker", type="primary")
        if submitted:
            values = dict(name=name.strip(), department=department, shift=shift, hours_worked=hours,
                          ppe_compliant=int(ppe), training_current=int(training), last_check_date=check_date.isoformat())
            if row:
                values["id"] = row["id"]
            save(service, token, "workers", values, "Worker")


def incident_page(service, token, start, end):
    """Filter incident history and create or update a classified incident report."""
    st.title("Incidents")
    st.caption(f"Incident dates: {start:%d %b %Y} – {end:%d %b %Y}")
    frame = records(service, token, "incidents", start, end)
    a, b, c = st.columns([2, 1, 1])
    text = a.text_input("Search incidents", placeholder="Description, type or identifier")
    department = b.selectbox("Department", ["All departments"] + DEPARTMENTS, key="incidents_department")
    status_filter = c.selectbox("Status", ["All statuses", "Open", "Investigating", "Closed"], key="incidents_status")
    view = filter_table(frame, text, department)
    if status_filter != "All statuses":
        view = view[view["status"] == status_filter]
    show_table(view)
    if not view.empty:
        monthly = view.copy()
        monthly["month"] = pd.to_datetime(monthly["date"]).dt.strftime("%Y-%m")
        chart(count_by(monthly, "month"), "month", "count", "Reported incidents over time")
    if not service.can(token, "incidents", "write"):
        st.info("Your role can review incident records. Editing is restricted to authorized operational roles.")
        return
    with st.expander("Report or update an incident"):
        all_rows = records(service, token, "incidents")
        row = edit_selection(all_rows, "incident", "description")
        workers = records(service, token, "workers") if service.can(token, "workers") else pd.DataFrame(columns=["id", "name"])
        worker_options = [""] + workers["id"].tolist()
        worker_labels = dict(zip(workers["id"], workers["name"]))
        kinds = ["Injury", "Near miss", "Equipment failure", "Environmental"]
        severities = ["Low", "Medium", "High", "Critical"]
        statuses = ["Open", "Investigating", "Closed"]
        with st.form(f"incident_form_{row.get('id', 'new')}"):
            a, b = st.columns(2)
            incident_date = a.date_input("Incident date", value=date_value(row.get("date")))
            department = b.selectbox("Incident department", DEPARTMENTS, index=choice_index(DEPARTMENTS, row.get("department")))
            worker = a.selectbox("Worker (optional)", worker_options, index=choice_index(worker_options, row.get("worker_id", "")),
                                  format_func=lambda value: "Not linked" if not value else f"{value} · {worker_labels[value]}")
            kind = b.selectbox("Incident type", kinds, index=choice_index(kinds, row.get("kind")))
            incident_shift = a.selectbox("Incident shift", SHIFTS, index=choice_index(SHIFTS, row.get("shift")))
            severity = a.selectbox("Severity", severities, index=choice_index(severities, row.get("severity")))
            status = b.selectbox("Incident status", statuses, index=choice_index(statuses, row.get("status")))
            description = st.text_area("What happened?", value=row.get("description", ""),
                                       placeholder="Record the event, location and immediate response.")
            submitted = st.form_submit_button("Save incident", type="primary")
        if submitted:
            values = dict(date=incident_date.isoformat(), department=department, shift=incident_shift, worker_id=worker, kind=kind,
                          severity=severity, status=status, description=description.strip())
            if row:
                values["id"] = row["id"]
            save(service, token, "incidents", values, "Incident")


def equipment_page(service, token, start, end):
    """Review equipment availability and component checks, and record sensor readings."""
    st.title("Equipment")
    st.caption("Asset condition and availability are the current snapshot. Sensor history uses the reporting period.")
    frame = records(service, token, "equipment")
    a, b = st.columns([2, 1])
    text = a.text_input("Search equipment", placeholder="Asset name, type or identifier")
    department = b.selectbox("Department", ["All departments"] + DEPARTMENTS, key="equipment_department")
    view = availability_frame(filter_table(frame, text, department))
    show_table(view)
    st.caption("Availability = operating hours ÷ (operating hours + downtime hours) × 100. A zero-hour asset has no availability estimate.")
    if service.can(token, "equipment", "write"):
        with st.expander("Add or update an equipment record"):
            row = edit_selection(frame, "equipment", "name")
            kinds = ["Haul truck", "Drill", "Crusher", "Conveyor", "Pump"]
            statuses = ["Operating", "Standby", "Under maintenance", "Out of service"]
            conditions = ["Good", "Needs attention", "Critical"]
            with st.form(f"equipment_form_{row.get('id', 'new')}"):
                a, b = st.columns(2)
                name = a.text_input("Equipment name", value=row.get("name", ""))
                department = b.selectbox("Equipment department", DEPARTMENTS, index=choice_index(DEPARTMENTS, row.get("department")))
                kind = a.selectbox("Equipment type", kinds, index=choice_index(kinds, row.get("kind")))
                status = b.selectbox("Operating status", statuses, index=choice_index(statuses, row.get("status")))
                operating = a.number_input("Operating hours", min_value=0.0, value=float(row.get("operating_hours", 0.0)))
                downtime = b.number_input("Downtime hours", min_value=0.0, value=float(row.get("downtime_hours", 0.0)))
                service_date = a.date_input("Last service date", value=date_value(row.get("last_service_date")))
                interval = b.number_input("Service interval (days)", min_value=1, max_value=3650,
                                           value=int(row.get("service_interval_days", 30)))
                component_values = {}
                for column, label in [("brake_condition", "Brake condition"), ("tyre_condition", "Tyre condition"),
                                      ("engine_condition", "Engine condition")]:
                    if column in frame:
                        component_values[column] = st.selectbox(label, conditions, index=choice_index(conditions, row.get(column, "Good")))
                submitted = st.form_submit_button("Save equipment", type="primary")
            if submitted:
                values = dict(name=name.strip(), department=department, kind=kind, operating_hours=operating,
                              downtime_hours=downtime, status=status, last_service_date=service_date.isoformat(),
                              service_interval_days=interval, **component_values)
                if row:
                    values["id"] = row["id"]
                save(service, token, "equipment", values, "Equipment")
    else:
        st.info("Your role has read-only access to equipment condition.")
    if service.can(token, "readings"):
        st.subheader("Sensor readings")
        readings = records(service, token, "readings", start, end)
        if department != "All departments":
            readings = readings[readings["equipment_id"].isin(view["id"])]
        show_table(readings)
        st.caption("Temperature and vibration flags use configurable teaching thresholds; these are not manufacturer limits.")
        if service.can(token, "readings", "write"):
            reading_form(service, token, frame)


def reading_form(service, token, equipment):
    """Capture a dated temperature and vibration observation for an existing asset."""
    if equipment.empty:
        st.info("Add equipment before recording a sensor reading.")
        return
    with st.expander("Record a sensor reading"):
        labels = dict(zip(equipment["id"], equipment["name"]))
        with st.form("reading_form"):
            asset = st.selectbox("Sensor equipment", equipment["id"].tolist(),
                                 format_func=lambda value: f"{value} · {labels[value]}")
            a, b, c = st.columns(3)
            reading_date = a.date_input("Reading date", value=date.today())
            temperature = b.number_input("Temperature (°C)", min_value=-50.0, max_value=500.0, value=65.0, step=0.5)
            vibration = c.number_input("Vibration (mm/s)", min_value=0.0, max_value=100.0, value=3.0, step=0.1)
            submitted = st.form_submit_button("Save reading", type="primary")
        if submitted:
            save(service, token, "readings", dict(equipment_id=asset, date=reading_date.isoformat(),
                                                 temperature_c=temperature, vibration_mm_s=vibration), "Sensor reading")


def maintenance_page(service, token):
    """Show due work, asset service intervals and editable maintenance jobs."""
    st.title("Maintenance")
    today = date.today()
    st.caption(f"Current schedule as at {today:%d %b %Y} · Due soon means within the next seven days.")
    frame = records(service, token, "maintenance")
    equipment = records(service, token, "equipment")
    pending = frame[frame["status"] != "Completed"]
    due = pd.to_datetime(pending["due_date"]).dt.date
    a, b, c = st.columns(3)
    a.metric("Overdue jobs", int((due < today).sum()))
    b.metric("Due in 7 days", int(((due >= today) & (due <= today + timedelta(days=7))).sum()))
    c.metric("Completed jobs", int((frame["status"] == "Completed").sum()))
    status = st.selectbox("Show maintenance", ["All jobs", "Overdue", "Due in 7 days", "Scheduled", "In progress", "Completed"])
    view = frame.copy()
    if status == "Overdue":
        view = pending[due < today]
    elif status == "Due in 7 days":
        view = pending[(due >= today) & (due <= today + timedelta(days=7))]
    elif status != "All jobs":
        view = frame[frame["status"] == status]
    show_table(view)
    with st.expander("Asset service dates and low availability"):
        asset_view = availability_frame(equipment)
        asset_view["next_service_date"] = pd.to_datetime(asset_view["last_service_date"]) + pd.to_timedelta(asset_view["service_interval_days"], unit="D")
        asset_view["service_overdue"] = asset_view["next_service_date"].dt.date < today
        show_table(asset_view[["id", "name", "availability_pct", "next_service_date", "service_overdue"]])
        st.caption("Service intervals and job schedules are separate checks. Completing a job does not silently rewrite recorded operating hours.")
    if not service.can(token, "maintenance", "write"):
        st.info("Your role can review the maintenance schedule. Editing is restricted to authorized operational roles.")
        return
    if equipment.empty:
        st.info("Add equipment before scheduling maintenance.")
        return
    with st.expander("Schedule or complete maintenance"):
        row = edit_selection(frame, "maintenance job", "description")
        labels = dict(zip(equipment["id"], equipment["name"]))
        options = equipment["id"].tolist()
        statuses = ["Scheduled", "In progress", "Completed"]
        with st.form(f"maintenance_form_{row.get('id', 'new')}"):
            a, b = st.columns(2)
            asset = a.selectbox("Maintenance equipment", options, index=choice_index(options, row.get("equipment_id")),
                                format_func=lambda value: f"{value} · {labels[value]}")
            status = b.selectbox("Job status", statuses, index=choice_index(statuses, row.get("status")))
            created = a.date_input("Job date", value=date_value(row.get("date")))
            due_date = b.date_input("Due date", value=date_value(row.get("due_date"), today + timedelta(days=7)))
            completed = a.date_input("Completion date (used for completed jobs)", value=date_value(row.get("completed_date")))
            description = st.text_area("Maintenance work", value=row.get("description", ""))
            submitted = st.form_submit_button("Save maintenance", type="primary")
        if submitted:
            values = dict(equipment_id=asset, date=created.isoformat(), due_date=due_date.isoformat(),
                          completed_date=completed.isoformat() if status == "Completed" else "",
                          status=status, description=description.strip())
            if row:
                values["id"] = row["id"]
            save(service, token, "maintenance", values, "Maintenance job")


def risk_page(service, token, start, end):
    """Display the risk register and capture likelihood-times-consequence assessments."""
    st.title("Risk register")
    st.caption("Teaching matrix: risk score = likelihood × consequence (each 1–5). Ratings are prioritization aids requiring competent review.")
    frame = records(service, token, "risks", start, end)
    a, b = st.columns([2, 1])
    text = a.text_input("Search risks", placeholder="Hazard, control, owner or identifier")
    department = b.selectbox("Department", ["All departments"] + DEPARTMENTS, key="risks_department")
    show_table(filter_table(frame, text, department))
    if not service.can(token, "risks", "write"):
        st.info("Your role can review risk assessments. Editing is restricted to authorized operational roles.")
        return
    with st.expander("Assess or update a risk"):
        all_rows = records(service, token, "risks")
        row = edit_selection(all_rows, "risk", "hazard")
        statuses = ["Open", "Controlled", "Closed"]
        with st.form(f"risk_form_{row.get('id', 'new')}"):
            a, b = st.columns(2)
            risk_date = a.date_input("Assessment date", value=date_value(row.get("date")))
            department = b.selectbox("Risk department", DEPARTMENTS, index=choice_index(DEPARTMENTS, row.get("department")))
            hazard = st.text_input("Hazard", value=row.get("hazard", ""))
            likelihood = a.slider("Likelihood · 1 rare / 5 almost certain", min_value=1, max_value=5, value=int(row.get("likelihood", 3)))
            consequence = b.slider("Consequence · 1 minor / 5 catastrophic", min_value=1, max_value=5, value=int(row.get("consequence", 3)))
            control = st.text_area("Control measures", value=row.get("control", ""))
            owner = a.text_input("Responsible person", value=row.get("owner", ""))
            status = b.selectbox("Risk status", statuses, index=choice_index(statuses, row.get("status")))
            submitted = st.form_submit_button("Save risk", type="primary")
        st.caption("Low 1–4 · Medium 5–9 · High 10–16 · Critical 17–25. The saved register displays the calculated score and band.")
        if submitted:
            values = dict(date=risk_date.isoformat(), department=department, hazard=hazard.strip(), likelihood=likelihood,
                          consequence=consequence, control=control.strip(), owner=owner.strip(), status=status)
            if row:
                values["id"] = row["id"]
            save(service, token, "risks", values, "Risk assessment")


def alerts_page(service, token, start, end):
    """Show permission-scoped automatic alerts with severity and category filters."""
    st.title("Alerts")
    st.caption("Calculated from records on each visit. Resolve the underlying record to clear a condition. No messages are sent externally.")
    alerts = service.alerts(token, start_date=start, end_date=end)
    a, b, c = st.columns(3)
    a.metric("Active alerts", len(alerts))
    b.metric("Critical", int(alerts["severity"].str.lower().eq("critical").sum()) if not alerts.empty else 0)
    c.metric("Categories", alerts["category"].nunique() if not alerts.empty else 0)
    left, right = st.columns(2)
    severity = left.selectbox("Alert severity", ["All severities"] + sorted(alerts["severity"].unique().tolist()))
    category = right.selectbox("Alert category", ["All categories"] + sorted(alerts["category"].unique().tolist()))
    view = alerts.copy()
    if severity != "All severities":
        view = view[view["severity"] == severity]
    if category != "All categories":
        view = view[view["category"] == category]
    show_table(view)
    if not view.empty:
        chart(count_by(view, "category"), "category", "count", "Alerts by category", "category")
    st.warning("Demonstration thresholds are not validated mine safety limits. Use approved site procedures and manufacturer specifications for real operations.")


def analytics_data(service, token, start, end, department, shift):
    """Build only authorized analysis frames and apply transparent relevant filters."""
    data = {}
    for table in TABLE_LABELS:
        if service.can(token, table):
            dated = table in ("incidents", "readings", "risks")
            data[table] = records(service, token, table, start if dated else None, end if dated else None)
    if "incidents" in data and "workers" in data and "shift" not in data["incidents"]:
        shifts = data["workers"][["id", "shift"]].rename(columns={"id": "worker_id"})
        data["incidents"] = data["incidents"].merge(shifts, on="worker_id", how="left")
        data["incidents"]["shift"] = data["incidents"]["shift"].fillna("Unassigned")
    for table, frame in data.items():
        if department != "All departments" and "department" in frame:
            data[table] = frame[frame["department"] == department]
        if shift != "All shifts" and "shift" in data[table]:
            data[table] = data[table][data[table]["shift"] == shift]
    if "equipment" in data:
        assets = data["equipment"]["id"]
        for table in ("readings", "maintenance"):
            if table in data and department != "All departments":
                data[table] = data[table][data[table]["equipment_id"].isin(assets)]
        data["equipment"] = availability_frame(data["equipment"])
    return data


def analysis_questions(service, token):
    """Offer analysis questions only when their required data is authorized."""
    questions = {"How many active alerts need attention?": "alerts"}
    if service.can(token, "incidents"):
        questions.update({"How many incidents occurred in the selected period?": "incident_count",
                          "Which department reported the most incidents?": "incident_department",
                          "Which incident types occur most often?": "incident_types"})
        if service.can(token, "workers"):
            questions["Which shift is linked to the most incidents?"] = "incident_shift"
    if service.can(token, "workers"):
        questions["What percentage of workers comply with PPE requirements?"] = "ppe"
        questions["Which worker checks need follow-up?"] = "worker_checks"
    if service.can(token, "equipment"):
        questions.update({"Which equipment has the highest downtime?": "downtime",
                          "Which equipment has the lowest availability?": "availability",
                          "What is the average equipment availability?": "mean_availability",
                          "Which equipment needs immediate inspection?": "inspection"})
    if service.can(token, "readings"):
        questions.update({"Which equipment recorded the highest temperature?": "temperature",
                          "Which equipment recorded the highest vibration?": "vibration",
                          "What percentage of monitored equipment has normal sensor readings?": "normal"})
    if service.can(token, "maintenance"):
        questions["Which maintenance jobs are due or overdue?"] = "maintenance"
    if service.can(token, "risks"):
        questions["Where are the highest open risk scores?"] = "risks"
    return questions


def analytics_page(service, token, start, end):
    """Answer selectable questions, chart the evidence, and export authorized CSVs."""
    st.title("Analytics & reports")
    st.caption("Choose a question, inspect its evidence, then download the result or an authorized source table.")
    questions = analysis_questions(service, token)
    question = st.selectbox("Analysis question", list(questions))
    analysis = questions[question]
    a, b = st.columns(2)
    department = a.selectbox("Analysis department", ["All departments"] + DEPARTMENTS)
    use_shift = analysis in ("incident_count", "incident_department", "incident_types", "incident_shift", "ppe", "worker_checks")
    shift = b.selectbox("Analysis shift", ["All shifts"] + SHIFTS, disabled=not use_shift,
                        help="Shift applies to worker checks and incidents linked to a worker.")
    if not use_shift:
        shift = "All shifts"
    if analysis in ("incident_count", "incident_department", "incident_types", "incident_shift", "temperature", "vibration", "normal", "risks"):
        st.caption(f"Dated evidence: {start:%d %b %Y} – {end:%d %b %Y}.")
    else:
        st.caption("This question uses the current snapshot/schedule. The sidebar reporting period does not restrict current assets or worker checks.")
    if analysis.startswith("incident"):
        st.caption("Shift analysis uses the shift recorded on each incident report.")
    data = analytics_data(service, token, start, end, department, shift)
    result = answer_question(service, token, analysis, data, start, end, department)
    if result is not None and not result.empty:
        with st.expander("View the analysis evidence", expanded=False):
            show_table(result)
        st.download_button("Download this analysis (CSV)", result.to_csv(index=False).encode("utf-8-sig"),
                           file_name=f"khwezi_{analysis}_{start}_{end}.csv", mime="text/csv")
    st.divider()
    st.subheader("Source data reports")
    permitted = [table for table in TABLE_LABELS if service.can(token, table)]
    table = st.selectbox("Report dataset", permitted, format_func=lambda value: TABLE_LABELS[value])
    dated = table in ("incidents", "readings", "risks")
    st.caption("Exports contain your role’s authorized source dataset. Department and shift analysis filters above apply to the analysis download only. "
               + ("The reporting period filters this source export." if dated else "This source export contains the current snapshot or full maintenance schedule."))
    report = service.export_csv(token, table, start_date=start if dated else None, end_date=end if dated else None)
    st.download_button("Download source report (CSV)", report, file_name=f"khwezi_{table}.csv", mime="text/csv")


def answer_question(service, token, analysis, data, start, end, department):
    """Compute an answer and its chart, returning the exact evidence for export."""
    if analysis.startswith("incident"):
        frame = data["incidents"].copy()
        if analysis == "incident_count":
            st.metric("Incidents in the selected period", len(frame))
            frame["month"] = pd.to_datetime(frame["date"]).dt.strftime("%Y-%m")
            result = count_by(frame, "month")
            chart(result, "month", "count", "Incident trend")
            return result
        group = {"incident_department": "department", "incident_types": "kind", "incident_shift": "shift"}[analysis]
        result = count_by(frame, group).sort_values("count", ascending=False)
        if not result.empty:
            leaders = result[result["count"] == result["count"].max()]
            st.metric("Highest incident count", int(leaders["count"].iloc[0]))
            st.write("Leading category: " + ", ".join(leaders[group].astype(str)))
        chart(result, group, "count", "Reported incidents", group)
        return result
    if analysis in ("ppe", "worker_checks"):
        frame = data["workers"].copy()
        if analysis == "ppe":
            rate = frame["ppe_compliant"].mean() * 100
            st.metric("Workers compliant with PPE", "—" if pd.isna(rate) else f"{rate:.1f}%")
            st.caption(f"Denominator: {len(frame)} workers in the filtered current snapshot.")
            frame["PPE check"] = frame["ppe_compliant"].map({1: "Compliant", 0: "Non-compliant"})
            result = count_by(frame, "PPE check")
            chart(result, "PPE check", "count", "PPE compliance", "PPE check")
            return result
        result = frame[(frame["ppe_compliant"] == 0) | (frame["training_current"] == 0) | (frame["hours_worked"] > service.thresholds["fatigue_hours"])]
        st.metric("Workers requiring follow-up", len(result))
        show_table(result)
        return result
    if analysis in ("downtime", "availability", "mean_availability", "inspection"):
        frame = data["equipment"].copy()
        if analysis == "inspection":
            mask = frame["status"].eq("Out of service")
            for column in ("brake_condition", "tyre_condition", "engine_condition"):
                if column in frame:
                    mask |= frame[column].eq("Critical")
            if "readings" in data and not data["readings"].empty:
                latest = data["readings"].sort_values(["date", "id"]).drop_duplicates("equipment_id", keep="last")
                sensor_critical = latest["temperature_status"].str.lower().eq("critical") | latest["vibration_status"].str.lower().eq("critical")
                mask |= frame["id"].isin(latest.loc[sensor_critical, "equipment_id"])
            result = frame[mask]
            st.metric("Assets flagged for immediate inspection", len(result))
            st.caption("Flags: a critical component check, out-of-service status, or a critical latest sensor reading within the selected period. Review using site procedures.")
            show_table(result)
            return result
        column = "downtime_hours" if analysis == "downtime" else "availability_pct"
        result = frame[["id", "name", "department", column]].sort_values(column, ascending=analysis != "downtime")
        if analysis == "mean_availability":
            mean = result[column].mean()
            st.metric("Mean equipment availability", "—" if pd.isna(mean) else f"{mean:.1f}%")
            st.caption("Arithmetic mean across assets with recorded hours; this is not a fleet-hour-weighted value.")
        elif not result.empty and result[column].notna().any():
            best = result.iloc[0]
            st.metric("Highest downtime" if analysis == "downtime" else "Lowest availability",
                      f"{best[column]:.1f}" + (" h" if analysis == "downtime" else "%"))
            st.write(f"Asset: **{best['name']}** ({best['id']})")
        chart(result, "name", column, "Equipment comparison", horizontal=True)
        return result
    if analysis in ("temperature", "vibration", "normal"):
        readings = data["readings"].copy()
        if readings.empty:
            st.info("No sensor readings match the selected period and department.")
            return readings
        if analysis == "normal":
            latest = readings.sort_values(["date", "id"]).drop_duplicates("equipment_id", keep="last").copy()
            latest["normal"] = latest["temperature_status"].str.lower().eq("normal") & latest["vibration_status"].str.lower().eq("normal")
            st.metric("Monitored assets with normal readings", f"{latest['normal'].mean() * 100:.1f}%")
            st.caption(f"Denominator: {len(latest)} assets with readings in the period. Uses each asset’s latest reading; unmonitored assets are excluded.")
            result = count_by(latest, "normal")
            result["normal"] = result["normal"].map({True: "Normal", False: "Warning or critical"})
            chart(result, "normal", "count", "Latest sensor condition", "normal")
            return latest
        column = "temperature_c" if analysis == "temperature" else "vibration_mm_s"
        result = readings.groupby("equipment_id", as_index=False)[column].max().sort_values(column, ascending=False)
        if "equipment" in data:
            result = result.merge(data["equipment"][["id", "name"]], left_on="equipment_id", right_on="id", how="left")
        st.metric("Highest recorded " + analysis, f"{result[column].max():.1f}" + (" °C" if analysis == "temperature" else " mm/s"))
        chart(result, "equipment_id", column, "Peak reading by equipment", horizontal=True)
        return result
    if analysis == "maintenance":
        frame = data["maintenance"].copy()
        today = date.today()
        frame = frame[frame["status"] != "Completed"]
        frame["due_state"] = pd.to_datetime(frame["due_date"]).dt.date.apply(
            lambda value: "Overdue" if value < today else "Due in 7 days" if value <= today + timedelta(days=7) else "Later")
        result = frame[frame["due_state"] != "Later"]
        st.metric("Due or overdue maintenance jobs", len(result))
        chart(count_by(result, "due_state"), "due_state", "count", "Maintenance follow-up", "due_state")
        return result
    if analysis == "risks":
        result = data["risks"]
        result = result[result["status"] == "Open"].sort_values("score", ascending=False)
        st.metric("Open risks", len(result))
        chart(result, "hazard", "score", "Open risk scores", "band", horizontal=True)
        return result
    result = service.alerts(token, start_date=start, end_date=end)
    result = filter_table(result, "", department)
    critical = int(result["severity"].str.lower().eq("critical").sum()) if not result.empty else 0
    a, b = st.columns(2)
    a.metric("Active alerts", len(result))
    b.metric("Active critical alerts", critical)
    st.caption("Current worker/equipment/maintenance checks plus incident, risk and sensor evidence in the reporting period.")
    chart(count_by(result, "category"), "category", "count", "Active alert categories", "category")
    if "equipment" in data and not result.empty:
        asset_alerts = result[result["record_id"].isin(data["equipment"]["id"])]
        if not asset_alerts.empty:
            chart(count_by(asset_alerts, "record_id"), "record_id", "count", "Equipment with the most alerts", horizontal=True)
    return result


def users_page(service, token, current_user):
    """Let administrators create accounts and manage roles, passwords and access."""
    st.title("Users")
    st.caption("Administrator workspace · Permission checks also run in the data service.")
    users = service.list_users(token)
    show_table(users)
    with st.expander("Create a user"):
        with st.form("create_user_form"):
            username = st.text_input("New username")
            password = st.text_input("New password", type="password", help="Use at least eight characters.")
            role = st.selectbox("New user role", ROLES)
            submitted = st.form_submit_button("Create user", type="primary")
        if submitted:
            try:
                service.create_user(token, username.strip(), password, role)
                st.session_state["flash_message"] = f"User {username.strip()} created."
                st.rerun()
            except (ValueError, PermissionError) as error:
                st.error(str(error))
    with st.expander("Manage an existing user"):
        username = st.selectbox("Account", users["username"].tolist())
        row = users[users["username"] == username].iloc[0]
        with st.form(f"manage_user_{username}"):
            role = st.selectbox("Account role", ROLES, index=choice_index(ROLES, row["role"]))
            active = st.checkbox("Account active", value=bool(row.get("active", True)))
            password = st.text_input("Replacement password (leave blank to keep current)", type="password")
            submitted = st.form_submit_button("Save account", type="primary")
        if submitted:
            try:
                service.update_user(token, username, role=role, password=password or None, active=active)
                st.session_state["flash_message"] = f"Account {username} updated."
                st.rerun()
            except (ValueError, PermissionError) as error:
                st.error(str(error))


def main():
    """Configure the app, enforce authentication, and dispatch the authorized page."""
    st.set_page_config(page_title="Khwezi Monitor | Mining Safety", page_icon="⛏", layout="wide")
    st.markdown(CSS, unsafe_allow_html=True)
    service = get_service(os.environ.get("KHWEZI_DATA_DIR") or None)
    token = st.session_state.get("token")
    try:
        user = service.get_user(token) if token else None
    except PermissionError:
        st.session_state.clear()
        user = None
        st.info("Your session ended or account access changed. Please sign in again.")
    if not user:
        login_page(service)
        return
    page, start, end = sidebar(service, token, user)
    flash()
    if start > end:
        st.error("Start date must be on or before end date. Adjust the reporting period in the sidebar.")
        return
    try:
        if page == "Dashboard":
            dashboard_page(service, token, start, end)
        elif page == "Worker safety":
            worker_page(service, token)
        elif page == "Incidents":
            incident_page(service, token, start, end)
        elif page == "Equipment":
            equipment_page(service, token, start, end)
        elif page == "Maintenance":
            maintenance_page(service, token)
        elif page == "Risk register":
            risk_page(service, token, start, end)
        elif page == "Alerts":
            alerts_page(service, token, start, end)
        elif page == "Analytics & reports":
            analytics_page(service, token, start, end)
        elif page == "Users":
            users_page(service, token, user)
    except PermissionError as error:
        st.error(str(error))
    st.markdown('<div class="notice">Khwezi Monitor · MINN2020A project · Synthetic demonstration data. '
                'Educational prototype; thresholds and outputs require site-specific validation before operational use.</div>',
                unsafe_allow_html=True)


if __name__ == "__main__":
    main()
