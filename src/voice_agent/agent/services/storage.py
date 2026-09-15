"""CSV storage: technicians, booked service calls, and urgent requests emailed to the team."""

import csv
import re
import threading
from dataclasses import dataclass
from pathlib import Path

SERVICE_CALL_FIELDS = [
    "id",
    "created_at",
    "call_id",
    "status",  # booked or cancelled
    "customer_name",
    "phone",  # the number the technician calls
    "calling_from",  # the number the customer called from; used to find their calls later
    "address",
    "issue",
    "technician",
    "calendar_id",
    "event_id",
    "start",
    "end",
]

URGENT_REQUEST_FIELDS = ["id", "created_at", "call_id", "customer_name", "phone", "calling_from", "address", "issue"]


@dataclass(frozen=True)
class Technician:
    name: str
    calendar_id: str


class Storage:
    def __init__(self, data_dir: Path):
        self.technicians_file = data_dir / "technicians.csv"
        self.service_calls_file = data_dir / "service_calls.csv"
        self.urgent_requests_file = data_dir / "urgent_requests.csv"
        # Held around check-then-write sequences (e.g. booking) so two calls in this process can't interleave.
        self.lock = threading.Lock()

    def technicians(self) -> list[Technician]:
        technicians = [
            Technician(name=row["name"].strip(), calendar_id=row["calendar_id"].strip())
            for row in read_rows(self.technicians_file)
            if row.get("name", "").strip() and row.get("calendar_id", "").strip()
        ]
        if not technicians:
            raise ValueError(f"No technicians with a name and calendar_id in {self.technicians_file}")
        return technicians

    def service_calls(self, **filters: str) -> list[dict[str, str]]:
        return read_rows(self.service_calls_file, **filters)

    def service_calls_for_phone(self, number: str) -> list[dict[str, str]]:
        """Booked service calls where the technician calls this number or the customer called from it."""
        return [
            row
            for row in self.service_calls(status="booked")
            if same_phone(number, row.get("phone", "")) or same_phone(number, row.get("calling_from", ""))
        ]

    def add_service_call(self, row: dict[str, str]) -> None:
        append_row(self.service_calls_file, SERVICE_CALL_FIELDS, row)

    def update_service_call(self, service_call_id: str, **changes: str) -> None:
        rows = read_rows(self.service_calls_file)
        for row in rows:
            if row["id"] == service_call_id:
                row.update(changes)
        write_rows(self.service_calls_file, SERVICE_CALL_FIELDS, rows)

    def urgent_requests(self, **filters: str) -> list[dict[str, str]]:
        return read_rows(self.urgent_requests_file, **filters)

    def add_urgent_request(self, row: dict[str, str]) -> None:
        append_row(self.urgent_requests_file, URGENT_REQUEST_FIELDS, row)


def same_phone(a: str, b: str) -> bool:
    """Compare phone numbers by their last 10 digits, so '+1 (917) 261-8204' matches '19172618204'."""
    a_digits, b_digits = re.sub(r"\D", "", a)[-10:], re.sub(r"\D", "", b)[-10:]
    return len(a_digits) >= 7 and a_digits == b_digits


def read_rows(path: Path, **filters: str) -> list[dict[str, str]]:
    """Rows whose columns equal every filter value. A missing file has no rows."""
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as file:
        return [row for row in csv.DictReader(file) if all(row.get(k) == v for k, v in filters.items())]


def write_rows(path: Path, fields: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fields, restval="", extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)  # never leave a half-written file


def append_row(path: Path, fields: list[str], row: dict[str, str]) -> None:
    if path.exists():
        with path.open(newline="", encoding="utf-8") as file:
            header = next(csv.reader(file), [])
        if header != fields:  # a column was added since the file was created: rewrite with the new header
            write_rows(path, fields, [*read_rows(path), row])
            return
    else:
        write_rows(path, fields, [])
    with path.open("a", newline="", encoding="utf-8") as file:
        csv.DictWriter(file, fieldnames=fields, restval="", extrasaction="ignore").writerow(row)
