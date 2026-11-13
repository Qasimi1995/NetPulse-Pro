import csv
import sqlite3
from typing import Tuple

DB_NAME = "netpulse_data.db"


def get_connection() -> sqlite3.Connection:
  conn = sqlite3.connect(DB_NAME, timeout=10.0)
  conn.execute("PRAGMA journal_mode=WAL;")
  conn.execute("PRAGMA synchronous=NORMAL;")
  return conn


def init_db() -> None:
  with get_connection() as conn:
    conn.execute("""
            CREATE TABLE IF NOT EXISTS bandwidth_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                bytes_sent INTEGER NOT NULL,
                bytes_recv INTEGER NOT NULL
            )
        """)
    conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_records_timestamp
            ON bandwidth_records (timestamp)
        """)
    conn.commit()


def insert_record(bytes_sent: int, bytes_recv: int) -> None:
  if bytes_sent <= 0 and bytes_recv <= 0:
    return
  with get_connection() as conn:
    conn.execute(
        "INSERT INTO bandwidth_records (bytes_sent, bytes_recv) VALUES (?, ?)",
        (bytes_sent, bytes_recv),
    )
    conn.commit()


def query_range(
    start_datetime_str: str, end_datetime_str: str
) -> Tuple[int, int]:
  s_clean = (
      start_datetime_str
      if " " in start_datetime_str
      else f"{start_datetime_str} 00:00:00"
  )
  e_clean = (
      end_datetime_str
      if " " in end_datetime_str
      else f"{end_datetime_str} 23:59:59"
  )

  with get_connection() as conn:
    cursor = conn.cursor()
    cursor.execute(
        """
            SELECT SUM(bytes_sent), SUM(bytes_recv)
            FROM bandwidth_records
            WHERE timestamp BETWEEN ? AND ?
        """,
        (s_clean, e_clean),
    )
    row = cursor.fetchone()
    sent = row[0] if (row and row[0]) else 0
    recv = row[1] if (row and row[1]) else 0
    return sent, recv


def query_today_total() -> int:
  with get_connection() as conn:
    cursor = conn.cursor()
    cursor.execute("""
            SELECT SUM(bytes_sent) + SUM(bytes_recv)
            FROM bandwidth_records
            WHERE date(timestamp) = date('now', 'localtime')
        """)
    val = cursor.fetchone()[0]
    return int(val) if val else 0


def query_total_lifetime_usage() -> Tuple[int, int]:
  """Returns (total_bytes_sent, total_bytes_recv) across all recorded history."""
  with get_connection() as conn:
    cursor = conn.cursor()
    cursor.execute("SELECT SUM(bytes_sent), SUM(bytes_recv) FROM bandwidth_records")
    row = cursor.fetchone()
    sent = row[0] if (row and row[0]) else 0
    recv = row[1] if (row and row[1]) else 0
    return sent, recv


def query_tracked_usage_since(reset_timestamp: str) -> Tuple[int, int]:
  """Returns usage accumulated at or after the stored reset baseline.

  Historical records are never modified, so range analytics and CSV exports
  keep the full history while the monitored totals reflect only the current
  usage cycle.
  """
  with get_connection() as conn:
    cursor = conn.cursor()
    cursor.execute(
        """
            SELECT SUM(bytes_sent), SUM(bytes_recv)
            FROM bandwidth_records
            WHERE timestamp >= ?
        """,
        (reset_timestamp,),
    )
    row = cursor.fetchone()
    sent = row[0] if (row and row[0]) else 0
    recv = row[1] if (row and row[1]) else 0
    return sent, recv


def export_range_to_csv(
    start_datetime_str: str, end_datetime_str: str, filepath: str
) -> int:
  s_clean = (
      start_datetime_str
      if " " in start_datetime_str
      else f"{start_datetime_str} 00:00:00"
  )
  e_clean = (
      end_datetime_str
      if " " in end_datetime_str
      else f"{end_datetime_str} 23:59:59"
  )

  with get_connection() as conn:
    cursor = conn.cursor()
    cursor.execute(
        """
            SELECT timestamp, bytes_sent, bytes_recv
            FROM bandwidth_records
            WHERE timestamp BETWEEN ? AND ?
            ORDER BY timestamp ASC
        """,
        (s_clean, e_clean),
    )
    rows = cursor.fetchall()

  with open(filepath, "w", newline="", encoding="utf-8") as f:
    writer = csv.writer(f)
    writer.writerow(
        ["Timestamp", "Bytes Sent", "Bytes Received", "Total (Bytes)"]
    )
    for r in rows:
      writer.writerow([r[0], r[1], r[2], r[1] + r[2]])

  return len(rows)
