"""Validated SQLite persistence, authentication and pandas analytics.

All public data operations accept a server-generated session token. The current
user is looked up on EVERY operation, so disabled accounts and role changes take
effect immediately. UI visibility is convenience; this module enforces access.
This is a local educational prototype, not an operational mine safety system.
"""
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import hashlib
import hmac
import math
import os
import re
import secrets
import sqlite3
import threading
from contextlib import contextmanager

import pandas as pd
from seed_data import DEMO_PASSWORD, DEMO_USERS, DEPARTMENTS, generate_seed_data

ROLES = ("Administrator", "Safety Officer", "Mining Engineer", "Maintenance Engineer", "Manager")
TABLE_COLUMNS = {
    "workers": ("id", "name", "department", "shift", "hours_worked", "ppe_compliant", "training_current", "last_check_date"),
    "incidents": ("id", "date", "department", "shift", "worker_id", "kind", "severity", "status", "description"),
    "equipment": ("id", "name", "department", "kind", "operating_hours", "downtime_hours", "status", "last_service_date", "service_interval_days", "brake_condition", "tyre_condition", "engine_condition"),
    "maintenance": ("id", "equipment_id", "date", "due_date", "completed_date", "status", "description"),
    "risks": ("id", "date", "department", "hazard", "likelihood", "consequence", "control", "owner", "status"),
    "readings": ("id", "equipment_id", "date", "temperature_c", "vibration_mm_s"),
}
READ_ACCESS = {
    "Administrator": set(TABLE_COLUMNS),
    "Safety Officer": {"workers", "incidents", "risks"},
    "Mining Engineer": set(TABLE_COLUMNS),
    "Maintenance Engineer": {"equipment", "maintenance", "readings"},
    "Manager": set(TABLE_COLUMNS),
}
DATE_COLUMNS = {"workers": "last_check_date", "incidents": "date", "maintenance": "date", "risks": "date", "readings": "date"}
ENUMS = {
    "department": DEPARTMENTS,
    "shift": ("Day", "Night"),
    "incident_kind": ("Injury", "Near miss", "Equipment failure", "Environmental"),
    "severity": ("Low", "Medium", "High", "Critical"),
    "incident_status": ("Open", "Investigating", "Closed"),
    "equipment_kind": ("Haul truck", "Drill", "Crusher", "Conveyor", "Pump"),
    "equipment_status": ("Operating", "Standby", "Under maintenance", "Out of service"),
    "maintenance_status": ("Scheduled", "In progress", "Completed"),
    "risk_status": ("Open", "Controlled", "Closed"),
    "condition": ("Good", "Needs attention", "Critical"),
}
DEFAULT_THRESHOLDS = {
    "fatigue_hours": 12.0,          # worker's recorded shift duration; > 12 h
    "minimum_ppe_pct": 100.0,      # each noncompliant worker is also flagged
    "downtime_hours": 24.0,        # recorded cumulative equipment downtime; > 24 h
    "minimum_availability_pct": 85.0,  # operating / (operating + downtime)
    "service_due_soon_days": 7,    # calendar days; interval per equipment
    "repeat_measurements": 3,     # distinct non-normal measurement dates
}
ALERT_COLUMNS = ("severity", "category", "record_id", "date", "department", "message", "action")


def finite_number(value, label, minimum=None, maximum=None, integer=False):
    """Parse a finite number and enforce inclusive bounds, rejecting booleans."""
    if isinstance(value, bool) or value is None or str(value).strip() == "":
        raise ValueError(f"{label} must be a number.")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label} must be a number.") from None
    if not math.isfinite(number):
        raise ValueError(f"{label} must be finite.")
    if minimum is not None and number < minimum:
        raise ValueError(f"{label} must be at least {minimum}.")
    if maximum is not None and number > maximum:
        raise ValueError(f"{label} must be at most {maximum}.")
    if integer and number != int(number):
        raise ValueError(f"{label} must be a whole number.")
    return int(number) if integer else number


def temperature_status(value):
    """Classify Celsius: <80 Normal; 80 through 100 Warning; >100 Critical."""
    number = finite_number(value, "Temperature", -273.15)
    return "Normal" if number < 80 else "Warning" if number <= 100 else "Critical"


def vibration_status(value):
    """Classify mm/s: <5 Normal; 5 through 8 Warning; >8 Critical."""
    number = finite_number(value, "Vibration", 0)
    return "Normal" if number < 5 else "Warning" if number <= 8 else "Critical"


