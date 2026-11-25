import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit


ROOT = Path(__file__).resolve().parent
DATABASE = ROOT / "tech_surveillance.db"
MAX_BODY_SIZE = 1_000_000


def connect_database():
    connection = sqlite3.connect(DATABASE, timeout=10)
    connection.row_factory = sqlite3.Row
    return connection


def initialize_database():
    with connect_database() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS records (
                id TEXT PRIMARY KEY,
                type TEXT NOT NULL CHECK (type IN ('employee', 'client')),
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class RequestHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def send_json(self, status, value):
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if self.headers.get("Origin") == "null":
            self.send_header("Access-Control-Allow-Origin", "null")
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ValueError("Invalid request length.") from None
        if length < 1 or length > MAX_BODY_SIZE:
            raise ValueError("Request body must be between 1 byte and 1 MB.")
        try:
            value = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ValueError("Request body must contain valid JSON.") from None
        if not isinstance(value, dict):
            raise ValueError("Request body must be a JSON object.")
        return value

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/api/health":
            self.send_json(200, {"ok": True})
            return
        if path == "/api/records":
            with connect_database() as connection:
                rows = connection.execute(
                    "SELECT payload FROM records ORDER BY created_at DESC"
                ).fetchall()
            self.send_json(200, [json.loads(row["payload"]) for row in rows])
            return
        if path.startswith("/api/"):
            self.send_json(404, {"error": "API route not found."})
            return
        if path not in ("/", "/index.html"):
            self.send_json(404, {"error": "File not found."})
            return
        self.path = "/index.html"
        super().do_GET()

    def do_OPTIONS(self):
        if self.headers.get("Origin") != "null" or not urlsplit(self.path).path.startswith("/api/"):
            self.send_json(403, {"error": "Cross-origin request not allowed."})
            return
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "null")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Max-Age", "600")
        self.end_headers()

    def do_POST(self):
        if urlsplit(self.path).path != "/api/records":
            self.send_json(404, {"error": "API route not found."})
            return
        try:
            record = self.read_json()
            record_type = record.get("type")
            name = str(record.get("name", "")).strip()
            if record_type not in ("employee", "client") or not name:
                raise ValueError("A valid record type and name are required.")

            now = timestamp()
            record.update(
                id=str(record.get("id") or uuid.uuid4()),
                type=record_type,
                name=name,
                status=record.get("status")
                if record.get("status") in ("Active", "Inactive")
                else "Active",
                createdAt=str(record.get("createdAt") or now),
                updatedAt=now,
            )
            with connect_database() as connection:
                connection.execute(
                    "INSERT OR IGNORE INTO records (id, type, payload, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (
                        record["id"],
                        record_type,
                        json.dumps(record, ensure_ascii=False),
                        record["createdAt"],
                    ),
                )
                row = connection.execute(
                    "SELECT payload FROM records WHERE id = ?", (record["id"],)
                ).fetchone()
            if row is None:
                self.send_json(500, {"error": "Could not save this record."})
                return
            self.send_json(201, json.loads(row["payload"]))
        except ValueError as error:
            self.send_json(400, {"error": str(error)})
        except sqlite3.Error:
            self.log_error("Database error while creating a record.")
            self.send_json(500, {"error": "Database error while saving the record."})

    def do_PUT(self):
        path = urlsplit(self.path).path
        prefix = "/api/records/"
        if not path.startswith(prefix):
            self.send_json(404, {"error": "API route not found."})
            return
        record_id = unquote(path[len(prefix) :])
        try:
            changes = self.read_json()
            with connect_database() as connection:
                row = connection.execute(
                    "SELECT type, payload FROM records WHERE id = ?", (record_id,)
                ).fetchone()
                if row is None:
                    self.send_json(404, {"error": "Record not found."})
                    return
                record = json.loads(row["payload"])
                record.update(changes)
                if not str(record.get("name", "")).strip():
                    raise ValueError("A name is required.")
                record["id"] = record_id
                record["type"] = row["type"]
                record["status"] = (
                    record.get("status")
                    if record.get("status") in ("Active", "Inactive")
                    else "Active"
                )
                record["updatedAt"] = timestamp()
                connection.execute(
                    "UPDATE records SET payload = ? WHERE id = ?",
                    (json.dumps(record, ensure_ascii=False), record_id),
                )
            self.send_json(200, record)
        except ValueError as error:
            self.send_json(400, {"error": str(error)})
        except sqlite3.Error:
            self.log_error("Database error while updating a record.")
            self.send_json(500, {"error": "Database error while updating the record."})

    def do_DELETE(self):
        path = urlsplit(self.path).path
        prefix = "/api/records/"
        if not path.startswith(prefix):
            self.send_json(404, {"error": "API route not found."})
            return
        record_id = unquote(path[len(prefix) :])
        try:
            with connect_database() as connection:
                result = connection.execute(
                    "DELETE FROM records WHERE id = ?", (record_id,)
                )
            if result.rowcount == 0:
                self.send_json(404, {"error": "Record not found."})
                return
            self.send_response(204)
            if self.headers.get("Origin") == "null":
                self.send_header("Access-Control-Allow-Origin", "null")
            self.end_headers()
        except sqlite3.Error:
            self.log_error("Database error while deleting a record.")
            self.send_json(500, {"error": "Database error while deleting the record."})


if __name__ == "__main__":
    initialize_database()
    port = int(os.environ.get("PORT", 8000))
    server = ThreadingHTTPServer(("0.0.0.0", port), RequestHandler)
    print(f"Tech Surveillance is available on port {port}")
    print("On other devices, open http://<this-computer's-LAN-IP>:8000")
    print("No login is enabled; only share this server on a trusted network.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping Tech Surveillance server.")
        server.server_close()
