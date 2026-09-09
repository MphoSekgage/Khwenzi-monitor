"""Deterministic, deliberately fictional records for the Khwezi teaching demo.

No names or operational records describe real workers or a real mine. CSV files
are an inspectable copy of these generated inputs; SQLite is the live data store.
"""
from datetime import date, timedelta
from pathlib import Path
import csv

DEMO_PASSWORD = "MineDemo!2026"
DEMO_DATE = "2026-09-09"
DEPARTMENTS = ("Extraction", "Processing", "Engineering", "Logistics")
DEMO_USERS = {"admin": "Administrator", "safety": "Safety Officer",
              "mining": "Mining Engineer", "maintenance": "Maintenance Engineer",
              "manager": "Manager"}


def generate_seed_data():
    """Return repeatable synthetic tables, including dated measurement history."""
    tables = {name: [] for name in ("workers", "incidents", "equipment", "maintenance", "risks", "readings")}
    for i in range(1, 41):
        tables["workers"].append(dict(id=f"W{i:03}", name=f"Synthetic Worker {i:02}",
            department=DEPARTMENTS[(i-1) % 4], shift="Night" if i % 3 == 0 else "Day",
            hours_worked=[8, 9, 10, 11, 12, 13, 14][i % 7], ppe_compliant=int(i % 7 != 0),
            training_current=int(i % 9 != 0), last_check_date=DEMO_DATE))
    incident_kinds = ("Injury", "Near miss", "Equipment failure", "Environmental")
    severity = ("Low", "Medium", "High", "Critical")
    for i in range(1, 37):
        day = date(2026, 7, 1) + timedelta(days=(i-1)*2)
        tables["incidents"].append(dict(id=f"I{i:03}", date=day.isoformat(),
            department=DEPARTMENTS[(i-1) % 4], shift="Night" if i % 3 == 0 else "Day", worker_id=f"W{((i-1)%40)+1:03}",
            kind=incident_kinds[(i-1) % 4], severity=severity[(i*3) % 4],
            status="Closed" if i <= 26 else ("Investigating" if i % 2 else "Open"),
            description=f"SYNTHETIC scenario {i:02}: {incident_kinds[(i-1)%4].lower()} recorded for a classroom exercise."))
    kinds = ("Haul truck", "Drill", "Crusher", "Conveyor", "Pump")
    for i in range(1, 17):
        tables["equipment"].append(dict(id=f"E{i:03}", name=f"Demo {kinds[(i-1)%5]} {i:02}",
            department=DEPARTMENTS[(i-1) % 4], kind=kinds[(i-1) % 5],
            operating_hours=round(110+i*13.5, 1), downtime_hours=[0, 4, 8, 18, 36, 70][i%6],
            status="Under maintenance" if i in (6,12) else "Operating",
            last_service_date=(date(2026, 9, 9)-timedelta(days=12+i*4)).isoformat(),
            service_interval_days=45,
            brake_condition="Critical" if i == 6 else "Good",
            tyre_condition="Needs attention" if i in (3,7) else "Good",
            engine_condition="Needs attention" if i in (4,12) else "Good"))
        for j in range(5):
            # E004 repeatedly overheats; E007 repeatedly vibrates; boundary values
            # are deliberately present so the demonstration can explain them.
            temperature = [74, 80, 88, 100, 106][(i+j)%5] if i == 4 else [65, 72, 79, 84, 94][(i+j)%5]
            vibration = [4.4, 5.0, 7.2, 8.0, 8.8][(i+j)%5] if i == 7 else [2.2, 3.1, 4.9, 5.3, 6.2][(i+j)%5]
            tables["readings"].append(dict(id=f"R{i:03}-{j+1}", equipment_id=f"E{i:03}",
                date=(date(2026,9,1)+timedelta(days=j*2)).isoformat(),
                temperature_c=temperature, vibration_mm_s=vibration))
    for i in range(1, 13):
        completed = i <= 5
        tables["maintenance"].append(dict(id=f"M{i:03}", equipment_id=f"E{i:03}",
            date=f"2026-09-{min(i,9):02}", due_date=f"2026-09-{min(i+2,20):02}",
            completed_date=f"2026-09-{i+1:02}" if completed else "",
            status="Completed" if completed else ("In progress" if i % 2 == 0 else "Scheduled"),
            description=f"SYNTHETIC preventive inspection and lubrication task {i:02}."))
    hazards = ("Mobile equipment interaction", "Dust exposure", "Unexpected machinery movement",
               "Slip or trip", "Conveyor entanglement", "Excessive noise", "Falling material", "Fatigue")
    for i, hazard in enumerate(hazards, 1):
        tables["risks"].append(dict(id=f"K{i:03}", date=f"2026-09-{i:02}",
            department=DEPARTMENTS[(i-1)%4], hazard=hazard,
            likelihood=[1,2,3,4,5,2,4,3][i-1], consequence=[2,3,4,5,5,4,4,3][i-1],
            control=f"Demo control {i:02}: isolate, inspect and brief the work team.",
            owner=f"Synthetic Supervisor {(i-1)%4+1}", status="Controlled" if i in (1,6) else "Open"))
    return tables


def write_seed_csvs(destination):
    """Write each seed table as UTF-8 CSV for inspection and reproducibility."""
    folder = Path(destination)
    folder.mkdir(parents=True, exist_ok=True)
    for name, rows in generate_seed_data().items():
        with (folder / f"{name}.csv").open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)


if __name__ == "__main__":
    write_seed_csvs(Path(__file__).parent / "data")