def availability(operating_hours, downtime_hours):
    """Return operating/(operating+downtime)*100, or None for no observation."""
    operating = finite_number(operating_hours, "Operating hours", 0)
    downtime = finite_number(downtime_hours, "Downtime hours", 0)
    return None if operating + downtime == 0 else 100.0 * operating / (operating + downtime)


def risk_score(likelihood, consequence):
    """Multiply integer likelihood and consequence, each on the 1 to 5 scale."""
    return finite_number(likelihood, "Likelihood", 1, 5, True) * finite_number(consequence, "Consequence", 1, 5, True)


def risk_band(score):
    """Map scores 1-4 Low, 5-9 Medium, 10-16 High and 17-25 Critical."""
    score = finite_number(score, "Risk score", 1, 25, True)
    return "Low" if score <= 4 else "Medium" if score <= 9 else "High" if score <= 16 else "Critical"


def valid_date(value, label="Date", optional=False):
    """Accept a calendar date in exact ISO YYYY-MM-DD format; never guess dates."""
    value = str(value).strip() if value is not None else ""
    if optional and not value:
        return ""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError(f"{label} must use YYYY-MM-DD.")
    try:
        date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{label} is not a valid calendar date.") from None
    return value


def password_hash(password, salt):
    """Derive a SHA-256 PBKDF2 hash using a unique random salt and 210,000 rounds."""
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), 210_000).hex()


class MiningService:
    """Own the database and authenticated sessions for one running application."""

    def __init__(self, data_dir=None, thresholds=None, seed=True):
        """Create schema, validate prototype settings, and seed only a new database."""
        self.data_dir = Path(data_dir or os.environ.get("KHWEZI_DATA_DIR", Path(__file__).parent / "runtime"))
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "khwezi.sqlite3"
        self._sessions = {}
        self._session_lock = threading.Lock()
        self.thresholds = dict(DEFAULT_THRESHOLDS)
        if thresholds:
            unknown = set(thresholds) - set(DEFAULT_THRESHOLDS)
            if unknown:
                raise ValueError(f"Unknown threshold: {', '.join(sorted(unknown))}")
            self.thresholds.update(thresholds)
        for key, value in self.thresholds.items():
            maximum = 100 if key.endswith("_pct") else 24 if key == "fatigue_hours" else None
            self.thresholds[key] = finite_number(value, key, 1 if key == "repeat_measurements" else 0, maximum,
                key in ("service_due_soon_days", "repeat_measurements"))
        self._initialize(seed)

    @contextmanager
    def _connect(self):
        """Open a short-lived connection with foreign keys and a busy timeout."""
        connection = sqlite3.connect(self.db_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self, seed):
        """Create related tables transactionally and mark first-time initialization."""
        with self._connect() as connection:
            connection.executescript("""
            CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS users(username TEXT PRIMARY KEY,role TEXT NOT NULL,
                salt TEXT NOT NULL,password_hash TEXT NOT NULL,active INTEGER NOT NULL DEFAULT 1);
            CREATE TABLE IF NOT EXISTS workers(id TEXT PRIMARY KEY,name TEXT NOT NULL,department TEXT NOT NULL,
                shift TEXT NOT NULL,hours_worked REAL NOT NULL,ppe_compliant INTEGER NOT NULL,
                training_current INTEGER NOT NULL,last_check_date TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS incidents(id TEXT PRIMARY KEY,date TEXT NOT NULL,department TEXT NOT NULL,shift TEXT NOT NULL DEFAULT 'Day',
                worker_id TEXT REFERENCES workers(id),kind TEXT NOT NULL,severity TEXT NOT NULL,
                status TEXT NOT NULL,description TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS equipment(id TEXT PRIMARY KEY,name TEXT NOT NULL,department TEXT NOT NULL,
                kind TEXT NOT NULL,operating_hours REAL NOT NULL,downtime_hours REAL NOT NULL,
                status TEXT NOT NULL,last_service_date TEXT NOT NULL,service_interval_days INTEGER NOT NULL,
                brake_condition TEXT NOT NULL DEFAULT 'Good',tyre_condition TEXT NOT NULL DEFAULT 'Good',engine_condition TEXT NOT NULL DEFAULT 'Good');
            CREATE TABLE IF NOT EXISTS maintenance(id TEXT PRIMARY KEY,equipment_id TEXT NOT NULL REFERENCES equipment(id),
                date TEXT NOT NULL,due_date TEXT NOT NULL,completed_date TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,description TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS risks(id TEXT PRIMARY KEY,date TEXT NOT NULL,department TEXT NOT NULL,
                hazard TEXT NOT NULL,likelihood INTEGER NOT NULL,consequence INTEGER NOT NULL,
                control TEXT NOT NULL,owner TEXT NOT NULL,status TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS readings(id TEXT PRIMARY KEY,equipment_id TEXT NOT NULL REFERENCES equipment(id),
                date TEXT NOT NULL,temperature_c REAL NOT NULL,vibration_mm_s REAL NOT NULL,
                UNIQUE(equipment_id,date));
            CREATE INDEX IF NOT EXISTS incidents_date_idx ON incidents(date);
            CREATE INDEX IF NOT EXISTS readings_equipment_date_idx ON readings(equipment_id,date);
            """)
            # Small additive migration preserves databases created by an older demo.
            additions = {"incidents": {"shift": "Day"}, "equipment": {
                "brake_condition":"Good", "tyre_condition":"Good", "engine_condition":"Good"}}
            for table, fields in additions.items():
                existing = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
                for column, default in fields.items():
                    if column not in existing:
                        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT NOT NULL DEFAULT '{default}'")
            initialized = connection.execute("SELECT value FROM metadata WHERE key='initialized'").fetchone()
            if not initialized:
                if seed:
                    for table, rows in generate_seed_data().items():
                        for row in rows:
                            columns = TABLE_COLUMNS[table]
                            values = [None if key == "worker_id" and row[key] == "" else row[key] for key in columns]
                            connection.execute(f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})", values)
                for username, role in DEMO_USERS.items():
                    salt = secrets.token_hex(16)
                    connection.execute("INSERT INTO users(username,role,salt,password_hash) VALUES(?,?,?,?)",
                        (username, role, salt, password_hash(DEMO_PASSWORD, salt)))
                connection.execute("INSERT INTO metadata VALUES('initialized','1')")

    def login(self, username, password):
        """Verify a salted hash with constant-time comparison and issue an 8-hour token."""
        if not isinstance(username, str) or not isinstance(password, str):
            return None
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM users WHERE username=? AND active=1", (username.strip().lower(),)).fetchone()
        # Perform the same expensive derivation for unknown accounts.
        salt = row["salt"] if row else "0" * 32
        actual = password_hash(password, salt)
        if not row or not hmac.compare_digest(actual, row["password_hash"]):
            return None
        token = secrets.token_urlsafe(32)
        with self._session_lock:
            self._sessions[token] = (row["username"], datetime.now(timezone.utc) + timedelta(hours=8))
        return token

    def logout(self, token):
        """Invalidate only the supplied session; repeated logout is harmless."""
        with self._session_lock:
            self._sessions.pop(token, None)

    def get_user(self, token):
        """Resolve an unexpired token against the current active database account."""
        if not isinstance(token, str):
            raise PermissionError("Please sign in with a valid account.")
        with self._session_lock:
            session = self._sessions.get(token)
        if not session or session[1] <= datetime.now(timezone.utc):
            raise PermissionError("Your session is missing or expired. Please sign in.")
        with self._connect() as connection:
            row = connection.execute("SELECT username,role FROM users WHERE username=? AND active=1", (session[0],)).fetchone()
        if not row:
            raise PermissionError("This account is disabled. Please contact the administrator.")
        return dict(row)

    def can(self, token, table, action="read"):
        """Apply the role matrix; all signed-in roles may view their dashboard/reports."""
        user = self.get_user(token)
        if action not in ("read", "write", "export"):
            raise ValueError("Action must be read, write or export.")
        if table == "users":
            return user["role"] == "Administrator"
        if table in ("dashboard", "reports"):
            return action != "write"
        if table not in TABLE_COLUMNS:
            raise ValueError("Unknown data table.")
        return table in READ_ACCESS[user["role"]] and not (action == "write" and user["role"] == "Manager")

    def _require(self, token, table, action="read"):
        """Reject a prohibited operation before any operational records are read."""
        if not self.can(token, table, action):
            raise PermissionError(f"Your role cannot {action} {table}.")

    def list_records(self, token, table, start_date=None, end_date=None):
        """Read authorized rows with inclusive date bounds and add derived fields.

        Equipment is a current inventory snapshot and has no event-date filter.
        Worker filters use last_check_date; other dated tables use record date.
        """
        self._require(token, table)
        if table not in TABLE_COLUMNS:
            raise ValueError("Use list_users to view accounts.")
        start, end = self._date_range(start_date, end_date)
        query, arguments, terms = f"SELECT * FROM {table}", [], []
        if table in DATE_COLUMNS:
            column = DATE_COLUMNS[table]
            if start:
                terms.append(f"{column}>=?")
                arguments.append(start)
            if end:
                terms.append(f"{column}<=?")
                arguments.append(end)
        if terms:
            query += " WHERE " + " AND ".join(terms)
        with self._connect() as connection:
            frame = pd.read_sql_query(query + " ORDER BY id", connection, params=arguments)
        if table == "risks":
            frame["score"] = frame["likelihood"] * frame["consequence"]
            frame["band"] = frame["score"].map(risk_band)
        elif table == "equipment":
            frame["availability_pct"] = [availability(op, down) for op, down in zip(frame.operating_hours, frame.downtime_hours)]
        elif table == "readings":
            frame["temperature_status"] = frame.temperature_c.map(temperature_status)
            frame["vibration_status"] = frame.vibration_mm_s.map(vibration_status)
        return frame

    def _date_range(self, start_date, end_date):
        """Validate inclusive optional date bounds and reject reversed ranges."""
        start = valid_date(start_date) if start_date else None
        end = valid_date(end_date) if end_date else None
        if start and end and start > end:
            raise ValueError("Start date must be on or before end date.")
        return start, end

    def _validated_record(self, table, values):
        """Normalize complete records, constrain enums/numbers/dates and relations."""
        if not isinstance(values, dict):
            raise ValueError("Record must be a dictionary.")
        values = dict(values)
        if table == "incidents":
            values.setdefault("shift", "Day")
        if table == "equipment":
            for column in ("brake_condition", "tyre_condition", "engine_condition"):
                values.setdefault(column, "Good")
        missing = set(TABLE_COLUMNS[table]) - set(values)
        if missing:
            raise ValueError("Missing fields: " + ", ".join(sorted(missing)))
        row = {column: values[column] for column in TABLE_COLUMNS[table]}
        optional = {"worker_id", "completed_date"}
        for key, value in row.items():
            if key in ("hours_worked", "operating_hours", "downtime_hours", "service_interval_days", "likelihood", "consequence", "temperature_c", "vibration_mm_s", "ppe_compliant", "training_current"):
                continue
            row[key] = str(value).strip() if value is not None else ""
            if key not in optional and not row[key]:
                raise ValueError(f"{key.replace('_', ' ').capitalize()} is required.")
            if len(row[key]) > (1500 if key in ("description", "control") else 200):
                raise ValueError(f"{key} is too long.")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", row["id"]):
            raise ValueError("ID must use 1–40 letters, digits, hyphens or underscores.")
        for key in ("date", "last_check_date", "last_service_date", "due_date", "completed_date"):
            if key in row:
                row[key] = valid_date(row[key], key.replace("_", " "), key == "completed_date")
        for key in ("department", "shift", "severity"):
            if key in row and row[key] not in ENUMS[key]:
                raise ValueError(f"{key} must be one of: {', '.join(ENUMS[key])}.")
        for key in ("brake_condition", "tyre_condition", "engine_condition"):
            if key in row and row[key] not in ENUMS["condition"]:
                raise ValueError(f"{key} must be Good, Needs attention or Critical.")
        singular = {"incidents": "incident", "equipment": "equipment", "maintenance": "maintenance", "risks": "risk"}.get(table)
        for key in ("kind", "status"):
            if key in row and row[key] not in ENUMS[f"{singular}_{key}"]:
                raise ValueError(f"Invalid {table} {key}.")
        bounds = {"hours_worked": (0,24,False), "operating_hours": (0,None,False), "downtime_hours": (0,None,False),
                  "service_interval_days": (1,3650,True), "likelihood": (1,5,True), "consequence": (1,5,True),
                  "temperature_c": (-273.15,None,False), "vibration_mm_s": (0,None,False),
                  "ppe_compliant": (0,1,True), "training_current": (0,1,True)}
        for key, (minimum,maximum,integer) in bounds.items():
            if key in row:
                value = int(row[key]) if key in ("ppe_compliant", "training_current") and isinstance(row[key], bool) else row[key]
                row[key] = finite_number(value, key.replace("_", " "), minimum, maximum, integer)
        if table == "maintenance":
            if row["due_date"] < row["date"]:
                raise ValueError("Maintenance due date cannot precede its recorded date.")
            if row["status"] == "Completed" and not row["completed_date"]:
                raise ValueError("Completed maintenance requires a completion date.")
            if row["status"] != "Completed" and row["completed_date"]:
                raise ValueError("Only completed maintenance may have a completion date.")
            if row["completed_date"] and row["completed_date"] < row["date"]:
                raise ValueError("Completion cannot precede the maintenance record date.")
        if table == "incidents":
            row["worker_id"] = row["worker_id"] or None
        return row

    def save_record(self, token, table, values, create_only=False):
        """Insert or update a validated complete record using parameterized SQL.

        Existing IDs update by default; create_only=True rejects duplicates. IDs
        are stable keys. Foreign keys block references to nonexistent records.
        """
        self._require(token, table, "write")
        if table not in TABLE_COLUMNS:
            raise ValueError("Use the account-management methods for users.")
        row = self._validated_record(table, values)
        columns = TABLE_COLUMNS[table]
        with self._connect() as connection:
            exists = connection.execute(f"SELECT id FROM {table} WHERE id=?", (row["id"],)).fetchone()
            if exists and create_only:
                raise ValueError(f"ID {row['id']} already exists; use Edit to change it.")
            try:
                if exists:
                    changed_columns = columns[1:]
                    connection.execute(f"UPDATE {table} SET {','.join(key+'=?' for key in changed_columns)} WHERE id=?",
                        [row[key] for key in changed_columns] + [row["id"]])
                else:
                    connection.execute(f"INSERT INTO {table}({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
                        [row[key] for key in columns])
            except sqlite3.IntegrityError as error:
                if "readings.equipment_id" in str(error):
                    raise ValueError("This equipment already has a reading on that date; edit the existing measurement.") from None
                raise ValueError("A referenced worker/equipment ID does not exist, or this ID is already used.") from None
        return row["id"]

    def delete_record(self, token, table, record_id):
        """Delete an authorized record while preserving referenced workers/equipment."""
        self._require(token, table, "write")
        if table not in TABLE_COLUMNS:
            raise ValueError("Use update_user to disable an account.")
        with self._connect() as connection:
            try:
                cursor = connection.execute(f"DELETE FROM {table} WHERE id=?", (str(record_id),))
            except sqlite3.IntegrityError:
                raise ValueError("This record is referenced by another record and cannot be deleted.") from None
            if cursor.rowcount == 0:
                raise ValueError("Record ID was not found.")

    def list_users(self, token):
        """Return only account names, roles and active flags; never salts or hashes."""
        self._require(token, "users")
        with self._connect() as connection:
            return pd.read_sql_query("SELECT username,role,active FROM users ORDER BY username", connection)

    def create_user(self, token, username, password, role):
        """Create a unique active account after checking role and password policy."""
        self._require(token, "users", "write")
        username = str(username).strip().lower()
        if not re.fullmatch(r"[a-z][a-z0-9_-]{2,31}", username):
            raise ValueError("Username needs 3–32 lowercase letters, digits, hyphens or underscores, starting with a letter.")
        if role not in ROLES:
            raise ValueError("Choose a valid role.")
        self._validate_password(password)
        salt = secrets.token_hex(16)
        with self._connect() as connection:
            try:
                connection.execute("INSERT INTO users(username,role,salt,password_hash) VALUES(?,?,?,?)",
                    (username,role,salt,password_hash(password,salt)))
            except sqlite3.IntegrityError:
                raise ValueError("This username already exists.") from None
        return username

    def _validate_password(self, password):
        """Require at least 10 characters and bound input to avoid unreasonable work."""
        if not isinstance(password, str) or not 10 <= len(password) <= 256:
            raise ValueError("Password must contain 10–256 characters.")

    def update_user(self, token, username, role=None, password=None, active=None):
        """Change an account, preserving one active admin and revoking changed sessions."""
        self._require(token, "users", "write")
        if role is not None and role not in ROLES:
            raise ValueError("Choose a valid role.")
        if password is not None:
            self._validate_password(password)
        if active is not None and (not isinstance(active, (int, bool)) or active not in (0,1)):
            raise ValueError("Active must be 0 or 1.")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
            if not row:
                raise ValueError("Account was not found.")
            next_role = role if role is not None else row["role"]
            next_active = int(active) if active is not None else row["active"]
            if row["role"] == "Administrator" and row["active"] and (next_role != "Administrator" or not next_active):
                count = connection.execute("SELECT COUNT(*) FROM users WHERE role='Administrator' AND active=1").fetchone()[0]
                if count <= 1:
                    raise ValueError("Keep at least one active administrator.")
            salt = secrets.token_hex(16) if password is not None else row["salt"]
            digest = password_hash(password,salt) if password is not None else row["password_hash"]
            connection.execute("UPDATE users SET role=?,active=?,salt=?,password_hash=? WHERE username=?",
                (next_role,next_active,salt,digest,username))
        if password is not None or not next_active:
            with self._session_lock:
                self._sessions = {key:value for key,value in self._sessions.items() if value[0] != username}

    def export_csv(self, token, table, start_date=None, end_date=None):
        """Export only authorized rows and neutralize spreadsheet formula injection."""
        self._require(token, table, "export")
        frame = self.list_records(token,table,start_date,end_date).copy()
        for column in frame.select_dtypes(include=["object", "string"]).columns:
            frame[column] = frame[column].map(self._safe_csv_cell)
        return frame.to_csv(index=False).encode("utf-8-sig")

    @staticmethod
    def _safe_csv_cell(value):
        """Prefix formula-like text with an apostrophe for safe spreadsheet opening."""
        if isinstance(value,str) and value.lstrip().startswith(("=", "+", "-", "@", "\t", "\r")):
            return "'" + value
        return value

    def alerts(self, token, start_date=None, end_date=None):
        """Derive current actionable alerts from authorized selected observations.

        Latest selected reading drives temperature/vibration warnings. Repeated
        warning counts distinct equipment measurement dates, never page reruns.
        Service schedules use the latest completed task through the report date,
        including historical completions before a chosen start date.
        """
        self.get_user(token)
        start,end = self._date_range(start_date,end_date)
        as_of = date.fromisoformat(end) if end else date.today()
        rows = []
        if self.can(token,"workers"):
            workers = self.list_records(token,"workers",start,end)
            for row in workers.to_dict("records"):
                if row["hours_worked"] > self.thresholds["fatigue_hours"]:
                    rows.append(self._alert("High","Fatigue",row["id"],row["last_check_date"],row["department"],
                        f"{row['name']}: {row['hours_worked']:g} recorded shift hours exceeds {self.thresholds['fatigue_hours']:g} h.","Review duty and rest arrangements."))
                if not row["ppe_compliant"]:
                    rows.append(self._alert("High","PPE",row["id"],row["last_check_date"],row["department"],
                        f"{row['name']}: PPE check recorded as noncompliant.","Check PPE before work resumes."))
                if not row["training_current"]:
                    rows.append(self._alert("Medium","Training",row["id"],row["last_check_date"],row["department"],
                        f"{row['name']}: training is not current.","Arrange refresher training and review task authorization."))
            if not workers.empty:
                compliance = 100.0 * workers.ppe_compliant.mean()
                if compliance < self.thresholds["minimum_ppe_pct"]:
                    rows.append(self._alert("High","PPE compliance","WORKFORCE",str(as_of),"All authorized departments",
                        f"PPE compliance is {compliance:.1f}% against the {self.thresholds['minimum_ppe_pct']:g}% prototype target.","Review recorded PPE exceptions with supervisors."))
        if self.can(token,"incidents"):
            incidents = self.list_records(token,"incidents",start,end)
            for row in incidents.to_dict("records"):
                if row["status"] != "Closed" and row["severity"] in ("High","Critical"):
                    rows.append(self._alert(row["severity"],"Active incident",row["id"],row["date"],row["department"],
                        f"{row['kind']}: {row['status'].lower()} — {row['description']}","Escalate to the responsible supervisor and track corrective actions."))
        if self.can(token,"risks"):
            for row in self.list_records(token,"risks",start,end).to_dict("records"):
                if row["status"] == "Open" and row["band"] in ("High","Critical"):
                    rows.append(self._alert(row["band"],"Risk",row["id"],row["date"],row["department"],
                        f"{row['hazard']}: L{row['likelihood']} × C{row['consequence']} = {row['score']} ({row['band']}).",row["control"]))
        if self.can(token,"equipment"):
            equipment = self.list_records(token,"equipment")
            equipment_map = equipment.set_index("id").to_dict("index")
            readings = self.list_records(token,"readings",start,end)
            if not readings.empty:
                for equipment_id, history in readings.groupby("equipment_id"):
                    row = history.sort_values(["date","id"]).iloc[-1]
                    machine = equipment_map[equipment_id]
                    for label,value_key,state_key,unit in (("Temperature","temperature_c","temperature_status","°C"),("Vibration","vibration_mm_s","vibration_status","mm/s")):
                        if row[state_key] != "Normal":
                            rows.append(self._alert("Critical" if row[state_key] == "Critical" else "Medium",label,equipment_id,row["date"],machine["department"],
                                f"{machine['name']}: {row[value_key]:g} {unit} ({row[state_key]}).","Inspect the machine and follow the mine's approved response procedure."))
                    abnormal = history[(history.temperature_status != "Normal") | (history.vibration_status != "Normal")]
                    count = abnormal["date"].nunique()
                    if count >= self.thresholds["repeat_measurements"]:
                        rows.append(self._alert("High","Repeated measurements",equipment_id,row["date"],machine["department"],
                            f"{machine['name']}: abnormal readings on {count} distinct dates in the selected period.","Investigate the recurring pattern and arrange condition-based inspection."))
            # A completed historical task remains relevant to the current schedule.
            all_maintenance = self.list_records(token,"maintenance",None,str(as_of))
            for row in equipment.to_dict("records"):
                for component in ("brake", "tyre", "engine"):
                    condition = row[f"{component}_condition"]
                    if condition != "Good":
                        rows.append(self._alert("Critical" if condition == "Critical" else "Medium","Equipment condition",row["id"],str(as_of),row["department"],
                            f"{row['name']}: {component} check recorded as {condition.lower()} (current inventory).","Inspect this component and review equipment operating status."))
                percent = row["availability_pct"]
                if pd.notna(percent) and percent < self.thresholds["minimum_availability_pct"]:
                    rows.append(self._alert("Medium","Availability",row["id"],str(as_of),row["department"],
                        f"{row['name']}: availability {percent:.1f}% is below {self.thresholds['minimum_availability_pct']:g}% (current cumulative hours).","Review downtime causes and recovery actions."))
                if row["downtime_hours"] > self.thresholds["downtime_hours"]:
                    rows.append(self._alert("High","Downtime",row["id"],str(as_of),row["department"],
                        f"{row['name']}: {row['downtime_hours']:g} cumulative downtime hours exceeds {self.thresholds['downtime_hours']:g} h.","Prioritize fault diagnosis and record a maintenance task."))
                completed = all_maintenance[(all_maintenance.equipment_id == row["id"]) &
                    (all_maintenance.status == "Completed") & (all_maintenance.completed_date <= str(as_of))]
                anchors = completed.completed_date.tolist()
                if row["last_service_date"] <= str(as_of):
                    anchors.append(row["last_service_date"])
                if not anchors:
                    continue
                last_service = date.fromisoformat(max(anchors))
                due = last_service + timedelta(days=int(row["service_interval_days"]))
                remaining = (due-as_of).days
                if remaining <= self.thresholds["service_due_soon_days"]:
                    label = "overdue" if remaining < 0 else "due today" if remaining == 0 else f"due in {remaining} days"
                    rows.append(self._alert("High" if remaining < 0 else "Medium","Service schedule",row["id"],str(due),row["department"],
                        f"{row['name']}: interval service {label}; latest service {last_service}, interval {row['service_interval_days']} calendar days.","Review the service schedule and arrange the next task."))
            for row in all_maintenance.to_dict("records"):
                if row["status"] != "Completed" and row["due_date"] < str(as_of) and (not start or row["date"] >= start):
                    machine = equipment_map[row["equipment_id"]]
                    rows.append(self._alert("High","Overdue task",row["id"],row["due_date"],machine["department"],
                        f"{machine['name']}: task {row['id']} due {row['due_date']} remains {row['status'].lower()}.","Assign an owner and update task progress."))
        frame = pd.DataFrame(rows, columns=ALERT_COLUMNS)
        if not frame.empty:
            frame["_priority"] = frame.severity.map({"Critical":0,"High":1,"Medium":2,"Low":3})
            frame = frame.sort_values(["_priority","category","record_id"]).drop(columns="_priority").reset_index(drop=True)
        return frame

    @staticmethod
    def _alert(severity,category,record_id,record_date,department,message,action):
        """Build one uniform alert row for table display and CSV reporting."""
        return dict(zip(ALERT_COLUMNS,(severity,category,record_id,record_date,department,message,action)))

    def dashboard(self, token, start_date=None, end_date=None):
        """Aggregate only authorized tables; denominator-zero measures remain None."""
        self.get_user(token)
        self._date_range(start_date,end_date)
        metrics = {}
        result = {"metrics":metrics,
            "department_incidents":pd.DataFrame(columns=["department","count"]),
            "shift_incidents":pd.DataFrame(columns=["shift","count"]),
            "monthly_incidents":pd.DataFrame(columns=["month","count"]),
            "incident_severity":pd.DataFrame(columns=["severity","count"]),
            "equipment_availability":pd.DataFrame(columns=["id","name","department","availability_pct"]),
            "risk_distribution":pd.DataFrame(columns=["band","count"]),
            "equipment_alert_ranking":pd.DataFrame(columns=["equipment_id","name","abnormal_measurements"]),
            "department_risk_downtime":pd.DataFrame(columns=["department","high_risk_count","downtime_hours"])}
        if self.can(token,"workers"):
            frame = self.list_records(token,"workers",start_date,end_date)
            metrics.update({"Workers":len(frame),"PPE compliance (%)":None if frame.empty else round(100*frame.ppe_compliant.mean(),1),
                "Training current (%)":None if frame.empty else round(100*frame.training_current.mean(),1),
                "Fatigue flags":int((frame.hours_worked > self.thresholds["fatigue_hours"]).sum())})
        if self.can(token,"incidents"):
            frame = self.list_records(token,"incidents",start_date,end_date)
            metrics.update({"Incidents":len(frame),"Open incidents":int((frame.status != "Closed").sum()),
                "High / critical incidents":int(frame.severity.isin(["High","Critical"]).sum())})
            result["department_incidents"] = frame.groupby("department").size().reset_index(name="count")
            result["shift_incidents"] = frame.groupby("shift").size().reset_index(name="count")
            result["incident_severity"] = frame.groupby("severity").size().reset_index(name="count")
            months = frame.assign(month=frame.date.str.slice(0,7))
            result["monthly_incidents"] = months.groupby("month").size().reset_index(name="count")
        if self.can(token,"equipment"):
            frame = self.list_records(token,"equipment")
            fleet_availability = availability(frame.operating_hours.sum(),frame.downtime_hours.sum())
            metrics.update({"Equipment units":len(frame),"Fleet availability (%)":None if fleet_availability is None else round(fleet_availability,1),
                "Downtime hours":float(frame.downtime_hours.sum()),"Units under maintenance":int((frame.status == "Under maintenance").sum())})
            result["equipment_availability"] = frame[["id","name","department","availability_pct"]].copy()
            maintenance = self.list_records(token,"maintenance",start_date,end_date)
            metrics["Open maintenance tasks"] = int((maintenance.status != "Completed").sum())
            readings = self.list_records(token,"readings",start_date,end_date)
            abnormal = readings[(readings.temperature_status != "Normal") | (readings.vibration_status != "Normal")]
            ranking = abnormal.groupby("equipment_id")["date"].nunique().reset_index(name="abnormal_measurements")
            ranking = ranking.merge(frame[["id","name"]],left_on="equipment_id",right_on="id",how="left")
            result["equipment_alert_ranking"] = ranking[["equipment_id","name","abnormal_measurements"]].sort_values("abnormal_measurements",ascending=False)
        if self.can(token,"risks"):
            frame = self.list_records(token,"risks",start_date,end_date)
            metrics.update({"Risk assessments":len(frame),"Open high / critical risks":int(((frame.status == "Open") & frame.band.isin(["High","Critical"])).sum())})
            result["risk_distribution"] = frame.groupby("band").size().reset_index(name="count")
            if self.can(token,"equipment"):
                high = frame[(frame.status == "Open") & frame.band.isin(["High","Critical"])].groupby("department").size().reset_index(name="high_risk_count")
                downtime = self.list_records(token,"equipment").groupby("department")["downtime_hours"].sum().reset_index()
                result["department_risk_downtime"] = downtime.merge(high,on="department",how="outer").fillna(0)
        result["alert_count"] = len(self.alerts(token,start_date,end_date))
        return result
