"""
Campus Hardware Inventory System
================================

Laboratorysystem.py keeps ALL of the original application logic:

* the database layer (SQLite locally, or Supabase PostgreSQL through DATABASE_URL),
* the validation schemas (pydantic),
* the controllers (login, register, reset, inventory, borrowing, users, settings),
* the original Tkinter desktop interface.

Lab 8 changes (minimum-change approach):

1. Tkinter is OPTIONAL. Render has no desktop, so importing this file must not
   require Tkinter. The desktop classes are still here and still work on Windows.
2. The database layer reads DATABASE_URL. If it is set, the app uses PostgreSQL
   (Supabase). If it is not set, the app keeps using models/hardware_inventory.db.
3. The business logic that used to live inside the Tkinter button handlers was
   moved into plain functions ("controllers") so that BOTH the desktop window and
   the Flask web app (app.py) call the exact same code.
"""

from __future__ import annotations

import csv
import io
import logging
import math
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

# ---------------------------------------------------------------------------
# Lab 8 / A.1 - Make Tkinter optional
# ---------------------------------------------------------------------------
try:
    import tkinter as tk
    from tkinter import filedialog, font as tkfont, messagebox, ttk

    TKINTER_AVAILABLE = True
except ImportError:
    tk = None
    filedialog = None
    tkfont = None
    messagebox = None
    ttk = None
    TKINTER_AVAILABLE = False



class _TkinterMissing:
    """Placeholder base class used only when Tkinter is not installed."""


# Base classes for the desktop widgets. When Tkinter is missing (Render),
# the widget classes are still defined but inherit from a placeholder, so this
# module can be imported by Flask without crashing.
_TkFrame = tk.Frame if TKINTER_AVAILABLE else _TkinterMissing
_TkCanvas = tk.Canvas if TKINTER_AVAILABLE else _TkinterMissing
_TkToplevel = tk.Toplevel if TKINTER_AVAILABLE else _TkinterMissing
_TclError = tk.TclError if TKINTER_AVAILABLE else Exception

# PostgreSQL driver (only needed when DATABASE_URL is set).
try:
    import psycopg
    from psycopg.rows import dict_row

    PSYCOPG_AVAILABLE = True
except ImportError:
    psycopg = None
    dict_row = None
    PSYCOPG_AVAILABLE = False

import bcrypt
from pydantic import BaseModel, Field, ValidationError, field_validator

# ---------------------------------------------------------------------------
# Paths / logging / constants
# ---------------------------------------------------------------------------

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MODEL_DIR = os.path.join(BASE_DIR, "models")
LOG_DIR = os.path.join(BASE_DIR, "app_logging")

DB_PATH = os.path.join(MODEL_DIR, "hardware_inventory.db")
LOG_FILE = os.path.join(LOG_DIR, "app.log")

os.makedirs(MODEL_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)

# File log for the desktop + console log so messages also appear in Render's Logs tab.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"), logging.StreamHandler()],
)

LOW_STOCK_THRESHOLD = 5
DEFAULT_LOAN_DAYS = 7
MAX_FAILED_ATTEMPTS = 3
CATEGORIES = (
    "Microcontroller",
    "Sensor",
    "Cable",
    "Power Supply",
    "Tool",
    "Network Device",
    "Peripheral",
    "Component",
)

# Borrow log status values.
#   PENDING_BORROW  - a USER asked to borrow; waiting for an ADMIN
#   BORROWED        - approved / checked out; stock has been deducted
#   RETURN_PENDING  - the borrower asked to return; waiting for an ADMIN
#   RETURNED        - the return was approved; stock has been restored
#   REJECTED        - an ADMIN rejected the borrow request
LOAN_STATUSES = ("PENDING_BORROW", "BORROWED", "RETURN_PENDING", "RETURNED", "REJECTED")
OUT_STATUSES = ("BORROWED", "RETURN_PENDING")  # the items are physically out

# Render servers run on UTC. The campus is in the Philippines (UTC+8, no DST),
# so dates such as "due today" are computed in local time.
APP_UTC_OFFSET_HOURS = float(os.getenv("APP_UTC_OFFSET_HOURS", "8"))
APP_TZ = timezone(timedelta(hours=APP_UTC_OFFSET_HOURS))


def now_local() -> datetime:
    """Current campus time (naive datetime, same format the desktop app always used)."""
    return datetime.now(APP_TZ).replace(tzinfo=None)


def now_str() -> str:
    return now_local().strftime("%Y-%m-%d %H:%M:%S")


def today_str() -> str:
    return now_local().strftime("%Y-%m-%d")

# ---------------------------------------------------------------------------
# Typography - one family, resolved against what is actually installed
# ---------------------------------------------------------------------------

FONT_FAMILY = "Segoe UI"
FONT_STACK = (
    "Inter",                    # best modern UI face if it is installed
    "Segoe UI Variable Text",   # Windows 11
    "Segoe UI",                 # Windows 10
    "SF Pro Text",              # macOS
    "Roboto",
    "Helvetica Neue",
    "Noto Sans",
    "DejaVu Sans",
    "Arial",
)

# Type scale (points). One family; weight and size carry the hierarchy.
TYPE = {
    "display": 21,
    "metric": 25,
    "title": 15,
    "heading": 11,
    "body": 10,
    "label": 9,
    "micro": 8,
}


def F(role: str, bold: bool = False) -> tuple:
    """Font tuple for a role in the type scale."""
    return (FONT_FAMILY, TYPE[role], "bold") if bold else (FONT_FAMILY, TYPE[role])


def resolve_font_family(root: tk.Misc) -> None:
    global FONT_FAMILY
    try:
        available = set(tkfont.families(root))
    except _TclError:
        return
    for candidate in FONT_STACK:
        if candidate in available:
            FONT_FAMILY = candidate
            return


# ---------------------------------------------------------------------------
# HiDPI - crisp text on scaled Windows displays instead of blurry upscaling
# ---------------------------------------------------------------------------

UI_SCALE = 1.0


def s(value: float) -> int:
    """Scale a pixel dimension for the current display."""
    return max(1, int(round(value * UI_SCALE)))


def enable_hidpi(root: tk.Misc) -> None:
    global UI_SCALE
    if sys.platform != "win32":
        return
    try:
        from ctypes import windll

        try:
            windll.shcore.SetProcessDpiAwareness(1)  # system DPI aware
        except Exception:
            windll.user32.SetProcessDPIAware()
        dpi = windll.user32.GetDpiForSystem()
    except Exception:
        return

    UI_SCALE = max(1.0, min(dpi / 96.0, 2.0))
    try:
        root.tk.call("tk", "scaling", dpi / 72.0)
    except _TclError:
        pass


# ---------------------------------------------------------------------------
# Palette - light workspace, charcoal rail, red primary
# ---------------------------------------------------------------------------

THEMES = {
    "light": {
        "bg": "#F4F6FA",
        "sidebar": "#15161B",
        "sidebar_hover": "#23242B",
        "sidebar_active": "#F0483E",
        "sidebar_text": "#9BA1AD",
        "sidebar_text_active": "#FFFFFF",
        "sidebar_divider": "#2A2C34",
        "topbar": "#FFFFFF",
        "card": "#FFFFFF",
        "card_alt": "#F5F7FB",
        "table_head": "#F4F6FA",
        "row_alt": "#FAFBFD",
        "text": "#1C2029",
        "heading": "#101319",
        "muted": "#79818F",
        "border": "#E5E9F0",
        "border_focus": "#F0483E",
        "accent": "#F0483E",
        "accent_hover": "#D2382F",
        "accent_soft": "#FDEEEC",
        "blue": "#2C7BE5",
        "blue_soft": "#E9F1FD",
        "green": "#17A97A",
        "green_soft": "#E3F6EF",
        "yellow": "#E39A2B",
        "yellow_hover": "#C8821A",
        "yellow_soft": "#FDF2E1",
        "orange": "#F5803E",
        "orange_soft": "#FDEEE4",
        "red": "#E24A4F",
        "red_soft": "#FDECEC",
        "purple": "#7A5AF8",
        "purple_soft": "#EFEBFE",
        "white": "#FFFFFF",
        "shadow": "#E1E6EE",
        "track": "#EDF0F5",
    },
    "dark": {
        "bg": "#0C0E13",
        "sidebar": "#101216",
        "sidebar_hover": "#1D1F27",
        "sidebar_active": "#F0483E",
        "sidebar_text": "#8F97A5",
        "sidebar_text_active": "#FFFFFF",
        "sidebar_divider": "#22242D",
        "topbar": "#14171F",
        "card": "#14171F",
        "card_alt": "#1A1E28",
        "table_head": "#1A1E28",
        "row_alt": "#171B23",
        "text": "#E7EBF2",
        "heading": "#F7F9FC",
        "muted": "#8A93A3",
        "border": "#232833",
        "border_focus": "#F0483E",
        "accent": "#F0483E",
        "accent_hover": "#D2382F",
        "accent_soft": "#341A1B",
        "blue": "#5B9BF0",
        "blue_soft": "#16233A",
        "green": "#2BC08D",
        "green_soft": "#102D24",
        "yellow": "#E3A845",
        "yellow_hover": "#C88F2C",
        "yellow_soft": "#332711",
        "orange": "#F5803E",
        "orange_soft": "#331E10",
        "red": "#EF5B60",
        "red_soft": "#331617",
        "purple": "#9C86FA",
        "purple_soft": "#201B39",
        "white": "#FFFFFF",
        "shadow": "#07090D",
        "track": "#20242E",
    },
}

STATE = {"theme": "light"}


def C(key: str) -> str:
    return THEMES[STATE["theme"]][key]


USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
EMAIL_RE = re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@" r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+$"
)
SCHOOL_ID_RE = re.compile(r"^[A-Z0-9][A-Z0-9-]{3,19}$")

# ---------------------------------------------------------------------------
# Validation schemas
# ---------------------------------------------------------------------------


class UserSchema(BaseModel):
    username: str = Field(..., min_length=3, max_length=20)
    email: str
    password: str
    role: str = "USER"

    @field_validator("username")
    @classmethod
    def validate_username(cls, value: str) -> str:
        value = value.strip()
        if not USERNAME_RE.fullmatch(value):
            raise ValueError(
                "Username may contain only letters, numbers, underscore, dot, and hyphen."
            )
        return value

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str) -> str:
        value = value.strip().lower()
        if len(value) > 120:
            raise ValueError("Email address is too long.")
        if not EMAIL_RE.fullmatch(value):
            raise ValueError("Please enter a valid email address.")
        return value

    @field_validator("password")
    @classmethod
    def validate_password(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("Password must not begin or end with spaces.")
        if len(value) < 8:
            raise ValueError("Password must be at least 8 characters long.")
        if not re.search(r"[A-Z]", value):
            raise ValueError("Password must contain at least one uppercase letter.")
        if not re.search(r"[a-z]", value):
            raise ValueError("Password must contain at least one lowercase letter.")
        if not re.search(r"[0-9]", value):
            raise ValueError("Password must contain at least one number.")
        if not re.search(r"[@#$%^&*!?]", value):
            raise ValueError(
                "Password must contain at least one special character (@#$%^&*!?)."
            )
        return value

    @field_validator("role")
    @classmethod
    def validate_role(cls, value: str) -> str:
        value = value.strip().upper()
        if value not in ("USER", "ADMIN"):
            raise ValueError("Role must be USER or ADMIN.")
        return value


class BorrowSchema(BaseModel):
    """A borrow record is only valid with a verifiable student school ID."""

    borrower_name: str = Field(..., min_length=2, max_length=80)
    school_id: str
    quantity: int = Field(..., gt=0)
    loan_days: int = Field(default=DEFAULT_LOAN_DAYS, ge=1, le=180)
    notes: str = ""

    @field_validator("borrower_name")
    @classmethod
    def validate_borrower_name(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value:
            raise ValueError("Borrower name is required.")
        if not re.fullmatch(r"[A-Za-z.'\- ]+", value):
            raise ValueError(
                "Borrower name may contain only letters, spaces, hyphens, apostrophes and dots."
            )
        return value.title()

    @field_validator("school_id")
    @classmethod
    def validate_school_id(cls, value: str) -> str:
        value = value.strip().upper().replace(" ", "")
        if not value:
            raise ValueError("Student school ID is required.")
        if not SCHOOL_ID_RE.fullmatch(value):
            raise ValueError(
                "School ID must be 4-20 characters using letters, numbers or hyphens (e.g. 2023-04517)."
            )
        if sum(ch.isdigit() for ch in value) < 4:
            raise ValueError("School ID must contain at least 4 digits (e.g. 2023-04517).")
        return value

    @field_validator("notes")
    @classmethod
    def validate_notes(cls, value: str) -> str:
        value = value.strip()
        if len(value) > 250:
            raise ValueError("Notes must be 250 characters or fewer.")
        return value


class ItemSchema(BaseModel):
    item_name: str = Field(..., min_length=2, max_length=80)
    category: str = Field(..., min_length=2, max_length=40)
    stock: int = Field(..., ge=0)
    unit_price: float = Field(..., ge=0)

    @field_validator("item_name", "category")
    @classmethod
    def tidy(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value:
            raise ValueError("This field is required.")
        return value


def first_error(error: ValidationError) -> str:
    """Readable first message from a pydantic ValidationError."""
    message = error.errors()[0]["msg"]
    return message.removeprefix("Value error, ")

# ---------------------------------------------------------------------------
# Database layer - SQLite (desktop) or Supabase PostgreSQL (DATABASE_URL)
# ---------------------------------------------------------------------------

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
USE_POSTGRES = bool(DATABASE_URL)

# Exceptions that both database drivers can raise.
DB_ERRORS: tuple = (sqlite3.Error,) + ((psycopg.Error,) if PSYCOPG_AVAILABLE else ())
INTEGRITY_ERRORS: tuple = (sqlite3.IntegrityError,) + (
    (psycopg.IntegrityError,) if PSYCOPG_AVAILABLE else ()
)

_LIKE_RE = re.compile(r"\bLIKE\b")


def _to_postgres(query: str, has_params: bool) -> str:
    """Translate the SQLite-style SQL used in this file into PostgreSQL SQL.

    * `?` placeholders become `%s` (psycopg style)
    * LIKE becomes ILIKE, because SQLite's LIKE ignores letter case
    """
    if has_params:
        query = query.replace("%", "%%").replace("?", "%s")
    return _LIKE_RE.sub("ILIKE", query)


def database_label() -> str:
    """Safe description of the active database (never shows the password)."""
    if not USE_POSTGRES:
        return f"SQLite ({os.path.relpath(DB_PATH, BASE_DIR)})"
    host = re.sub(r"^.*@", "", DATABASE_URL).split("/")[0].split("?")[0]
    return f"PostgreSQL ({host})"


class Database:
    """One small connection wrapper so the rest of the code does not care which
    database is used. Rows can be read as row["column"] with both drivers."""

    def __init__(self):
        self.is_postgres = USE_POSTGRES
        if self.is_postgres:
            if not PSYCOPG_AVAILABLE:
                raise RuntimeError(
                    "DATABASE_URL is set but psycopg is not installed. "
                    "Run: python -m pip install -r requirements.txt"
                )
            if not DATABASE_URL.startswith(("postgresql://", "postgres://")):
                raise RuntimeError(
                    "DATABASE_URL must be the full Supabase URI that starts with postgresql:// "
                    "(it is empty, a placeholder, or malformed)."
                )
            self.raw = psycopg.connect(
                DATABASE_URL,
                row_factory=dict_row,
                prepare_threshold=None,  # required by the Supabase connection pooler
                connect_timeout=15,
            )
        else:
            self.raw = sqlite3.connect(DB_PATH, timeout=15)
            self.raw.row_factory = sqlite3.Row
            self.raw.execute("PRAGMA foreign_keys = ON")

    # SELECT ... FOR UPDATE locks rows in PostgreSQL. SQLite locks the whole
    # database with BEGIN IMMEDIATE instead, so the clause is empty there.
    @property
    def for_update(self) -> str:
        return " FOR UPDATE" if self.is_postgres else ""

    def execute(self, query: str, params=()):
        params = tuple(params)
        if self.is_postgres:
            cursor = self.raw.cursor()
            cursor.execute(_to_postgres(query, bool(params)), params or None)
            return cursor
        return self.raw.execute(query, params)

    def executemany(self, query: str, seq_of_params):
        if self.is_postgres:
            cursor = self.raw.cursor()
            cursor.executemany(_to_postgres(query, True), [tuple(p) for p in seq_of_params])
            return cursor
        return self.raw.executemany(query, seq_of_params)

    def cursor(self):
        return self

    def begin(self):
        """Start a write transaction (psycopg starts one automatically)."""
        if not self.is_postgres:
            self.raw.execute("BEGIN IMMEDIATE")

    def commit(self):
        self.raw.commit()

    def rollback(self):
        try:
            self.raw.rollback()
        except DB_ERRORS:
            pass

    def close(self):
        try:
            self.raw.close()
        except DB_ERRORS:
            pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is not None:
            self.rollback()
        self.close()
        return False


def connect_db() -> Database:
    return Database()


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def check_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except Exception:
        return False


def get_setting(key: str, default: str = "") -> str:
    with connect_db() as connection:
        row = connection.execute(
            "SELECT value FROM app_settings WHERE key = ?", (key,)
        ).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    with connect_db() as connection:
        connection.execute(
            "INSERT INTO app_settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        connection.commit()


def load_runtime_settings() -> None:
    global LOW_STOCK_THRESHOLD
    stored = get_setting("low_stock_threshold")
    if stored:
        try:
            LOW_STOCK_THRESHOLD = max(1, int(stored))
        except ValueError:
            pass
    stored_theme = get_setting("theme")
    if stored_theme in THEMES:
        STATE["theme"] = stored_theme


def write_system_log(username: str, action: str, details: str = "") -> None:
    try:
        with connect_db() as connection:
            connection.execute(
                "INSERT INTO system_logs (username, action, details, timestamp) VALUES (?, ?, ?, ?)",
                (username, action, details, now_str()),
            )
            connection.commit()
    except Exception:
        logging.exception("Could not write the system log entry.")
    logging.info("USER=%s | ACTION=%s | %s", username or "SYSTEM", action, details)


def stock_label(stock: int) -> str:
    if stock <= 0:
        return "Out of Stock"
    if stock <= LOW_STOCK_THRESHOLD:
        return "Low Stock"
    return "In Stock"


def _ensure_column(connection: Database, table: str, column: str, ddl: str) -> None:
    """Add a column to an older SQLite database that was created before it existed."""
    existing = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
        logging.info("Migration: added %s.%s", table, column)


# Original SQLite schema (desktop). Unchanged except for borrow_log.requested_by.
SQLITE_SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL COLLATE NOCASE,
        email TEXT UNIQUE NOT NULL COLLATE NOCASE,
        password_hash TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'USER',
        failed_attempts INTEGER NOT NULL DEFAULT 0,
        is_locked INTEGER NOT NULL DEFAULT 0,
        is_active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS inventory (
        item_id INTEGER PRIMARY KEY AUTOINCREMENT,
        item_name TEXT UNIQUE NOT NULL COLLATE NOCASE,
        category TEXT NOT NULL,
        stock INTEGER NOT NULL DEFAULT 0,
        unit_price REAL NOT NULL DEFAULT 0,
        in_stock TEXT NOT NULL DEFAULT 'Out of Stock'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS borrow_log (
        log_id INTEGER PRIMARY KEY AUTOINCREMENT,
        item_id INTEGER NOT NULL,
        item_name TEXT NOT NULL,
        borrower_name TEXT NOT NULL,
        school_id TEXT NOT NULL DEFAULT '',
        quantity INTEGER NOT NULL,
        processed_by TEXT NOT NULL,
        borrow_date TEXT NOT NULL,
        due_date TEXT,
        return_date TEXT,
        status TEXT NOT NULL DEFAULT 'BORROWED',
        notes TEXT,
        requested_by TEXT NOT NULL DEFAULT '',
        FOREIGN KEY (item_id) REFERENCES inventory(item_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS password_reset_requests (
        request_id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        username TEXT NOT NULL,
        email TEXT NOT NULL,
        new_password_hash TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'PENDING',
        requested_at TEXT NOT NULL,
        reviewed_at TEXT,
        reviewed_by TEXT,
        FOREIGN KEY (user_id) REFERENCES users(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS system_logs (
        log_id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT,
        action TEXT NOT NULL,
        details TEXT,
        timestamp TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS app_settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
]

# Same tables for Supabase PostgreSQL. migrate_sqlite_to_postgresql.py creates
# exactly these tables, so the migration and the app always agree.
POSTGRES_SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS users (
        id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
        username TEXT UNIQUE NOT NULL,
        email TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'USER' CHECK (role IN ('ADMIN', 'USER')),
        failed_attempts INTEGER NOT NULL DEFAULT 0,
        is_locked INTEGER NOT NULL DEFAULT 0,
        is_active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL DEFAULT to_char(now(), 'YYYY-MM-DD HH24:MI:SS')
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS inventory (
        item_id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
        item_name TEXT UNIQUE NOT NULL,
        category TEXT NOT NULL,
        stock INTEGER NOT NULL DEFAULT 0,
        unit_price DOUBLE PRECISION NOT NULL DEFAULT 0,
        in_stock TEXT NOT NULL DEFAULT 'Out of Stock'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS borrow_log (
        log_id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
        item_id BIGINT NOT NULL REFERENCES inventory(item_id),
        item_name TEXT NOT NULL,
        borrower_name TEXT NOT NULL,
        school_id TEXT NOT NULL DEFAULT '',
        quantity INTEGER NOT NULL DEFAULT 1,
        processed_by TEXT NOT NULL DEFAULT '',
        borrow_date TEXT NOT NULL,
        due_date TEXT,
        return_date TEXT,
        status TEXT NOT NULL DEFAULT 'BORROWED' CHECK (status IN
            ('PENDING_BORROW', 'BORROWED', 'RETURN_PENDING', 'RETURNED', 'REJECTED')),
        notes TEXT,
        requested_by TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS password_reset_requests (
        request_id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
        user_id BIGINT NOT NULL REFERENCES users(id),
        username TEXT NOT NULL,
        email TEXT NOT NULL,
        new_password_hash TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING', 'APPROVED', 'REJECTED')),
        requested_at TEXT NOT NULL,
        reviewed_at TEXT,
        reviewed_by TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS system_logs (
        log_id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
        username TEXT,
        action TEXT NOT NULL,
        details TEXT,
        timestamp TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS app_settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
]

# Case-insensitive uniqueness (SQLite used COLLATE NOCASE for this) + search indexes.
POSTGRES_INDEXES = [
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_users_username_lower ON users (LOWER(username))",
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_users_email_lower ON users (LOWER(email))",
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_inventory_name_lower ON inventory (LOWER(item_name))",
    "CREATE INDEX IF NOT EXISTS idx_borrow_school_id ON borrow_log (school_id)",
    "CREATE INDEX IF NOT EXISTS idx_borrow_status ON borrow_log (status)",
]


def initialize_database() -> None:
    with connect_db() as connection:
        if connection.is_postgres:
            for statement in POSTGRES_SCHEMA + POSTGRES_INDEXES:
                connection.execute(statement)
            connection.execute(
                "ALTER TABLE borrow_log ADD COLUMN IF NOT EXISTS requested_by TEXT NOT NULL DEFAULT ''"
            )
        else:
            for statement in SQLITE_SCHEMA:
                connection.execute(statement)
            # Migrations for databases created by older versions of the desktop app.
            _ensure_column(connection, "borrow_log", "school_id", "TEXT NOT NULL DEFAULT ''")
            _ensure_column(connection, "borrow_log", "requested_by", "TEXT NOT NULL DEFAULT ''")
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_borrow_school_id ON borrow_log(school_id)"
            )
            connection.execute("CREATE INDEX IF NOT EXISTS idx_borrow_status ON borrow_log(status)")

        user_count = connection.execute("SELECT COUNT(*) AS total FROM users").fetchone()["total"]
        if user_count == 0:
            connection.execute(
                """
                INSERT INTO users
                    (username, email, password_hash, role, failed_attempts, is_locked, is_active)
                VALUES (?, ?, ?, ?, 0, 0, 1)
                """,
                ("admin", "admin@campus.edu", hash_password("Admin@123"), "ADMIN"),
            )
            logging.info("Default ADMIN account created.")

        item_count = connection.execute(
            "SELECT COUNT(*) AS total FROM inventory"
        ).fetchone()["total"]
        if item_count == 0:
            seed = [
                ("Arduino Uno R3", "Microcontroller", 24, 1250.00),
                ("Raspberry Pi 4 (4GB)", "Microcontroller", 8, 4800.00),
                ("HC-SR04 Ultrasonic Sensor", "Sensor", 40, 145.00),
                ("DHT22 Temp/Humidity Sensor", "Sensor", 3, 320.00),
                ("Breadboard 830pt", "Component", 32, 180.00),
                ("Jumper Wire Set (120pc)", "Cable", 18, 220.00),
                ("Digital Multimeter", "Tool", 6, 1650.00),
                ("Soldering Iron 60W", "Tool", 4, 890.00),
                ("Cat6 Patch Cable 3m", "Cable", 55, 210.00),
                ("TP-Link 8-Port Switch", "Network Device", 2, 1350.00),
                ("12V 2A Power Adapter", "Power Supply", 14, 340.00),
                ("USB Logic Analyzer", "Tool", 0, 2100.00),
            ]
            connection.executemany(
                "INSERT INTO inventory (item_name, category, stock, unit_price, in_stock) "
                "VALUES (?, ?, ?, ?, ?)",
                [(n, c, st, p, stock_label(st)) for n, c, st, p in seed],
            )
            logging.info("Seeded starter inventory.")

        connection.commit()
    logging.info("Database ready: %s", database_label())


def refresh_stock_labels() -> None:
    with connect_db() as connection:
        connection.execute(
            "UPDATE inventory SET in_stock = CASE "
            "WHEN stock <= 0 THEN 'Out of Stock' "
            "WHEN stock <= ? THEN 'Low Stock' ELSE 'In Stock' END",
            (LOW_STOCK_THRESHOLD,),
        )
        connection.commit()

# ---------------------------------------------------------------------------
# Controllers - shared by the Tkinter desktop app and the Flask web app
# Every controller returns plain data or (ok, message) so any interface can use it.
# ---------------------------------------------------------------------------

def _user_dict(row) -> dict:
    return {"id": row["id"], "username": row["username"], "role": row["role"], "email": row["email"]}


# -- authentication ---------------------------------------------------------

def authenticate_user(username: str, password: str) -> tuple[bool, str, dict | None]:
    """Sign in with the 3-attempt lockout rule. Returns (ok, message, user)."""
    username = (username or "").strip()
    if not username or not password:
        return False, "Enter both your username and password.", None

    with connect_db() as connection:
        user = connection.execute(
            "SELECT * FROM users WHERE LOWER(username) = LOWER(?)", (username,)
        ).fetchone()

        if not user:
            return False, "That username and password do not match an account.", None
        if user["is_locked"]:
            return (
                False,
                "This account is locked after repeated failed attempts. "
                "Submit a reset request to unlock it.",
                None,
            )
        if not user["is_active"]:
            return False, "An administrator has disabled this account.", None

        if check_password(password, user["password_hash"]):
            connection.execute("UPDATE users SET failed_attempts = 0 WHERE id = ?", (user["id"],))
            connection.commit()
            result = _user_dict(user)
        else:
            attempts = user["failed_attempts"] + 1
            is_locked = 1 if attempts >= MAX_FAILED_ATTEMPTS else 0
            connection.execute(
                "UPDATE users SET failed_attempts = ?, is_locked = ? WHERE id = ?",
                (attempts, is_locked, user["id"]),
            )
            connection.commit()
            result = None

    if result:
        write_system_log(result["username"], "LOGIN", "Successful sign in")
        return True, f"Welcome back, {result['username']}.", result

    if is_locked:
        write_system_log(username, "ACCOUNT_LOCKED", "Exceeded maximum failed attempts")
        return False, "Three failed attempts reached. This account is now locked.", None
    left = MAX_FAILED_ATTEMPTS - attempts
    return False, f"That password is incorrect. {left} attempt(s) left before the account locks.", None


def get_user(user_id: int) -> dict | None:
    with connect_db() as connection:
        row = connection.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if not row or not row["is_active"] or row["is_locked"]:
        return None
    return _user_dict(row)


def register_user(username: str, email: str, password: str, role: str = "USER") -> tuple[bool, str]:
    try:
        validated = UserSchema(
            username=(username or "").strip(),
            email=(email or "").strip(),
            password=password or "",
            role=(role or "USER").strip().upper(),
        )
    except ValidationError as error:
        return False, first_error(error)

    with connect_db() as connection:
        existing = connection.execute(
            "SELECT username, email FROM users "
            "WHERE LOWER(username) = LOWER(?) OR LOWER(email) = LOWER(?)",
            (validated.username, validated.email),
        ).fetchone()
        if existing:
            if existing["username"].lower() == validated.username.lower():
                return False, "That username is already taken."
            return False, "That email address is already registered."

        try:
            connection.execute(
                "INSERT INTO users (username, email, password_hash, role) VALUES (?, ?, ?, ?)",
                (validated.username, validated.email, hash_password(validated.password), validated.role),
            )
            connection.commit()
        except INTEGRITY_ERRORS:
            connection.rollback()
            return False, "That username or email address is already registered."

    write_system_log(validated.username, "REGISTER", f"Account created with role {validated.role}")
    return True, "Account created. Sign in to continue."


def submit_password_reset(username: str, email: str, password: str, confirm: str) -> tuple[bool, str]:
    if password != confirm:
        return False, "The two passwords do not match."
    try:
        validated = UserSchema(
            username=(username or "").strip(),
            email=(email or "").strip(),
            password=password or "",
            role="USER",
        )
    except ValidationError as error:
        return False, first_error(error)

    with connect_db() as connection:
        user = connection.execute(
            "SELECT id, username, email FROM users "
            "WHERE LOWER(username) = LOWER(?) AND LOWER(email) = LOWER(?)",
            (validated.username, validated.email),
        ).fetchone()
        if not user:
            return False, "No account matches that username and email combination."

        connection.execute(
            """
            INSERT INTO password_reset_requests
                (user_id, username, email, new_password_hash, status, requested_at)
            VALUES (?, ?, ?, ?, 'PENDING', ?)
            """,
            (user["id"], user["username"], user["email"], hash_password(validated.password), now_str()),
        )
        connection.commit()

    write_system_log(user["username"], "RESET_REQUEST", "Submitted password reset request")
    return True, "Request sent to the administrators."


# -- users (admin) ----------------------------------------------------------

def list_users() -> list[dict]:
    with connect_db() as connection:
        rows = connection.execute("SELECT * FROM users ORDER BY id").fetchall()
    return [dict(row) for row in rows]


def list_reset_requests() -> list[dict]:
    with connect_db() as connection:
        rows = connection.execute(
            "SELECT * FROM password_reset_requests ORDER BY request_id DESC"
        ).fetchall()
    return [dict(row) for row in rows]


def unlock_user(user_id: int, admin: str) -> tuple[bool, str]:
    with connect_db() as connection:
        user = connection.execute("SELECT username FROM users WHERE id = ?", (user_id,)).fetchone()
        if not user:
            return False, "That account no longer exists."
        connection.execute(
            "UPDATE users SET is_locked = 0, failed_attempts = 0 WHERE id = ?", (user_id,)
        )
        connection.commit()
    write_system_log(admin, "UNLOCK", f"Unlocked account {user['username']}")
    return True, f"{user['username']} unlocked."


def toggle_user_active(user_id: int, admin: str, current_user_id: int) -> tuple[bool, str]:
    if int(user_id) == int(current_user_id):
        return False, "You cannot deactivate the account you are signed in with."
    with connect_db() as connection:
        user = connection.execute(
            "SELECT username, is_active FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if not user:
            return False, "That account no longer exists."
        new_state = 0 if user["is_active"] else 1
        connection.execute("UPDATE users SET is_active = ? WHERE id = ?", (new_state, user_id))
        connection.commit()
    write_system_log(admin, "USER_TOGGLE", f"Changed active state for {user['username']}")
    return True, f"{user['username']} is now {'active' if new_state else 'disabled'}."


def set_user_role(user_id: int, role: str, admin: str, current_user_id: int) -> tuple[bool, str]:
    role = (role or "").strip().upper()
    if role not in ("USER", "ADMIN"):
        return False, "Role must be USER or ADMIN."
    if int(user_id) == int(current_user_id):
        return False, "You cannot change the role of the account you are signed in with."
    with connect_db() as connection:
        user = connection.execute("SELECT username FROM users WHERE id = ?", (user_id,)).fetchone()
        if not user:
            return False, "That account no longer exists."
        connection.execute("UPDATE users SET role = ? WHERE id = ?", (role, user_id))
        connection.commit()
    write_system_log(admin, "ROLE_CHANGE", f"{user['username']} set to {role}")
    return True, f"{user['username']} is now {role.title()}."


def review_reset_request(request_id: int, approve: bool, admin: str) -> tuple[bool, str]:
    with connect_db() as connection:
        request = connection.execute(
            "SELECT * FROM password_reset_requests WHERE request_id = ?", (request_id,)
        ).fetchone()
        if request is None or request["status"] != "PENDING":
            return False, "That request has already been reviewed."

        if approve:
            connection.execute(
                "UPDATE users SET password_hash = ?, is_locked = 0, failed_attempts = 0 WHERE id = ?",
                (request["new_password_hash"], request["user_id"]),
            )
        connection.execute(
            "UPDATE password_reset_requests SET status = ?, reviewed_at = ?, reviewed_by = ? "
            "WHERE request_id = ?",
            ("APPROVED" if approve else "REJECTED", now_str(), admin, request_id),
        )
        connection.commit()

    write_system_log(
        admin, "RESET_REVIEW",
        f"{'Approved' if approve else 'Rejected'} reset for {request['username']}",
    )
    return True, f"Request {'approved' if approve else 'rejected'} for {request['username']}."


# -- inventory --------------------------------------------------------------

def list_inventory(term: str = "") -> list[dict]:
    pattern = f"%{(term or '').strip()}%"
    with connect_db() as connection:
        rows = connection.execute(
            "SELECT * FROM inventory WHERE item_name LIKE ? OR category LIKE ? ORDER BY item_name",
            (pattern, pattern),
        ).fetchall()
    return [dict(row) for row in rows]


def list_borrowable_items() -> list[dict]:
    with connect_db() as connection:
        rows = connection.execute(
            "SELECT item_id, item_name, category, stock FROM inventory WHERE stock > 0 ORDER BY item_name"
        ).fetchall()
    return [dict(row) for row in rows]


def get_item(item_id: int) -> dict | None:
    with connect_db() as connection:
        row = connection.execute("SELECT * FROM inventory WHERE item_id = ?", (item_id,)).fetchone()
    return dict(row) if row else None


def find_item_by_name(name: str, exclude_id: int | None = None) -> dict | None:
    query = "SELECT * FROM inventory WHERE LOWER(item_name) = LOWER(?)"
    params: list = [name]
    if exclude_id is not None:
        query += " AND item_id != ?"
        params.append(exclude_id)
    with connect_db() as connection:
        row = connection.execute(query, params).fetchone()
    return dict(row) if row else None


def create_item(data: ItemSchema, processed_by: str) -> tuple[bool, str]:
    """Insert a new item. Duplicate names must be handled with restock_item()."""
    existing = find_item_by_name(data.item_name)
    if existing:
        return False, (
            f'"{existing["item_name"]}" already exists (ID {existing["item_id"]}) '
            f"with {existing['stock']} unit(s)."
        )
    try:
        with connect_db() as connection:
            connection.execute(
                "INSERT INTO inventory (item_name, category, stock, unit_price, in_stock) "
                "VALUES (?, ?, ?, ?, ?)",
                (data.item_name, data.category, data.stock, data.unit_price, stock_label(data.stock)),
            )
            connection.commit()
    except INTEGRITY_ERRORS as error:
        logging.exception("Inventory save failed: %s", error)
        return False, "The database rejected the record. Refresh the inventory and try again."
    except DB_ERRORS as error:
        logging.exception("Inventory save failed: %s", error)
        return False, f"Could not save the item: {error}"

    detail = f"Added {data.item_name}"
    write_system_log(processed_by, "ITEM_CREATE", detail)
    return True, detail


def restock_item(item_id: int, add_stock: int, processed_by: str) -> tuple[bool, str]:
    if add_stock <= 0:
        return False, "Enter a quantity greater than zero to add."
    with connect_db() as connection:
        connection.begin()
        item = connection.execute(
            "SELECT item_id, item_name, stock FROM inventory WHERE item_id = ?" + connection.for_update,
            (item_id,),
        ).fetchone()
        if item is None:
            connection.rollback()
            return False, "That item no longer exists in the inventory."
        new_stock = item["stock"] + add_stock
        connection.execute(
            "UPDATE inventory SET stock = ?, in_stock = ? WHERE item_id = ?",
            (new_stock, stock_label(new_stock), item_id),
        )
        connection.commit()
    detail = f"Restocked {item['item_name']} by {add_stock} unit(s); new stock {new_stock}"
    write_system_log(processed_by, "ITEM_RESTOCK", detail)
    return True, detail


def update_item(item_id: int, data: ItemSchema, processed_by: str) -> tuple[bool, str]:
    # The item's own name is allowed, but another record cannot be renamed to it.
    if find_item_by_name(data.item_name, exclude_id=item_id):
        return False, (
            f'Another inventory record already uses "{data.item_name}". '
            "Choose a different item name."
        )
    try:
        with connect_db() as connection:
            connection.execute(
                "UPDATE inventory SET item_name = ?, category = ?, stock = ?, unit_price = ?, "
                "in_stock = ? WHERE item_id = ?",
                (data.item_name, data.category, data.stock, data.unit_price,
                 stock_label(data.stock), item_id),
            )
            connection.commit()
    except DB_ERRORS as error:
        logging.exception("Inventory save failed: %s", error)
        return False, f"Could not save the item: {error}"
    detail = f"Updated {data.item_name}"
    write_system_log(processed_by, "ITEM_UPDATE", detail)
    return True, detail


def delete_item(item_id: int, processed_by: str) -> tuple[bool, str]:
    with connect_db() as connection:
        item = connection.execute(
            "SELECT item_id, item_name FROM inventory WHERE item_id = ?", (item_id,)
        ).fetchone()
        if not item:
            return False, "That item no longer exists."
        open_loans = connection.execute(
            "SELECT COUNT(*) AS n FROM borrow_log WHERE item_id = ? "
            "AND status IN ('PENDING_BORROW', 'BORROWED', 'RETURN_PENDING')",
            (item_id,),
        ).fetchone()["n"]
        if open_loans:
            return False, (
                f"{item['item_name']} still has {open_loans} open loan/request(s). "
                "Finish or reject them first."
            )
        history = connection.execute(
            "SELECT COUNT(*) AS n FROM borrow_log WHERE item_id = ?", (item_id,)
        ).fetchone()["n"]
        if history:
            return False, (
                f"{item['item_name']} has {history} borrow record(s) in its history, so it "
                "cannot be deleted. Set its stock to 0 instead."
            )
        connection.execute("DELETE FROM inventory WHERE item_id = ?", (item_id,))
        connection.commit()
    write_system_log(processed_by, "ITEM_DELETE", f"Deleted {item['item_name']}")
    return True, f"{item['item_name']} deleted."


def inventory_csv() -> str:
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Item ID", "Item name", "Category", "Stock", "Unit price", "Status"])
    for item in list_inventory():
        writer.writerow([
            item["item_id"], item["item_name"], item["category"], item["stock"],
            f"{item['unit_price']:.2f}", item["in_stock"],
        ])
    return output.getvalue()


# -- borrowing --------------------------------------------------------------

def loan_status(entry, today: str | None = None) -> tuple[str, str | None]:
    """Display label and colour tag for one borrow_log row."""
    today = today or today_str()
    status = entry["status"]
    if status == "PENDING_BORROW":
        return "Pending approval", "warn"
    if status == "REJECTED":
        return "Rejected", "muted"
    if status == "RETURNED":
        return "Returned", "ok"
    overdue = (entry["due_date"] or "9999") < today
    if status == "RETURN_PENDING":
        return ("Return pending (overdue)" if overdue else "Return pending"), "warn"
    return ("Overdue", "danger") if overdue else ("Borrowed", None)


def _validate_quantity(connection: Database, item_id: int, quantity: int, lock: bool):
    item = connection.execute(
        "SELECT item_id, item_name, stock FROM inventory WHERE item_id = ?"
        + (connection.for_update if lock else ""),
        (item_id,),
    ).fetchone()
    if item is None:
        return None, "That item no longer exists in the inventory."
    if quantity > item["stock"]:
        return None, f"Only {item['stock']} unit(s) of {item['item_name']} are on hand."
    return item, ""


def record_borrow(item_id: int, data: BorrowSchema, processed_by: str) -> tuple[bool, str]:
    """Direct checkout by staff: insert the loan and decrement stock in one transaction."""
    connection = connect_db()
    try:
        connection.begin()
        item, problem = _validate_quantity(connection, item_id, data.quantity, lock=True)
        if item is None:
            connection.rollback()
            return False, problem

        now = now_local()
        connection.execute(
            """
            INSERT INTO borrow_log
                (item_id, item_name, borrower_name, school_id, quantity, processed_by,
                 borrow_date, due_date, status, notes, requested_by)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'BORROWED', ?, ?)
            """,
            (
                item["item_id"], item["item_name"], data.borrower_name, data.school_id,
                data.quantity, processed_by, now.strftime("%Y-%m-%d %H:%M:%S"),
                (now + timedelta(days=data.loan_days)).strftime("%Y-%m-%d"),
                data.notes, processed_by,
            ),
        )
        remaining = item["stock"] - data.quantity
        connection.execute(
            "UPDATE inventory SET stock = ?, in_stock = ? WHERE item_id = ?",
            (remaining, stock_label(remaining), item["item_id"]),
        )
        connection.commit()
    except DB_ERRORS as error:
        connection.rollback()
        return False, f"Could not save the borrow record: {error}"
    finally:
        connection.close()

    write_system_log(
        processed_by, "BORROW",
        f"{data.quantity} x {item['item_name']} to {data.borrower_name} ({data.school_id})",
    )
    return True, f"{data.quantity} x {item['item_name']} checked out to {data.school_id}."


def request_borrow(item_id: int, data: BorrowSchema, requested_by: str) -> tuple[bool, str]:
    """A USER asks to borrow. Stock is NOT deducted until an ADMIN approves."""
    with connect_db() as connection:
        item, problem = _validate_quantity(connection, item_id, data.quantity, lock=False)
        if item is None:
            return False, problem
        now = now_local()
        connection.execute(
            """
            INSERT INTO borrow_log
                (item_id, item_name, borrower_name, school_id, quantity, processed_by,
                 borrow_date, due_date, status, notes, requested_by)
            VALUES (?, ?, ?, ?, ?, '', ?, ?, 'PENDING_BORROW', ?, ?)
            """,
            (
                item["item_id"], item["item_name"], data.borrower_name, data.school_id,
                data.quantity, now.strftime("%Y-%m-%d %H:%M:%S"),
                (now + timedelta(days=data.loan_days)).strftime("%Y-%m-%d"),
                data.notes, requested_by,
            ),
        )
        connection.commit()
    write_system_log(
        requested_by, "BORROW_REQUEST",
        f"{data.quantity} x {item['item_name']} for {data.borrower_name} ({data.school_id})",
    )
    return True, f"Borrow request for {data.quantity} x {item['item_name']} sent for approval."


def _get_loan(connection: Database, log_id: int):
    return connection.execute(
        "SELECT * FROM borrow_log WHERE log_id = ?" + connection.for_update, (log_id,)
    ).fetchone()


def approve_borrow(log_id: int, admin: str) -> tuple[bool, str]:
    connection = connect_db()
    try:
        connection.begin()
        entry = _get_loan(connection, log_id)
        if entry is None or entry["status"] != "PENDING_BORROW":
            connection.rollback()
            return False, "That borrow request is no longer pending."
        item, problem = _validate_quantity(connection, entry["item_id"], entry["quantity"], lock=True)
        if item is None:
            connection.rollback()
            return False, problem

        # Keep the loan length the student asked for, counted from approval time.
        try:
            requested_days = (
                datetime.strptime(entry["due_date"], "%Y-%m-%d").date()
                - datetime.strptime(entry["borrow_date"][:10], "%Y-%m-%d").date()
            ).days
        except (TypeError, ValueError):
            requested_days = DEFAULT_LOAN_DAYS
        requested_days = max(1, requested_days)

        now = now_local()
        connection.execute(
            "UPDATE borrow_log SET status = 'BORROWED', processed_by = ?, borrow_date = ?, "
            "due_date = ? WHERE log_id = ?",
            (admin, now.strftime("%Y-%m-%d %H:%M:%S"),
             (now + timedelta(days=requested_days)).strftime("%Y-%m-%d"), log_id),
        )
        remaining = item["stock"] - entry["quantity"]
        connection.execute(
            "UPDATE inventory SET stock = ?, in_stock = ? WHERE item_id = ?",
            (remaining, stock_label(remaining), item["item_id"]),
        )
        connection.commit()
    except DB_ERRORS as error:
        connection.rollback()
        return False, f"Could not approve the request: {error}"
    finally:
        connection.close()

    write_system_log(
        admin, "BORROW_APPROVE",
        f"{entry['quantity']} x {entry['item_name']} to {entry['borrower_name']} ({entry['school_id']})",
    )
    return True, f"Approved: {entry['quantity']} x {entry['item_name']} to {entry['school_id']}."


def reject_borrow(log_id: int, admin: str) -> tuple[bool, str]:
    with connect_db() as connection:
        connection.begin()
        entry = _get_loan(connection, log_id)
        if entry is None or entry["status"] != "PENDING_BORROW":
            connection.rollback()
            return False, "That borrow request is no longer pending."
        connection.execute(
            "UPDATE borrow_log SET status = 'REJECTED', processed_by = ? WHERE log_id = ?",
            (admin, log_id),
        )
        connection.commit()
    write_system_log(
        admin, "BORROW_REJECT", f"{entry['quantity']} x {entry['item_name']} for {entry['school_id']}"
    )
    return True, f"Borrow request for {entry['item_name']} rejected."


def request_return(log_id: int, username: str, is_admin: bool = False) -> tuple[bool, str]:
    with connect_db() as connection:
        connection.begin()
        entry = _get_loan(connection, log_id)
        if entry is None or entry["status"] != "BORROWED":
            connection.rollback()
            return False, "Only items that are currently borrowed can be returned."
        if not is_admin and (entry["requested_by"] or "").lower() != username.lower():
            connection.rollback()
            return False, "You can only return items that you requested."
        connection.execute(
            "UPDATE borrow_log SET status = 'RETURN_PENDING' WHERE log_id = ?", (log_id,)
        )
        connection.commit()
    write_system_log(
        username, "RETURN_REQUEST", f"{entry['quantity']} x {entry['item_name']} ({entry['school_id']})"
    )
    return True, f"Return request for {entry['item_name']} sent for approval."


def record_return(log_id: int, processed_by: str) -> tuple[bool, str]:
    """Approve a return (or mark a borrowed item returned) and restore the stock."""
    connection = connect_db()
    try:
        connection.begin()
        entry = _get_loan(connection, log_id)
        if entry is None:
            connection.rollback()
            return False, "That borrow record no longer exists."
        if entry["status"] == "RETURNED":
            connection.rollback()
            return False, "This item was already returned."
        if entry["status"] not in OUT_STATUSES:
            connection.rollback()
            return False, "This record is not an approved loan, so there is nothing to return."

        connection.execute(
            "UPDATE borrow_log SET status = 'RETURNED', return_date = ? WHERE log_id = ?",
            (now_str(), log_id),
        )
        item = connection.execute(
            "SELECT stock FROM inventory WHERE item_id = ?" + connection.for_update,
            (entry["item_id"],),
        ).fetchone()
        if item is not None:
            restored = item["stock"] + entry["quantity"]
            connection.execute(
                "UPDATE inventory SET stock = ?, in_stock = ? WHERE item_id = ?",
                (restored, stock_label(restored), entry["item_id"]),
            )
        connection.commit()
    except DB_ERRORS as error:
        connection.rollback()
        return False, f"Could not save the return: {error}"
    finally:
        connection.close()

    write_system_log(
        processed_by, "RETURN",
        f"{entry['quantity']} x {entry['item_name']} from {entry['borrower_name']} ({entry['school_id']})",
    )
    return True, f"{entry['item_name']} returned to stock."


def reject_return(log_id: int, admin: str) -> tuple[bool, str]:
    with connect_db() as connection:
        connection.begin()
        entry = _get_loan(connection, log_id)
        if entry is None or entry["status"] != "RETURN_PENDING":
            connection.rollback()
            return False, "That return request is no longer pending."
        connection.execute("UPDATE borrow_log SET status = 'BORROWED' WHERE log_id = ?", (log_id,))
        connection.commit()
    write_system_log(
        admin, "RETURN_REJECT", f"{entry['quantity']} x {entry['item_name']} ({entry['school_id']})"
    )
    return True, f"Return request for {entry['item_name']} rejected; it is still on loan."


LOG_FILTERS = ("All", "Pending", "Borrowed", "Overdue", "Returned", "Rejected")


def list_borrow_log(term: str = "", status: str = "All", requested_by: str | None = None) -> list[dict]:
    pattern = f"%{(term or '').strip()}%"
    today = today_str()
    query = (
        "SELECT * FROM borrow_log "
        "WHERE (item_name LIKE ? OR borrower_name LIKE ? OR school_id LIKE ?)"
    )
    params: list = [pattern, pattern, pattern]
    if status == "Pending":
        query += " AND status IN ('PENDING_BORROW', 'RETURN_PENDING')"
    elif status == "Borrowed":
        query += " AND status IN ('BORROWED', 'RETURN_PENDING')"
    elif status == "Returned":
        query += " AND status = 'RETURNED'"
    elif status == "Rejected":
        query += " AND status = 'REJECTED'"
    elif status == "Overdue":
        query += " AND status IN ('BORROWED', 'RETURN_PENDING') AND due_date < ?"
        params.append(today)
    if requested_by is not None:
        query += " AND LOWER(requested_by) = LOWER(?)"
        params.append(requested_by)
    query += " ORDER BY log_id DESC"

    with connect_db() as connection:
        rows = connection.execute(query, params).fetchall()
    result = []
    for row in rows:
        entry = dict(row)
        entry["status_label"], entry["status_tag"] = loan_status(entry, today)
        result.append(entry)
    return result


def list_pending_requests() -> dict:
    with connect_db() as connection:
        borrows = connection.execute(
            "SELECT b.*, i.stock AS stock_now FROM borrow_log b "
            "LEFT JOIN inventory i ON i.item_id = b.item_id "
            "WHERE b.status = 'PENDING_BORROW' ORDER BY b.log_id"
        ).fetchall()
        returns = connection.execute(
            "SELECT * FROM borrow_log WHERE status = 'RETURN_PENDING' ORDER BY log_id"
        ).fetchall()
    return {"borrows": [dict(r) for r in borrows], "returns": [dict(r) for r in returns]}


def borrow_log_csv() -> str:
    with connect_db() as connection:
        entries = connection.execute("SELECT * FROM borrow_log ORDER BY log_id").fetchall()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Log ID", "Item", "Student", "School ID", "Quantity", "Borrowed", "Due",
                     "Returned", "Status", "Requested by", "Processed by", "Notes"])
    for entry in entries:
        writer.writerow([
            entry["log_id"], entry["item_name"], entry["borrower_name"], entry["school_id"],
            entry["quantity"], entry["borrow_date"], entry["due_date"] or "",
            entry["return_date"] or "", entry["status"], entry["requested_by"] or "",
            entry["processed_by"] or "", entry["notes"] or "",
        ])
    return output.getvalue()


def student_history(school_id: str) -> list:
    with connect_db() as connection:
        rows = connection.execute(
            "SELECT * FROM borrow_log WHERE UPPER(school_id) = ? ORDER BY log_id DESC",
            (school_id.strip().upper(),),
        ).fetchall()
    return [dict(row) for row in rows]


def list_students(term: str = "") -> list[dict]:
    pattern = f"%{(term or '').strip()}%"
    with connect_db() as connection:
        rows = connection.execute(
            """
            SELECT school_id,
                   MAX(borrower_name) AS borrower_name,
                   COUNT(*) AS total,
                   SUM(CASE WHEN status IN ('BORROWED', 'RETURN_PENDING') THEN 1 ELSE 0 END) AS still_out,
                   SUM(CASE WHEN status IN ('BORROWED', 'RETURN_PENDING') AND due_date < ?
                            THEN 1 ELSE 0 END) AS overdue,
                   MAX(borrow_date) AS last_borrow
            FROM borrow_log
            WHERE school_id != '' AND status != 'REJECTED'
              AND (school_id LIKE ? OR borrower_name LIKE ?)
            GROUP BY school_id
            ORDER BY still_out DESC, last_borrow DESC
            """,
            (today_str(), pattern, pattern),
        ).fetchall()
    return [dict(row) for row in rows]


# -- dashboard / activity / settings ----------------------------------------

def dashboard_metrics() -> dict:
    today = today_str()
    out = "('BORROWED', 'RETURN_PENDING')"
    with connect_db() as connection:
        def one(query, params=()):
            return connection.execute(query, params).fetchone()["n"]

        metrics = {
            # Inventory
            "items": one("SELECT COUNT(*) AS n FROM inventory"),
            "on_hand": one("SELECT COALESCE(SUM(stock), 0) AS n FROM inventory"),
            "categories": one("SELECT COUNT(DISTINCT category) AS n FROM inventory"),
            "healthy": one("SELECT COUNT(*) AS n FROM inventory WHERE stock > ?", (LOW_STOCK_THRESHOLD,)),
            "low_stock": one(
                "SELECT COUNT(*) AS n FROM inventory WHERE stock > 0 AND stock <= ?",
                (LOW_STOCK_THRESHOLD,),
            ),
            "out_of_stock": one("SELECT COUNT(*) AS n FROM inventory WHERE stock <= 0"),
            "value": one("SELECT COALESCE(SUM(stock * unit_price), 0) AS n FROM inventory"),
            # Borrowing
            "total_loans": one(
                "SELECT COUNT(*) AS n FROM borrow_log WHERE status IN ('BORROWED', 'RETURN_PENDING', 'RETURNED')"
            ),
            "on_loan": one(f"SELECT COALESCE(SUM(quantity), 0) AS n FROM borrow_log WHERE status IN {out}"),
            "borrowed_records": one(f"SELECT COUNT(*) AS n FROM borrow_log WHERE status IN {out}"),
            "returned_records": one("SELECT COUNT(*) AS n FROM borrow_log WHERE status = 'RETURNED'"),
            "pending_borrows": one("SELECT COUNT(*) AS n FROM borrow_log WHERE status = 'PENDING_BORROW'"),
            "pending_returns": one("SELECT COUNT(*) AS n FROM borrow_log WHERE status = 'RETURN_PENDING'"),
            "borrowers": one(
                f"SELECT COUNT(DISTINCT school_id) AS n FROM borrow_log "
                f"WHERE status IN {out} AND school_id != ''"
            ),
            "overdue": one(
                f"SELECT COUNT(*) AS n FROM borrow_log WHERE status IN {out} AND due_date < ?", (today,)
            ),
            "overdue_units": one(
                f"SELECT COALESCE(SUM(quantity), 0) AS n FROM borrow_log "
                f"WHERE status IN {out} AND due_date < ?",
                (today,),
            ),
            # Users
            "users": one("SELECT COUNT(*) AS n FROM users"),
            "active_users": one("SELECT COUNT(*) AS n FROM users WHERE is_active = 1"),
            "pending_resets": one(
                "SELECT COUNT(*) AS n FROM password_reset_requests WHERE status = 'PENDING'"
            ),
        }

        metrics["top_items"] = [dict(r) for r in connection.execute(
            "SELECT item_name, SUM(quantity) AS total FROM borrow_log "
            "WHERE status IN ('BORROWED', 'RETURN_PENDING', 'RETURNED') "
            "GROUP BY item_name ORDER BY total DESC LIMIT 5"
        ).fetchall()]
        metrics["categories_detail"] = [dict(r) for r in connection.execute(
            """
            SELECT category,
                   COUNT(*) AS item_types,
                   COALESCE(SUM(stock), 0) AS units,
                   COALESCE(SUM(stock * unit_price), 0) AS stock_value
            FROM inventory
            GROUP BY category
            ORDER BY units DESC, category
            """
        ).fetchall()]
        metrics["recent"] = [dict(r) for r in connection.execute(
            "SELECT * FROM borrow_log ORDER BY log_id DESC LIMIT 8"
        ).fetchall()]

    metrics["value"] = float(metrics["value"] or 0)
    for entry in metrics["recent"]:
        entry["status_label"], entry["status_tag"] = loan_status(entry, today)
    return metrics


def list_activity(limit: int = 300) -> list[dict]:
    with connect_db() as connection:
        rows = connection.execute(
            "SELECT * FROM system_logs ORDER BY log_id DESC LIMIT ?", (int(limit),)
        ).fetchall()
    return [dict(row) for row in rows]


def save_low_stock_threshold(raw_value, admin: str) -> tuple[bool, str]:
    global LOW_STOCK_THRESHOLD
    try:
        value = int(str(raw_value).strip())
    except ValueError:
        return False, "The threshold must be a whole number."
    if value < 1:
        return False, "The threshold must be at least 1."
    LOW_STOCK_THRESHOLD = value
    set_setting("low_stock_threshold", str(value))
    refresh_stock_labels()
    write_system_log(admin, "SETTINGS", f"Low stock threshold set to {value}")
    return True, f"Low stock threshold set to {value}."

# ===========================================================================
# DESKTOP INTERFACE (Tkinter) - only used when Tkinter is available
# ===========================================================================

# ---------------------------------------------------------------------------
# Drawing helpers
# ---------------------------------------------------------------------------


def _shade(hex_color: str, factor: float) -> str:
    hex_color = hex_color.lstrip("#")
    r, g, b = (int(hex_color[i : i + 2], 16) for i in (0, 2, 4))
    if factor >= 0:
        r = int(r + (255 - r) * factor)
        g = int(g + (255 - g) * factor)
        b = int(b + (255 - b) * factor)
    else:
        r = int(r * (1 + factor))
        g = int(g * (1 + factor))
        b = int(b * (1 + factor))
    return f"#{max(0, min(r, 255)):02X}{max(0, min(g, 255)):02X}{max(0, min(b, 255)):02X}"


def round_rect(canvas, x1, y1, x2, y2, r, **kwargs):
    """Smoothed rounded rectangle polygon."""
    r = max(1, min(r, (x2 - x1) / 2, (y2 - y1) / 2))
    points = [
        x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
        x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
    ]
    return canvas.create_polygon(points, smooth=True, **kwargs)


def draw_icon(canvas, name: str, cx: float, cy: float, color: str, size: int = 16):
    """Small vector glyphs drawn with primitives, so no icon font is needed."""
    u = size / 16.0
    w = max(1, 1.6 * u)

    def line(x1, y1, x2, y2):
        canvas.create_line(
            cx + x1 * u, cy + y1 * u, cx + x2 * u, cy + y2 * u,
            fill=color, width=w, capstyle="round",
        )

    def rect(x1, y1, x2, y2, filled=False):
        canvas.create_rectangle(
            cx + x1 * u, cy + y1 * u, cx + x2 * u, cy + y2 * u,
            outline=color, width=w, fill=color if filled else "",
        )

    def oval(x1, y1, x2, y2, filled=False):
        canvas.create_oval(
            cx + x1 * u, cy + y1 * u, cx + x2 * u, cy + y2 * u,
            outline=color, width=w, fill=color if filled else "",
        )

    def arc(x1, y1, x2, y2, start, extent):
        canvas.create_arc(
            cx + x1 * u, cy + y1 * u, cx + x2 * u, cy + y2 * u,
            start=start, extent=extent, style="arc", outline=color, width=w,
        )

    if name == "dashboard":
        rect(-7, -7, -1, -2)
        rect(1, -7, 7, 0)
        rect(-7, 0, -1, 7)
        rect(1, 2, 7, 7)
    elif name == "inventory":
        rect(-7, -4, 7, 6)
        line(-7, -1, 7, -1)
        line(-2, -1, -2, 6)
    elif name == "log":
        line(-5, -5, 6, -5)
        line(-5, 0, 6, 0)
        line(-5, 5, 2, 5)
        oval(-8, -6.2, -6.4, -4.6, filled=True)
    elif name == "students":
        oval(-4, -7, 4, 0)
        arc(-7, -1, 7, 11, 0, 180)
    elif name == "users":
        oval(-7, -6, -1, 0)
        oval(1.5, -4.5, 6.5, 0.5)
        arc(-8, -1, 2, 9, 0, 180)
    elif name == "activity":
        line(-7, 2, -3, 2)
        line(-3, 2, -1, -5)
        line(-1, -5, 2, 5)
        line(2, 5, 4, 0)
        line(4, 0, 7, 0)
    elif name == "settings":
        oval(-3, -3, 3, 3)
        for angle in range(0, 360, 45):
            rad = math.radians(angle)
            line(4.6 * math.cos(rad), 4.6 * math.sin(rad), 7 * math.cos(rad), 7 * math.sin(rad))
    elif name == "search":
        oval(-6, -6, 2, 2)
        line(2, 2, 6, 6)
    elif name == "logout":
        line(-6, -6, -6, 6)
        line(-6, -6, 1, -6)
        line(-6, 6, 1, 6)
        line(-1, 0, 7, 0)
        line(4, -3, 7, 0)
        line(4, 3, 7, 0)


class RoundedFrame(_TkFrame):
    """A card: rounded rectangle painted on a canvas behind a content frame."""

    def __init__(
        self,
        parent,
        radius: int = 14,
        padding: int = 18,
        fill: str | None = None,
        border: str | None = None,
        shadow: bool = True,
    ):
        parent_bg = parent.cget("bg")
        super().__init__(parent, bg=parent_bg, highlightthickness=0, bd=0)
        self.radius = s(radius)
        self.fill = fill or C("card")
        self.border = border if border is not None else C("border")
        self.shadow = shadow

        self.canvas = tk.Canvas(self, bg=parent_bg, highlightthickness=0, bd=0)
        self.canvas.place(x=0, y=0, relwidth=1, relheight=1)

        # Inset so the square-cornered body never covers the rounded arcs.
        inset = max(s(4), int(self.radius * 0.30) + 1)
        self.body = tk.Frame(self, bg=self.fill, padx=s(padding), pady=s(padding))
        self.body.pack(fill="both", expand=True, padx=inset, pady=inset)

        self.bind("<Configure>", self._redraw)

    def _redraw(self, _event=None):
        width, height = self.winfo_width(), self.winfo_height()
        self.canvas.delete("all")
        if width < 6 or height < 6:
            return
        bottom = height - (s(3) if self.shadow else 1)
        if self.shadow:
            round_rect(
                self.canvas, 1, s(3), width - 1, height - 1, self.radius,
                fill=C("shadow"), outline=C("shadow"),
            )
        round_rect(
            self.canvas, 0, 0, width - 1, bottom, self.radius,
            fill=self.fill, outline=self.border or self.fill,
        )


class RoundButton(_TkCanvas):
    """Flat rounded button with hover feedback."""

    def __init__(
        self,
        parent,
        text,
        command=None,
        bg=None,
        fg="white",
        width=180,
        height=40,
        radius=9,
        font_role="label",
        outline_only=False,
        **kwargs,
    ):
        parent_bg = parent.cget("bg") if hasattr(parent, "cget") else C("card")
        super().__init__(
            parent, width=s(width), height=s(height), bg=parent_bg,
            highlightthickness=0, bd=0, **kwargs,
        )
        self.command = command
        self.base_color = bg or C("accent")
        if outline_only:
            self.fill_color = parent_bg
            self.hover_fill = C("card_alt")
            self.outline = C("border")
            self.fg = fg if fg != "white" else C("text")
        else:
            self.fill_color = self.base_color
            self.hover_fill = _shade(self.base_color, -0.14)
            self.outline = None
            self.fg = fg
        self.w, self.h = s(width), s(height)
        self.radius = s(radius)
        self.text = text
        self.font_role = font_role
        self._draw(self.fill_color)

        self.bind("<Enter>", lambda e: self._draw(self.hover_fill))
        self.bind("<Leave>", lambda e: self._draw(self.fill_color))
        self.bind("<Button-1>", self._on_click)
        self.configure(cursor="hand2")

    def _draw(self, color):
        self.delete("all")
        round_rect(
            self, 1, 1, self.w - 1, self.h - 1, self.radius,
            fill=color, outline=self.outline or color,
        )
        self.create_text(
            self.w / 2, self.h / 2, text=self.text, fill=self.fg, font=F(self.font_role, True)
        )

    def _on_click(self, _event):
        if self.command:
            self.command()


class RoundedInput(_TkFrame):
    """Rounded text field with a focus ring, placeholder and password toggle."""

    def __init__(self, parent, is_password=False, placeholder="", radius=9):
        parent_bg = parent.cget("bg")
        super().__init__(parent, bg=parent_bg, highlightthickness=0, bd=0)
        self.radius = s(radius)
        self.focused = False

        self.canvas = tk.Canvas(self, bg=parent_bg, highlightthickness=0, bd=0)
        self.canvas.place(x=0, y=0, relwidth=1, relheight=1)

        inset = max(s(3), int(self.radius * 0.3) + 1)
        self.holder = tk.Frame(self, bg=C("card_alt"))
        self.holder.pack(fill="both", expand=True, padx=inset, pady=inset)

        self.is_password = is_password
        self.is_hidden = is_password
        self.placeholder = placeholder
        self._showing_placeholder = False

        self.entry = tk.Entry(
            self.holder,
            font=F("body"),
            show="*" if is_password else "",
            bd=0,
            relief="flat",
            bg=C("card_alt"),
            fg=C("text"),
            insertbackground=C("text"),
        )
        self.entry.pack(side="left", fill="both", expand=True, padx=(s(12), 0), pady=s(9))

        if is_password:
            self.toggle_btn = tk.Label(
                self.holder, text="Show", font=F("micro", True), bg=C("card_alt"),
                fg=C("muted"), cursor="hand2",
            )
            self.toggle_btn.pack(side="right", padx=(s(6), s(12)))
            self.toggle_btn.bind("<Button-1>", self._toggle_password)
        else:
            tk.Frame(self.holder, bg=C("card_alt"), width=s(12)).pack(side="right")

        self.entry.bind("<FocusIn>", self._on_focus)
        self.entry.bind("<FocusOut>", self._off_focus)
        self.bind("<Configure>", self._redraw)

        if placeholder and not is_password:
            self._set_placeholder()

    def _redraw(self, _event=None):
        width, height = self.winfo_width(), self.winfo_height()
        self.canvas.delete("all")
        if width < 6 or height < 6:
            return
        ring = C("border_focus") if self.focused else C("border")
        round_rect(
            self.canvas, 1, 1, width - 1, height - 1, self.radius,
            fill=C("card_alt"), outline=ring,
        )

    def _set_placeholder(self):
        self._showing_placeholder = True
        self.entry.insert(0, self.placeholder)
        self.entry.config(fg=C("muted"))

    def _toggle_password(self, _event=None):
        self.is_hidden = not self.is_hidden
        self.entry.config(show="*" if self.is_hidden else "")
        self.toggle_btn.config(
            text="Show" if self.is_hidden else "Hide",
            fg=C("muted") if self.is_hidden else C("accent"),
        )

    def _on_focus(self, _event=None):
        self.focused = True
        self._redraw()
        if self._showing_placeholder:
            self.entry.delete(0, "end")
            self.entry.config(fg=C("text"))
            self._showing_placeholder = False

    def _off_focus(self, _event=None):
        self.focused = False
        self._redraw()
        if self.placeholder and not self.entry.get():
            self._set_placeholder()

    def get(self) -> str:
        return "" if self._showing_placeholder else self.entry.get()

    def set(self, value: str):
        self._showing_placeholder = False
        self.entry.config(fg=C("text"))
        self.entry.delete(0, "end")
        self.entry.insert(0, value)

    def focus(self):
        self.entry.focus_set()

    def bind_key(self, sequence, func):
        self.entry.bind(sequence, func)


class SegmentedNav(_TkCanvas):
    """Rounded pill tab switcher."""

    def __init__(self, parent, options, command, width=364, height=42):
        parent_bg = parent.cget("bg")
        super().__init__(
            parent, width=s(width), height=s(height), bg=parent_bg, highlightthickness=0, bd=0
        )
        self.options = list(options)
        self.command = command
        self.w, self.h = s(width), s(height)
        self.active = self.options[0]
        self.configure(cursor="hand2")
        self.bind("<Button-1>", self._on_click)
        self._draw()

    def _draw(self):
        self.delete("all")
        round_rect(self, 1, 1, self.w - 1, self.h - 1, s(11), fill=C("card_alt"), outline=C("border"))
        pad = s(4)
        slot = (self.w - 2 * pad) / len(self.options)
        for index, name in enumerate(self.options):
            x1 = pad + index * slot
            x2 = x1 + slot
            active = name == self.active
            if active:
                round_rect(
                    self, x1, pad, x2, self.h - pad, s(8), fill=C("accent"), outline=C("accent")
                )
            self.create_text(
                (x1 + x2) / 2, self.h / 2, text=name,
                fill="#FFFFFF" if active else C("muted"), font=F("label", True),
            )

    def _on_click(self, event):
        pad = s(4)
        slot = (self.w - 2 * pad) / len(self.options)
        index = int(max(0, min((event.x - pad) // slot, len(self.options) - 1)))
        self.select(self.options[index])

    def select(self, name: str, notify: bool = True):
        self.active = name
        self._draw()
        if notify and self.command:
            self.command(name)


class NavItem(_TkCanvas):
    """Sidebar row: rounded pill, vector icon and label."""

    def __init__(self, parent, text, icon, command, width=208, height=42):
        super().__init__(
            parent, width=s(width), height=s(height), bg=C("sidebar"), highlightthickness=0, bd=0
        )
        self.text = text
        self.icon = icon
        self.command = command
        self.w, self.h = s(width), s(height)
        self.active = False
        self.hovering = False
        self.configure(cursor="hand2")
        self.bind("<Button-1>", lambda e: self.command(self.text))
        self.bind("<Enter>", self._enter)
        self.bind("<Leave>", self._leave)
        self._draw()

    def _enter(self, _event=None):
        self.hovering = True
        self._draw()

    def _leave(self, _event=None):
        self.hovering = False
        self._draw()

    def set_active(self, active: bool):
        self.active = active
        self._draw()

    def _draw(self):
        self.delete("all")
        if self.active:
            fill, fg = C("sidebar_active"), C("sidebar_text_active")
        elif self.hovering:
            fill, fg = C("sidebar_hover"), C("white")
        else:
            fill, fg = C("sidebar"), C("sidebar_text")
        round_rect(self, 2, 2, self.w - 2, self.h - 2, s(9), fill=fill, outline=fill)
        draw_icon(self, self.icon, s(22), self.h / 2, fg, size=s(15))
        self.create_text(s(42), self.h / 2, text=self.text, anchor="w", fill=fg, font=F("body", True))


class Donut(_TkCanvas):
    """Ring chart with a centered percentage - stock health at a glance."""

    def __init__(self, parent, segments, center_value, center_label, size=134, thickness=16):
        parent_bg = parent.cget("bg")
        super().__init__(
            parent, width=s(size), height=s(size), bg=parent_bg, highlightthickness=0, bd=0
        )
        dim = s(size)
        pad = s(thickness) / 2 + s(2)
        total = sum(value for value, _ in segments) or 1

        self.create_oval(
            pad, pad, dim - pad, dim - pad, outline=C("track"), width=s(thickness), style="arc"
        )
        start = 90.0
        for value, color in segments:
            if value <= 0:
                continue
            extent = -359.9 * value / total
            self.create_arc(
                pad, pad, dim - pad, dim - pad, start=start, extent=extent,
                outline=color, width=s(thickness), style="arc",
            )
            start += extent
        self.create_text(
            dim / 2, dim / 2 - s(7), text=center_value, fill=C("heading"), font=F("title", True)
        )
        self.create_text(
            dim / 2, dim / 2 + s(13), text=center_label, fill=C("muted"), font=F("micro")
        )


class Toast(_TkToplevel):
    """Corner notification that fades out on its own."""

    def __init__(self, parent, message, kind="success", duration=2600):
        super().__init__(parent)
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        colors = {
            "success": (C("green"), C("green_soft")),
            "error": (C("red"), C("red_soft")),
            "info": (C("blue"), C("blue_soft")),
            "warning": (C("orange"), C("orange_soft")),
        }
        fg, bg = colors.get(kind, colors["info"])
        self.configure(bg=bg)

        frame = tk.Frame(self, bg=bg, highlightbackground=fg, highlightthickness=1)
        frame.pack(fill="both", expand=True)
        tk.Frame(frame, bg=fg, width=s(4)).pack(side="left", fill="y")
        tk.Label(
            frame, text=message, font=F("label", True), fg=_shade(fg, -0.35), bg=bg,
            wraplength=s(280), justify="left",
        ).pack(side="left", padx=s(14), pady=s(12))

        self.update_idletasks()
        parent.update_idletasks()
        px, py = parent.winfo_rootx(), parent.winfo_rooty()
        pw, ph = parent.winfo_width(), parent.winfo_height()
        w, h = self.winfo_width(), self.winfo_height()
        self.geometry(f"+{px + pw - w - s(26)}+{py + ph - h - s(30)}")
        self.after(duration, self._fade)

    def _fade(self, alpha: float = 1.0):
        if alpha <= 0.05:
            try:
                self.destroy()
            except _TclError:
                pass
            return
        try:
            self.attributes("-alpha", alpha)
            self.after(30, lambda: self._fade(alpha - 0.08))
        except _TclError:
            pass


def show_toast(parent, message, kind="success"):
    try:
        Toast(parent.winfo_toplevel(), message, kind)
    except _TclError:
        pass


def apply_ttk_styles(root) -> None:
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except _TclError:
        pass

    style.configure(
        "Modern.Treeview",
        background=C("card"),
        fieldbackground=C("card"),
        foreground=C("text"),
        bordercolor=C("card"),
        borderwidth=0,
        rowheight=s(36),
        font=F("label"),
    )
    style.map(
        "Modern.Treeview",
        background=[("selected", C("accent_soft"))],
        foreground=[("selected", C("text"))],
    )
    style.configure(
        "Modern.Treeview.Heading",
        background=C("table_head"),
        foreground=C("muted"),
        relief="flat",
        borderwidth=0,
        padding=(s(10), s(10)),
        font=F("micro", True),
    )
    style.map("Modern.Treeview.Heading", background=[("active", C("table_head"))])
    style.layout("Modern.Treeview", [("Modern.Treeview.treearea", {"sticky": "nswe"})])

    style.configure(
        "Modern.Vertical.TScrollbar",
        background=C("border"),
        troughcolor=C("card"),
        bordercolor=C("card"),
        arrowcolor=C("muted"),
        relief="flat",
        borderwidth=0,
    )
    style.map("Modern.Vertical.TScrollbar", background=[("active", C("muted"))])

    style.configure(
        "Modern.TCombobox",
        fieldbackground=C("card_alt"),
        background=C("card_alt"),
        foreground=C("text"),
        bordercolor=C("border"),
        lightcolor=C("border"),
        darkcolor=C("border"),
        arrowcolor=C("muted"),
        selectbackground=C("card_alt"),
        selectforeground=C("text"),
        padding=s(8),
    )
    style.map(
        "Modern.TCombobox",
        fieldbackground=[("readonly", C("card_alt"))],
        bordercolor=[("focus", C("border_focus"))],
    )
    root.option_add("*TCombobox*Listbox.background", C("card"))
    root.option_add("*TCombobox*Listbox.foreground", C("text"))
    root.option_add("*TCombobox*Listbox.selectBackground", C("accent"))
    root.option_add("*TCombobox*Listbox.selectForeground", "#FFFFFF")
    root.option_add("*TCombobox*Listbox.font", F("label"))


class WidgetFactoryMixin:
    """Shared builders so every screen composes from the same parts."""

    def make_label(self, parent, text, role="body", bold=False, color=None, bg=None):
        return tk.Label(
            parent,
            text=text,
            font=F(role, bold),
            fg=color or C("text"),
            bg=bg or parent.cget("bg"),
        )

    def section_title(self, parent, text, subtitle=None, role="title"):
        wrap = tk.Frame(parent, bg=parent.cget("bg"))
        wrap.pack(fill="x", pady=(0, s(18)))
        tk.Label(wrap, text=text, font=F(role, True), fg=C("heading"), bg=parent.cget("bg")).pack(
            anchor="w"
        )
        if subtitle:
            tk.Label(
                wrap, text=subtitle, font=F("label"), fg=C("muted"), bg=parent.cget("bg"),
                justify="left", wraplength=s(370),
            ).pack(anchor="w", pady=(s(4), 0))
        return wrap

    def field(self, parent, label, is_password=False, placeholder="", pad=(0, 14), hint=None):
        tk.Label(
            parent, text=label, font=F("label", True), fg=C("muted"), bg=parent.cget("bg")
        ).pack(anchor="w", pady=(0, s(5)))
        widget = RoundedInput(parent, is_password=is_password, placeholder=placeholder)
        widget.pack(fill="x", pady=(0, s(4) if hint else s(pad[1])))
        if hint:
            tk.Label(
                parent, text=hint, font=F("micro"), fg=C("muted"), bg=parent.cget("bg"),
                justify="left", wraplength=s(360),
            ).pack(anchor="w", pady=(0, s(pad[1])))
        return widget

# ---------------------------------------------------------------------------
# Authentication (desktop)
# ---------------------------------------------------------------------------


class AuthenticationWindow(WidgetFactoryMixin):
    """Split-screen sign in, register and reset request screen."""

    def __init__(self, app: "InventoryApp"):
        self.app = app
        self.root = app.root
        self.root.title("Campus Hardware Inventory — Sign in")
        self.root.configure(bg=C("bg"))
        self.views = {}
        self._build_interface()

    def _build_interface(self):
        self.container = tk.Frame(self.root, bg=C("bg"))
        self.container.pack(fill="both", expand=True)

        hero = tk.Frame(self.container, bg=C("sidebar"), width=s(440))
        hero.pack(side="left", fill="y")
        hero.pack_propagate(False)

        inner = tk.Frame(hero, bg=C("sidebar"))
        inner.pack(fill="both", expand=True, padx=s(46), pady=s(52))

        badge = tk.Frame(inner, bg=C("sidebar"))
        badge.pack(anchor="w", pady=(0, s(30)))
        logo = tk.Canvas(
            badge, width=s(34), height=s(34), bg=C("sidebar"), highlightthickness=0, bd=0
        )
        logo.pack(side="left")
        round_rect(logo, 0, 0, s(34), s(34), s(10), fill=C("accent"), outline=C("accent"))
        draw_icon(logo, "inventory", s(17), s(17), "#FFFFFF", size=s(17))
        tk.Label(
            badge, text="Campus Hardware", font=F("heading", True), fg="white", bg=C("sidebar")
        ).pack(side="left", padx=s(12))

        tk.Label(
            inner,
            text="Every cable, board and\ntool accounted for.",
            font=F("display", True),
            fg=C("white"),
            bg=C("sidebar"),
            justify="left",
        ).pack(anchor="w")

        tk.Label(
            inner,
            text="Track lab stock, check equipment out to students by school ID, and see what is overdue before the semester ends.",
            font=F("label"),
            fg=C("sidebar_text"),
            bg=C("sidebar"),
            justify="left",
            wraplength=s(330),
        ).pack(anchor="w", pady=(s(14), s(34)))

        features = [
            ("students", "Student ID checkout", "Every loan is tied to a verified school ID"),
            ("inventory", "Live stock counts", "Stock moves the moment an item leaves or returns"),
            ("log", "Overdue watchlist", "Due dates flagged the day they pass"),
            ("activity", "Full audit trail", "Who processed what, and when"),
        ]
        for icon, title, desc in features:
            row = tk.Frame(inner, bg=C("sidebar"))
            row.pack(fill="x", pady=s(8), anchor="w")
            glyph = tk.Canvas(
                row, width=s(34), height=s(34), bg=C("sidebar"), highlightthickness=0, bd=0
            )
            glyph.pack(side="left")
            round_rect(
                glyph, 0, 0, s(34), s(34), s(10), fill=C("sidebar_hover"), outline=C("sidebar_hover")
            )
            draw_icon(glyph, icon, s(17), s(17), C("accent"), size=s(15))
            box = tk.Frame(row, bg=C("sidebar"))
            box.pack(side="left", padx=s(13))
            tk.Label(box, text=title, font=F("label", True), fg="white", bg=C("sidebar")).pack(
                anchor="w"
            )
            tk.Label(
                box, text=desc, font=F("micro"), fg=C("sidebar_text"), bg=C("sidebar"),
                wraplength=s(280), justify="left",
            ).pack(anchor="w")

        tk.Label(
            inner, text=f"v3.2    {database_label().split(' ')[0]}    Role-based access",
            font=F("micro"), fg=C("sidebar_text"), bg=C("sidebar"),
        ).pack(side="bottom", anchor="w")

        right = tk.Frame(self.container, bg=C("bg"))
        right.pack(side="left", fill="both", expand=True)

        center = tk.Frame(right, bg=C("bg"))
        center.place(relx=0.5, rely=0.5, anchor="center")

        card = RoundedFrame(center, radius=18, padding=32)
        card.pack(padx=s(20), pady=s(20))
        card_inner = card.body

        self.nav = SegmentedNav(
            card_inner, ["Sign in", "Register", "Reset"], command=self._show_view, width=364
        )
        self.nav.pack(pady=(0, s(24)))

        self.views_container = tk.Frame(card_inner, bg=C("card"))
        self.views_container.pack(fill="both", expand=True)

        self._build_login_view()
        self._build_register_view()
        self._build_reset_view()
        self._show_view("Sign in")

    def _show_view(self, key: str):
        for name, view in self.views.items():
            if name == key:
                view.pack(fill="both", expand=True)
            else:
                view.pack_forget()

    def _build_login_view(self):
        view = tk.Frame(self.views_container, bg=C("card"))
        self.views["Sign in"] = view

        self.section_title(view, "Welcome back", "Sign in with your account credentials.")
        self.login_username = self.field(view, "Username", placeholder="admin")
        self.login_password = self.field(view, "Password", is_password=True, pad=(0, 20))
        self.login_password.bind_key("<Return>", lambda e: self.login())
        self.login_username.bind_key("<Return>", lambda e: self.login())

        RoundButton(
            view, "Sign in", command=self.login, width=364, height=44, font_role="body"
        ).pack(pady=(0, s(20)))

        note = RoundedFrame(view, radius=11, padding=13, fill=C("card_alt"), shadow=False)
        note.pack(fill="x")
        tk.Label(
            note.body, text="Accounts lock after 3 failed attempts", font=F("micro", True),
            fg=C("text"), bg=C("card_alt"),
        ).pack(anchor="w")
        tk.Label(
            note.body,
            text="Locked out? Submit a reset request and an administrator will review it.",
            font=F("micro"), fg=C("muted"), bg=C("card_alt"), justify="left", wraplength=s(310),
        ).pack(anchor="w", pady=(s(3), 0))

    def _build_register_view(self):
        view = tk.Frame(self.views_container, bg=C("card"))
        self.views["Register"] = view

        self.section_title(view, "Create account", "New lab staff or student assistant access.")
        self.register_username = self.field(view, "Username", pad=(0, 12))
        self.register_email = self.field(view, "Email address", pad=(0, 12))
        self.register_password = self.field(
            view, "Password", is_password=True, pad=(0, 14),
            hint="8+ characters with an uppercase, lowercase, number and one of @#$%^&*!?",
        )

        role_row = tk.Frame(view, bg=C("card"))
        role_row.pack(fill="x", pady=(0, s(18)))
        tk.Label(
            role_row, text="Account role", font=F("label", True), fg=C("muted"), bg=C("card")
        ).pack(side="left", pady=s(6))
        self.register_role = tk.StringVar(value="USER")
        ttk.Combobox(
            role_row, textvariable=self.register_role, values=("USER", "ADMIN"),
            state="readonly", width=12, style="Modern.TCombobox", font=F("label"),
        ).pack(side="right")

        RoundButton(
            view, "Create account", command=self.register, bg=C("green"), width=364, height=44,
            font_role="body",
        ).pack()

    def _build_reset_view(self):
        view = tk.Frame(self.views_container, bg=C("card"))
        self.views["Reset"] = view

        self.section_title(
            view, "Reset password", "An administrator reviews every request before it takes effect."
        )
        self.reset_username = self.field(view, "Account username", pad=(0, 10))
        self.reset_email = self.field(view, "Registered email", pad=(0, 10))
        self.reset_password = self.field(view, "New password", is_password=True, pad=(0, 10))
        self.reset_confirm = self.field(view, "Confirm new password", is_password=True, pad=(0, 16))

        RoundButton(
            view, "Send request", command=self.submit_reset_request, bg=C("yellow"),
            width=364, height=44, font_role="body",
        ).pack()

    # -- actions (now call the shared controllers) ---------------------------

    def login(self):
        ok, message, user = authenticate_user(
            self.login_username.get(), self.login_password.get()
        )
        if not ok:
            messagebox.showerror("Sign in", message)
            return
        self.app.open_dashboard(user)

    def register(self):
        ok, message = register_user(
            self.register_username.get(),
            self.register_email.get(),
            self.register_password.get(),
            self.register_role.get(),
        )
        if not ok:
            messagebox.showerror("Create account", message)
            return
        show_toast(self.root, message, "success")
        self.nav.select("Sign in")

    def submit_reset_request(self):
        ok, message = submit_password_reset(
            self.reset_username.get(),
            self.reset_email.get(),
            self.reset_password.get(),
            self.reset_confirm.get(),
        )
        if not ok:
            messagebox.showerror("Reset password", message)
            return
        show_toast(self.root, message, "info")
        self.nav.select("Sign in")


# ---------------------------------------------------------------------------
# Modal dialogs (desktop)
# ---------------------------------------------------------------------------


class ModalDialog(_TkToplevel, WidgetFactoryMixin):
    def __init__(self, parent, title, width=470, height=560):
        super().__init__(parent)
        self.title(title)
        self.configure(bg=C("card"))
        self.resizable(False, False)
        self.transient(parent.winfo_toplevel())
        self.result = None

        parent.update_idletasks()
        top = parent.winfo_toplevel()
        x = top.winfo_rootx() + (top.winfo_width() - s(width)) // 2
        y = top.winfo_rooty() + max(s(20), (top.winfo_height() - s(height)) // 3)
        self.geometry(f"{s(width)}x{s(height)}+{x}+{y}")

        self.body = tk.Frame(self, bg=C("card"), padx=s(30), pady=s(28))
        self.body.pack(fill="both", expand=True)
        self.bind("<Escape>", lambda e: self.destroy())

    def finish(self, value=None):
        self.result = value
        self.destroy()


class BorrowDialog(ModalDialog):
    """Check an item out to a student. School ID is required."""

    def __init__(self, parent, processed_by: str, preset_item_id: int | None = None):
        super().__init__(parent, "New borrow", width=480, height=680)
        self.processed_by = processed_by
        self.items = list_borrowable_items()

        self.section_title(
            self.body, "New borrow", "Items leave the shelf against a student school ID."
        )

        if not self.items:
            tk.Label(
                self.body,
                text="Nothing is available to borrow — every item is at zero stock.",
                font=F("label"), fg=C("muted"), bg=C("card"), wraplength=s(380), justify="left",
            ).pack(anchor="w", pady=(0, s(20)))
            RoundButton(
                self.body, "Close", command=self.destroy, outline_only=True, width=410, height=42
            ).pack()
            self.grab_set()
            return

        self.labels = [f"{row['item_name']}    {row['stock']} on hand" for row in self.items]
        self.item_map = dict(zip(self.labels, [row["item_id"] for row in self.items]))

        tk.Label(self.body, text="Item", font=F("label", True), fg=C("muted"), bg=C("card")).pack(
            anchor="w", pady=(0, s(5))
        )
        self.item_var = tk.StringVar(value=self.labels[0])
        if preset_item_id is not None:
            for label, item_id in self.item_map.items():
                if item_id == preset_item_id:
                    self.item_var.set(label)
                    break
        ttk.Combobox(
            self.body, textvariable=self.item_var, values=self.labels, state="readonly",
            style="Modern.TCombobox", font=F("label"),
        ).pack(fill="x", pady=(0, s(14)))

        self.name_input = self.field(
            self.body, "Student name", placeholder="Juan Dela Cruz", pad=(0, 14)
        )

        tk.Label(
            self.body, text="Student school ID", font=F("label", True), fg=C("muted"), bg=C("card")
        ).pack(anchor="w", pady=(0, s(5)))
        self.school_input = RoundedInput(self.body, placeholder="2023-04517")
        self.school_input.pack(fill="x", pady=(0, s(5)))
        tk.Label(
            self.body,
            text="4-20 characters, at least 4 digits. Saved in uppercase.",
            font=F("micro"), fg=C("muted"), bg=C("card"), justify="left",
        ).pack(anchor="w")
        self.history_hint = tk.Label(
            self.body, text="", font=F("micro", True), fg=C("orange"), bg=C("card"),
            justify="left", wraplength=s(390),
        )
        self.history_hint.pack(anchor="w", pady=(s(4), s(14)))
        self.school_input.entry.bind("<FocusOut>", self._lookup_student, add="+")
        self.school_input.entry.bind("<Return>", self._lookup_student, add="+")

        row = tk.Frame(self.body, bg=C("card"))
        row.pack(fill="x", pady=(0, s(14)))

        qty_col = tk.Frame(row, bg=C("card"))
        qty_col.pack(side="left", fill="x", expand=True, padx=(0, s(8)))
        tk.Label(qty_col, text="Quantity", font=F("label", True), fg=C("muted"), bg=C("card")).pack(
            anchor="w", pady=(0, s(5))
        )
        self.qty_input = RoundedInput(qty_col)
        self.qty_input.pack(fill="x")
        self.qty_input.set("1")

        days_col = tk.Frame(row, bg=C("card"))
        days_col.pack(side="left", fill="x", expand=True, padx=(s(8), 0))
        tk.Label(
            days_col, text="Due in (days)", font=F("label", True), fg=C("muted"), bg=C("card")
        ).pack(anchor="w", pady=(0, s(5)))
        self.days_input = RoundedInput(days_col)
        self.days_input.pack(fill="x")
        self.days_input.set(str(DEFAULT_LOAN_DAYS))

        self.notes_input = self.field(
            self.body, "Notes (optional)", placeholder="Lab section, purpose, condition", pad=(0, 22)
        )

        actions = tk.Frame(self.body, bg=C("card"))
        actions.pack(fill="x")
        RoundButton(
            actions, "Cancel", command=self.destroy, outline_only=True, width=130, height=42
        ).pack(side="left")
        RoundButton(actions, "Check out item", command=self.save, width=250, height=42).pack(
            side="right"
        )

        self.name_input.focus()
        self.grab_set()

    def _lookup_student(self, _event=None):
        value = self.school_input.get().strip().upper()
        if not value:
            self.history_hint.config(text="")
            return
        rows = [r for r in student_history(value) if r["status"] != "REJECTED"]
        open_loans = [r for r in rows if r["status"] in OUT_STATUSES]
        if not rows:
            self.history_hint.config(text="First borrow on record for this ID.", fg=C("muted"))
        elif open_loans:
            names = ", ".join(sorted({r["item_name"] for r in open_loans}))
            self.history_hint.config(
                text=f"{rows[0]['borrower_name']} — {len(open_loans)} item(s) still out: {names}",
                fg=C("orange"),
            )
        else:
            self.history_hint.config(
                text=f"{rows[0]['borrower_name']} — {len(rows)} past loan(s), all returned.",
                fg=C("green"),
            )

    def save(self):
        try:
            quantity = int(self.qty_input.get().strip() or "0")
        except ValueError:
            messagebox.showerror("New borrow", "Quantity must be a whole number.", parent=self)
            return
        try:
            loan_days = int(self.days_input.get().strip() or str(DEFAULT_LOAN_DAYS))
        except ValueError:
            messagebox.showerror("New borrow", "Due in (days) must be a whole number.", parent=self)
            return

        try:
            data = BorrowSchema(
                borrower_name=self.name_input.get(),
                school_id=self.school_input.get(),
                quantity=quantity,
                loan_days=loan_days,
                notes=self.notes_input.get(),
            )
        except ValidationError as error:
            messagebox.showerror("New borrow", first_error(error), parent=self)
            return

        item_id = self.item_map.get(self.item_var.get())
        if item_id is None:
            messagebox.showerror("New borrow", "Choose an item to check out.", parent=self)
            return

        ok, message = record_borrow(item_id, data, self.processed_by)
        if not ok:
            messagebox.showerror("New borrow", message, parent=self)
            return
        self.finish(message)


class ItemDialog(ModalDialog):
    def __init__(self, parent, processed_by: str, item: dict | None = None):
        title = "Edit item" if item else "New item"
        super().__init__(parent, title, width=450, height=500)
        self.processed_by = processed_by
        self.item = item

        self.section_title(
            self.body, title, "Stock status updates itself from the quantity on hand."
        )
        self.name_input = self.field(self.body, "Item name", pad=(0, 14))

        tk.Label(
            self.body, text="Category", font=F("label", True), fg=C("muted"), bg=C("card")
        ).pack(anchor="w", pady=(0, s(5)))
        self.category_var = tk.StringVar(value=CATEGORIES[0])
        ttk.Combobox(
            self.body, textvariable=self.category_var, values=CATEGORIES,
            style="Modern.TCombobox", font=F("label"),
        ).pack(fill="x", pady=(0, s(14)))

        row = tk.Frame(self.body, bg=C("card"))
        row.pack(fill="x", pady=(0, s(22)))
        left = tk.Frame(row, bg=C("card"))
        left.pack(side="left", fill="x", expand=True, padx=(0, s(8)))
        tk.Label(
            left, text="Stock on hand", font=F("label", True), fg=C("muted"), bg=C("card")
        ).pack(anchor="w", pady=(0, s(5)))
        self.stock_input = RoundedInput(left)
        self.stock_input.pack(fill="x")

        right = tk.Frame(row, bg=C("card"))
        right.pack(side="left", fill="x", expand=True, padx=(s(8), 0))
        tk.Label(
            right, text="Unit price", font=F("label", True), fg=C("muted"), bg=C("card")
        ).pack(anchor="w", pady=(0, s(5)))
        self.price_input = RoundedInput(right)
        self.price_input.pack(fill="x")

        if item:
            self.name_input.set(item["item_name"])
            self.category_var.set(item["category"])
            self.stock_input.set(str(item["stock"]))
            self.price_input.set(f"{item['unit_price']:.2f}")
        else:
            self.stock_input.set("0")
            self.price_input.set("0.00")

        actions = tk.Frame(self.body, bg=C("card"))
        actions.pack(fill="x")
        RoundButton(
            actions, "Cancel", command=self.destroy, outline_only=True, width=120, height=42
        ).pack(side="left")
        RoundButton(actions, "Save item", command=self.save, width=230, height=42).pack(side="right")

        self.name_input.focus()
        self.grab_set()

    def save(self):
        try:
            stock = int(self.stock_input.get().strip() or "0")
            price = float(self.price_input.get().strip() or "0")
        except ValueError:
            messagebox.showerror("Save item", "Stock and unit price must be numbers.", parent=self)
            return

        try:
            data = ItemSchema(
                item_name=self.name_input.get(),
                category=self.category_var.get(),
                stock=stock,
                unit_price=price,
            )
        except ValidationError as error:
            messagebox.showerror("Save item", first_error(error), parent=self)
            return

        if self.item:
            ok, message = update_item(self.item["item_id"], data, self.processed_by)
        else:
            existing = find_item_by_name(data.item_name)
            if existing:
                # The record may exist but be hidden by the current search filter.
                if data.stock <= 0:
                    messagebox.showinfo(
                        "Item already exists",
                        f'"{existing["item_name"]}" already exists (ID {existing["item_id"]}) '
                        "and has no additional stock to add.",
                        parent=self,
                    )
                    return
                if not messagebox.askyesno(
                    "Item already exists",
                    f'"{existing["item_name"]}" is already stored in the database '
                    f"(ID {existing['item_id']}) but may be outside the current search view.\n\n"
                    f"Current stock: {existing['stock']} unit(s)\n"
                    f"Add {data.stock} unit(s) to that existing item instead?",
                    parent=self,
                ):
                    return
                ok, message = restock_item(existing["item_id"], data.stock, self.processed_by)
            else:
                ok, message = create_item(data, self.processed_by)

        if not ok:
            messagebox.showerror("Save item", message, parent=self)
            return
        self.finish(message)

# ---------------------------------------------------------------------------
# Dashboard (desktop)
# ---------------------------------------------------------------------------


class DashboardWindow(WidgetFactoryMixin):
    NAV_ITEMS = [
        ("Dashboard", "dashboard", False),
        ("Inventory", "inventory", False),
        ("Borrow log", "log", False),
        ("Students", "students", False),
        ("Users", "users", True),
        ("Activity", "activity", True),
        ("Settings", "settings", True),
    ]

    def __init__(self, app: "InventoryApp", user: dict):
        self.app = app
        self.root = app.root
        self.user = user
        self.is_admin = user["role"] == "ADMIN"
        self.current_page = "Dashboard"
        self.nav_buttons = {}

        self.root.title(f"Campus Hardware Inventory — {user['username']}")
        self.root.configure(bg=C("bg"))
        refresh_stock_labels()
        self._build_shell()
        self.show_page("Dashboard")

    # -- shell --------------------------------------------------------------

    def _build_shell(self):
        self.container = tk.Frame(self.root, bg=C("bg"))
        self.container.pack(fill="both", expand=True)

        sidebar = tk.Frame(self.container, bg=C("sidebar"), width=s(236))
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)

        brand = tk.Frame(sidebar, bg=C("sidebar"))
        brand.pack(fill="x", padx=s(20), pady=(s(20), s(24)))
        logo = tk.Canvas(
            brand, width=s(32), height=s(32), bg=C("sidebar"), highlightthickness=0, bd=0
        )
        logo.pack(side="left")
        round_rect(logo, 0, 0, s(32), s(32), s(9), fill=C("accent"), outline=C("accent"))
        draw_icon(logo, "inventory", s(16), s(16), "#FFFFFF", size=s(16))
        tk.Label(
            brand, text="Inventory", font=F("heading", True), fg="white", bg=C("sidebar")
        ).pack(side="left", padx=s(11))

        for name, icon, admin_only in self.NAV_ITEMS:
            if admin_only and not self.is_admin:
                continue
            item = NavItem(sidebar, name, icon, self.show_page)
            item.pack(padx=s(12), pady=s(2))
            self.nav_buttons[name] = item

        footer = tk.Frame(sidebar, bg=C("sidebar"))
        footer.pack(side="bottom", fill="x", padx=s(18), pady=s(20))
        tk.Frame(footer, bg=C("sidebar_divider"), height=1).pack(fill="x", pady=(0, s(16)))

        who = tk.Frame(footer, bg=C("sidebar"))
        who.pack(fill="x")
        avatar = tk.Canvas(
            who, width=s(36), height=s(36), bg=C("sidebar"), highlightthickness=0, bd=0
        )
        avatar.pack(side="left")
        avatar.create_oval(0, 0, s(36), s(36), fill=C("sidebar_hover"), outline=C("sidebar_hover"))
        avatar.create_text(
            s(18), s(18), text=self.user["username"][:2].upper(), fill="white", font=F("label", True)
        )
        info = tk.Frame(who, bg=C("sidebar"))
        info.pack(side="left", padx=s(11))
        tk.Label(
            info, text=self.user["username"], font=F("label", True), fg="white", bg=C("sidebar")
        ).pack(anchor="w")
        tk.Label(
            info, text=self.user["role"].title(), font=F("micro"), fg=C("sidebar_text"),
            bg=C("sidebar"),
        ).pack(anchor="w")

        signout = tk.Canvas(
            footer, width=s(190), height=s(38), bg=C("sidebar"), highlightthickness=0, bd=0,
            cursor="hand2",
        )
        signout.pack(anchor="w", pady=(s(14), 0))
        draw_icon(signout, "logout", s(14), s(19), C("sidebar_text"), size=s(14))
        signout.create_text(
            s(32), s(19), text="Sign out", anchor="w", fill=C("sidebar_text"), font=F("label", True)
        )
        signout.bind("<Button-1>", lambda e: self.sign_out())

        main = tk.Frame(self.container, bg=C("bg"))
        main.pack(side="left", fill="both", expand=True)

        topbar = tk.Frame(main, bg=C("topbar"), height=s(66))
        topbar.pack(fill="x")
        topbar.pack_propagate(False)
        tk.Frame(main, bg=C("border"), height=1).pack(fill="x")

        self.page_title = tk.Label(
            topbar, text="Dashboard", font=F("heading", True), fg=C("heading"), bg=C("topbar")
        )
        self.page_title.pack(side="left", padx=s(26))

        right = tk.Frame(topbar, bg=C("topbar"))
        right.pack(side="right", padx=s(22))

        RoundButton(right, "New borrow", command=self.new_borrow, width=132, height=38).pack(
            side="right", padx=(s(12), 0)
        )
        RoundButton(
            right,
            "Dark" if STATE["theme"] == "light" else "Light",
            command=self.app.toggle_theme,
            outline_only=True,
            width=76,
            height=38,
        ).pack(side="right")

        self.content = tk.Frame(main, bg=C("bg"))
        self.content.pack(fill="both", expand=True)

    def _highlight_nav(self):
        for name, item in self.nav_buttons.items():
            item.set_active(name == self.current_page)

    def clear_content(self):
        for widget in self.content.winfo_children():
            widget.destroy()

    def scroll_area(self):
        canvas = tk.Canvas(self.content, bg=C("bg"), highlightthickness=0, bd=0)
        scrollbar = ttk.Scrollbar(
            self.content, orient="vertical", command=canvas.yview,
            style="Modern.Vertical.TScrollbar",
        )
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        inner = tk.Frame(canvas, bg=C("bg"), padx=s(26), pady=s(24))
        window = canvas.create_window((0, 0), window=inner, anchor="nw")

        def _resize(_event=None):
            canvas.configure(scrollregion=canvas.bbox("all"))
            canvas.itemconfig(window, width=canvas.winfo_width())

        inner.bind("<Configure>", _resize)
        canvas.bind("<Configure>", _resize)
        canvas.bind_all("<MouseWheel>", lambda e: canvas.yview_scroll(int(-e.delta / 120), "units"))
        return inner

    def show_page(self, name: str):
        self.current_page = name
        self._highlight_nav()
        self.page_title.config(text=name)
        self.clear_content()
        {
            "Dashboard": self.page_dashboard,
            "Inventory": self.page_inventory,
            "Borrow log": self.page_borrow_log,
            "Students": self.page_students,
            "Users": self.page_users,
            "Activity": self.page_activity,
            "Settings": self.page_settings,
        }[name]()

    # -- reusable page parts ------------------------------------------------

    def page_header(self, parent, title, subtitle):
        header = tk.Frame(parent, bg=parent.cget("bg"))
        header.pack(fill="x", pady=(0, s(18)))
        titles = tk.Frame(header, bg=parent.cget("bg"))
        titles.pack(side="left")
        tk.Label(
            titles, text=title, font=F("title", True), fg=C("heading"), bg=parent.cget("bg")
        ).pack(anchor="w")
        tk.Label(
            titles, text=subtitle, font=F("label"), fg=C("muted"), bg=parent.cget("bg")
        ).pack(anchor="w", pady=(s(4), 0))
        actions = tk.Frame(header, bg=parent.cget("bg"))
        actions.pack(side="right")
        return actions

    def stat_tile(self, parent, value, unit, label, color, last=False):
        card = RoundedFrame(parent, radius=14, padding=18)
        card.pack(side="left", fill="both", expand=True, padx=(0, 0 if last else s(14)))
        body = card.body

        strip = tk.Canvas(body, width=s(30), height=s(4), bg=C("card"), highlightthickness=0, bd=0)
        strip.pack(anchor="w", pady=(0, s(12)))
        round_rect(strip, 0, 0, s(30), s(4), s(2), fill=color, outline=color)

        value_row = tk.Frame(body, bg=C("card"))
        value_row.pack(anchor="w", fill="x")
        tk.Label(
            value_row, text=str(value), font=F("metric", True), fg=C("heading"), bg=C("card")
        ).pack(side="left")
        if unit:
            tk.Label(value_row, text=unit, font=F("micro"), fg=C("muted"), bg=C("card")).pack(
                side="left", padx=s(6), pady=(s(12), 0)
            )
        tk.Label(body, text=label, font=F("label"), fg=C("muted"), bg=C("card")).pack(
            anchor="w", pady=(s(6), 0)
        )
        return card

    def panel(self, parent, title, side=None, action=None, action_command=None, last=False):
        card = RoundedFrame(parent, radius=14, padding=0)
        if side:
            card.pack(
                side=side, fill="both", expand=True, padx=(0, 0 if last else s(14)), pady=(0, s(14))
            )
        else:
            card.pack(fill="both", expand=True, pady=(0, s(14)))

        header = tk.Frame(card.body, bg=C("card"), padx=s(20), pady=s(16))
        header.pack(fill="x")
        tk.Label(
            header, text=title, font=F("heading", True), fg=C("heading"), bg=C("card")
        ).pack(side="left")
        if action and action_command:
            link = tk.Label(
                header, text=action, font=F("label", True), fg=C("accent"), bg=C("card"),
                cursor="hand2",
            )
            link.pack(side="right")
            link.bind("<Button-1>", lambda e: action_command())
        tk.Frame(card.body, bg=C("border"), height=1).pack(fill="x")

        body = tk.Frame(card.body, bg=C("card"), padx=s(20), pady=s(18))
        body.pack(fill="both", expand=True)
        return body

    def build_table(self, parent, columns, widths, height=12, anchors=None):
        wrap = tk.Frame(parent, bg=C("card"))
        wrap.pack(fill="both", expand=True)

        tree = ttk.Treeview(
            wrap, columns=columns, show="headings", height=height, style="Modern.Treeview"
        )
        scrollbar = ttk.Scrollbar(
            wrap, orient="vertical", command=tree.yview, style="Modern.Vertical.TScrollbar"
        )
        tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        tree.pack(side="left", fill="both", expand=True)

        for index, column in enumerate(columns):
            anchor = anchors[index] if anchors else "w"
            tree.heading(column, text=column, anchor=anchor)
            tree.column(column, width=s(widths[index]), anchor=anchor, stretch=True)

        tree.tag_configure("odd", background=C("row_alt"))
        tree.tag_configure("even", background=C("card"))
        tree.tag_configure("danger", foreground=C("red"))
        tree.tag_configure("warn", foreground=C("yellow"))
        tree.tag_configure("ok", foreground=C("green"))
        tree.tag_configure("muted", foreground=C("muted"))
        return tree

    @staticmethod
    def fill_table(tree, rows, empty_message="Nothing to show yet."):
        tree.delete(*tree.get_children())
        if not rows:
            blanks = [""] * (len(tree["columns"]) - 1)
            tree.insert("", "end", values=[empty_message] + blanks, tags=("even",))
            return
        for index, (values, tag) in enumerate(rows):
            tags = ["odd" if index % 2 else "even"]
            if tag:
                tags.append(tag)
            tree.insert("", "end", values=values, tags=tuple(tags))

    # -- dashboard ----------------------------------------------------------

    def page_dashboard(self):
        body = self.scroll_area()
        metrics = dashboard_metrics()

        tk.Label(
            body,
            text=f"Good to see you, {self.user['username']}.",
            font=F("title", True), fg=C("heading"), bg=C("bg"),
        ).pack(anchor="w")
        tk.Label(
            body, text=now_local().strftime("%A, %d %B %Y"), font=F("label"),
            fg=C("muted"), bg=C("bg"),
        ).pack(anchor="w", pady=(s(4), s(20)))

        # Primary operational metrics.
        tiles = tk.Frame(body, bg=C("bg"))
        tiles.pack(fill="x", pady=(0, s(14)))
        self.stat_tile(tiles, metrics["on_hand"], "units", "Total stocks in hand", C("blue"))
        self.stat_tile(tiles, metrics["on_loan"], "units", "Currently on loan", C("purple"))
        self.stat_tile(tiles, metrics["low_stock"], "items", "Low on stock", C("yellow"))
        self.stat_tile(tiles, metrics["overdue"], "loans", "Overdue", C("red"), last=True)

        # Additional management metrics.
        more_tiles = tk.Frame(body, bg=C("bg"))
        more_tiles.pack(fill="x", pady=(0, s(16)))
        self.stat_tile(more_tiles, metrics["items"], "types", "Inventory item types", C("green"))
        self.stat_tile(more_tiles, metrics["categories"], "groups", "Categories", C("blue"))
        self.stat_tile(
            more_tiles, metrics["pending_borrows"] + metrics["pending_returns"], "requests",
            "Waiting for approval", C("orange"),
        )
        self.stat_tile(
            more_tiles, metrics["returned_records"], "records", "Returned records",
            C("green"), last=True,
        )

        columns = tk.Frame(body, bg=C("bg"))
        columns.pack(fill="both", expand=True)

        # Stock health.
        left = self.panel(columns, "Stock health", side="left")
        chart_row = tk.Frame(left, bg=C("card"))
        chart_row.pack(fill="x")
        healthy_pct = round(metrics["healthy"] / (metrics["items"] or 1) * 100)
        Donut(
            chart_row,
            [
                (metrics["healthy"], C("green")),
                (metrics["low_stock"], C("yellow")),
                (metrics["out_of_stock"], C("red")),
            ],
            f"{healthy_pct}%",
            "well stocked",
        ).pack(side="left")

        legend = tk.Frame(chart_row, bg=C("card"))
        legend.pack(side="left", padx=s(18), anchor="center")
        for label, value, color in (
            ("Well stocked", metrics["healthy"], C("green")),
            ("Low stock", metrics["low_stock"], C("yellow")),
            ("Out of stock", metrics["out_of_stock"], C("red")),
        ):
            row = tk.Frame(legend, bg=C("card"))
            row.pack(fill="x", pady=s(5))
            dot = tk.Canvas(row, width=s(9), height=s(9), bg=C("card"), highlightthickness=0, bd=0)
            dot.pack(side="left", pady=(s(4), 0))
            dot.create_oval(0, 0, s(9), s(9), fill=color, outline=color)
            tk.Label(
                row, text=f"  {label}", font=F("label"), fg=C("muted"), bg=C("card")
            ).pack(side="left")
            tk.Label(
                row, text=str(value), font=F("label", True), fg=C("text"), bg=C("card")
            ).pack(side="right", padx=s(10))

        tk.Frame(left, bg=C("border"), height=1).pack(fill="x", pady=s(16))
        for label, value in (
            ("Item groups", metrics["categories"]),
            ("All item types", metrics["items"]),
            ("Students holding items", metrics["borrowers"]),
            ("Stock value", f"₱{metrics['value']:,.2f}"),
        ):
            row = tk.Frame(left, bg=C("card"))
            row.pack(fill="x", pady=s(5))
            tk.Label(row, text=label, font=F("label"), fg=C("muted"), bg=C("card")).pack(side="left")
            tk.Label(
                row, text=str(value), font=F("label", True), fg=C("text"), bg=C("card")
            ).pack(side="right")

        # Borrowing snapshot.
        middle = self.panel(columns, "Borrowing snapshot", side="left")
        snapshot = (
            ("Total loan records", metrics["total_loans"]),
            ("Currently borrowed records", metrics["borrowed_records"]),
            ("Returned records", metrics["returned_records"]),
            ("Units currently on loan", metrics["on_loan"]),
            ("Borrow requests pending", metrics["pending_borrows"]),
            ("Return requests pending", metrics["pending_returns"]),
            ("Overdue loan records", metrics["overdue"]),
            ("Overdue units", metrics["overdue_units"]),
            ("Active borrowers", metrics["borrowers"]),
        )
        for label, value in snapshot:
            row = tk.Frame(middle, bg=C("card"))
            row.pack(fill="x", pady=s(5))
            tk.Label(row, text=label, font=F("label"), fg=C("muted"), bg=C("card")).pack(side="left")
            value_color = C("red") if "Overdue" in label and value else C("text")
            tk.Label(
                row, text=str(value), font=F("label", True),
                fg=value_color, bg=C("card"),
            ).pack(side="right")

        tk.Frame(middle, bg=C("border"), height=1).pack(fill="x", pady=s(12))
        tk.Label(
            middle,
            text=f"User accounts: {metrics['active_users']} active / {metrics['users']} total",
            font=F("micro"), fg=C("muted"), bg=C("card"),
        ).pack(anchor="w")

        # Most borrowed items.
        right = self.panel(columns, "Most borrowed items", side="left", last=True)
        if metrics["top_items"]:
            top = max(row["total"] for row in metrics["top_items"]) or 1
            for row in metrics["top_items"]:
                line = tk.Frame(right, bg=C("card"))
                line.pack(fill="x", pady=s(8))
                head = tk.Frame(line, bg=C("card"))
                head.pack(fill="x")
                tk.Label(
                    head, text=row["item_name"], font=F("label"), fg=C("text"), bg=C("card")
                ).pack(side="left")
                tk.Label(
                    head, text=str(row["total"]), font=F("label", True),
                    fg=C("heading"), bg=C("card")
                ).pack(side="right")
                track = tk.Frame(line, bg=C("track"), height=s(7))
                track.pack(fill="x", pady=(s(6), 0))
                track.pack_propagate(False)
                tk.Frame(track, bg=C("accent")).place(
                    relwidth=max(0.04, row["total"] / top), relheight=1
                )
        else:
            tk.Label(
                right,
                text="No loans recorded yet. Use New borrow to check the first item out.",
                font=F("label"), fg=C("muted"), bg=C("card"),
                wraplength=s(280), justify="left",
            ).pack(anchor="w")

        # Category-level inventory breakdown.
        category_panel = self.panel(
            body, "Inventory by category",
            action="Open inventory", action_command=lambda: self.show_page("Inventory"),
        )
        category_tree = self.build_table(
            category_panel,
            ("Category", "Item types", "Units", "Stock value"),
            (220, 110, 100, 150),
            height=8,
            anchors=("w", "center", "center", "e"),
        )
        category_rows = [
            (
                (row["category"], row["item_types"], row["units"], f"₱{row['stock_value']:,.2f}"),
                None,
            )
            for row in metrics["categories_detail"]
        ]
        self.fill_table(category_tree, category_rows, "No inventory categories yet.")

        recent = self.panel(
            body, "Recent borrow activity", action="Open borrow log",
            action_command=lambda: self.show_page("Borrow log"),
        )
        tree = self.build_table(
            recent,
            ("Item", "Student", "School ID", "Qty", "Borrowed", "Due", "Status"),
            (220, 160, 120, 60, 140, 100, 150),
            height=8,
        )
        rows = [
            (
                (
                    entry["item_name"], entry["borrower_name"], entry["school_id"],
                    entry["quantity"], (entry["borrow_date"] or "")[:16],
                    entry["due_date"] or "—", "● " + entry["status_label"],
                ),
                entry["status_tag"],
            )
            for entry in metrics["recent"]
        ]
        self.fill_table(tree, rows, "No borrow activity yet.")

    # -- inventory ----------------------------------------------------------

    def page_inventory(self):
        body = self.scroll_area()
        actions = self.page_header(
            body, "Inventory", f"Items at or below {LOW_STOCK_THRESHOLD} units are flagged low."
        )
        RoundButton(
            actions, "Refresh", command=self.load_inventory, outline_only=True,
            width=92, height=38,
        ).pack(side="left", padx=s(8))
        RoundButton(
            actions, "Export CSV", command=self.export_inventory, outline_only=True,
            width=112, height=38,
        ).pack(side="left", padx=s(8))
        if self.is_admin:
            RoundButton(actions, "Add item", command=self.add_item, width=104, height=38).pack(
                side="left"
            )

        search_row = tk.Frame(body, bg=C("bg"))
        search_row.pack(fill="x", pady=(0, s(14)))
        self.inventory_search = RoundedInput(search_row, placeholder="Search items or categories")
        self.inventory_search.pack(side="left", fill="x", expand=True)
        self.inventory_search.entry.bind("<KeyRelease>", lambda e: self.load_inventory())

        panel = self.panel(body, "All items")
        self.inventory_tree = self.build_table(
            panel,
            ("ID", "Item name", "Category", "Stock", "Unit price", "Status"),
            (50, 260, 150, 80, 110, 120),
            height=14,
            anchors=("center", "w", "w", "center", "e", "center"),
        )
        self.inventory_tree.bind("<Double-1>", lambda e: self.edit_item())

        row = tk.Frame(panel, bg=C("card"))
        row.pack(fill="x", pady=(s(16), 0))
        RoundButton(row, "Borrow selected", command=self.borrow_selected, width=150, height=38).pack(
            side="left"
        )
        if self.is_admin:
            RoundButton(
                row, "Edit", command=self.edit_item, outline_only=True, width=90, height=38
            ).pack(side="left", padx=s(8))
            RoundButton(
                row, "Delete", command=self.delete_item, bg=C("red"), width=90, height=38
            ).pack(side="left")

        self.load_inventory()

    def load_inventory(self):
        rows = []
        for item in list_inventory(self.inventory_search.get()):
            if item["stock"] <= 0:
                tag = "danger"
            elif item["stock"] <= LOW_STOCK_THRESHOLD:
                tag = "warn"
            else:
                tag = None
            rows.append(
                (
                    (
                        item["item_id"], item["item_name"], item["category"], item["stock"],
                        f"₱{item['unit_price']:,.2f}", "● " + item["in_stock"],
                    ),
                    tag,
                )
            )
        self.fill_table(self.inventory_tree, rows, "No items match that search.")

    def selected_item(self) -> dict | None:
        selection = self.inventory_tree.selection()
        if not selection:
            show_toast(self.root, "Select an item in the table first.", "warning")
            return None
        item_id = self.inventory_tree.item(selection[0], "values")[0]
        if not str(item_id).isdigit():
            return None
        return get_item(int(item_id))

    def add_item(self):
        dialog = ItemDialog(self.content, self.user["username"])
        self.root.wait_window(dialog)
        if dialog.result:
            self.load_inventory()
            show_toast(self.root, dialog.result, "success")

    def edit_item(self):
        if not self.is_admin:
            show_toast(self.root, "Only administrators can edit items.", "warning")
            return
        item = self.selected_item()
        if not item:
            return
        dialog = ItemDialog(self.content, self.user["username"], item)
        self.root.wait_window(dialog)
        if dialog.result:
            self.load_inventory()
            show_toast(self.root, dialog.result, "success")

    def delete_item(self):
        item = self.selected_item()
        if not item:
            return
        if not messagebox.askyesno("Delete item", f"Delete {item['item_name']} from the inventory?"):
            return
        ok, message = delete_item(item["item_id"], self.user["username"])
        if not ok:
            messagebox.showerror("Delete item", message)
            return
        self.load_inventory()
        show_toast(self.root, message, "success")

    def borrow_selected(self):
        item = self.selected_item()
        if item:
            self.new_borrow(preset_item_id=item["item_id"])

    def export_inventory(self):
        path = filedialog.asksaveasfilename(
            defaultextension=".csv",
            filetypes=[("CSV file", "*.csv")],
            initialfile=f"inventory_{now_local():%Y%m%d}.csv",
        )
        if not path:
            return
        with open(path, "w", newline="", encoding="utf-8") as handle:
            handle.write(inventory_csv())
        write_system_log(
            self.user["username"], "EXPORT", f"Inventory exported to {os.path.basename(path)}"
        )
        show_toast(self.root, "Inventory exported.", "success")

    # -- borrow log ---------------------------------------------------------

    def page_borrow_log(self):
        body = self.scroll_area()
        actions = self.page_header(
            body, "Borrow log", "Every loan is recorded against the student's school ID."
        )
        RoundButton(
            actions, "Export CSV", command=self.export_borrow_log, outline_only=True,
            width=112, height=38,
        ).pack(side="left", padx=s(8))
        RoundButton(actions, "New borrow", command=self.new_borrow, width=120, height=38).pack(
            side="left"
        )

        filters = tk.Frame(body, bg=C("bg"))
        filters.pack(fill="x", pady=(0, s(14)))
        self.log_search = RoundedInput(filters, placeholder="Search item, student name or school ID")
        self.log_search.pack(side="left", fill="x", expand=True)
        self.log_search.entry.bind("<KeyRelease>", lambda e: self.load_borrow_log())

        self.log_filter = tk.StringVar(value="All")
        ttk.Combobox(
            filters, textvariable=self.log_filter,
            values=LOG_FILTERS, state="readonly",
            width=14, style="Modern.TCombobox", font=F("label"),
        ).pack(side="left", padx=(s(12), 0))
        self.log_filter.trace_add("write", lambda *_: self.load_borrow_log())

        panel = self.panel(body, "Loan records")
        self.log_tree = self.build_table(
            panel,
            ("ID", "Item", "Student", "School ID", "Qty", "Borrowed", "Due", "Returned", "Status", "By"),
            (50, 190, 150, 110, 55, 130, 95, 130, 150, 90),
            height=14,
            anchors=("center", "w", "w", "w", "center", "w", "center", "w", "w", "w"),
        )

        row = tk.Frame(panel, bg=C("card"))
        row.pack(fill="x", pady=(s(16), 0))
        RoundButton(
            row, "Mark as returned", command=self.return_selected, bg=C("green"),
            width=160, height=38,
        ).pack(side="left")
        RoundButton(
            row, "View student history", command=self.view_selected_student, outline_only=True,
            width=172, height=38,
        ).pack(side="left", padx=s(8))
        if self.is_admin:
            # Web users send borrow requests; administrators can approve them here too.
            RoundButton(
                row, "Approve request", command=lambda: self.review_loan(True),
                width=150, height=38,
            ).pack(side="left", padx=(0, s(8)))
            RoundButton(
                row, "Reject request", command=lambda: self.review_loan(False), bg=C("red"),
                width=140, height=38,
            ).pack(side="left")

        self.load_borrow_log()

    def load_borrow_log(self):
        rows = [
            (
                (
                    entry["log_id"], entry["item_name"], entry["borrower_name"],
                    entry["school_id"] or "—", entry["quantity"],
                    (entry["borrow_date"] or "")[:16], entry["due_date"] or "—",
                    (entry["return_date"] or "—")[:16], "● " + entry["status_label"],
                    entry["processed_by"] or entry["requested_by"] or "—",
                ),
                entry["status_tag"],
            )
            for entry in list_borrow_log(self.log_search.get(), self.log_filter.get())
        ]
        self.fill_table(self.log_tree, rows, "No loans match that filter.")

    def selected_log(self):
        selection = self.log_tree.selection()
        if not selection:
            show_toast(self.root, "Select a loan record first.", "warning")
            return None
        values = self.log_tree.item(selection[0], "values")
        return values if str(values[0]).isdigit() else None

    def return_selected(self):
        values = self.selected_log()
        if not values:
            return
        ok, message = record_return(int(values[0]), self.user["username"])
        if not ok:
            messagebox.showerror("Mark as returned", message)
            return
        self.load_borrow_log()
        show_toast(self.root, message, "success")

    def review_loan(self, approve: bool):
        """Approve/reject a pending borrow request or a pending return request."""
        values = self.selected_log()
        if not values:
            return
        log_id = int(values[0])
        status = str(values[8])
        if "Pending approval" in status:
            ok, message = (approve_borrow if approve else reject_borrow)(log_id, self.user["username"])
        elif "Return pending" in status:
            ok, message = (record_return if approve else reject_return)(log_id, self.user["username"])
        else:
            ok, message = False, "Select a record that is waiting for approval."
        if not ok:
            messagebox.showerror("Review request", message)
            return
        self.load_borrow_log()
        show_toast(self.root, message, "success" if approve else "info")

    def view_selected_student(self):
        values = self.selected_log()
        if not values:
            return
        school_id = values[3]
        self.show_page("Students")
        self.student_search.set(school_id)
        self.load_students()

    def new_borrow(self, preset_item_id: int | None = None):
        dialog = BorrowDialog(self.content, self.user["username"], preset_item_id)
        self.root.wait_window(dialog)
        if dialog.result:
            show_toast(self.root, dialog.result, "success")
            if self.current_page == "Borrow log":
                self.load_borrow_log()
            elif self.current_page == "Inventory":
                self.load_inventory()
            else:
                self.show_page(self.current_page)

    def export_borrow_log(self):
        path = filedialog.asksaveasfilename(
            defaultextension=".csv",
            filetypes=[("CSV file", "*.csv")],
            initialfile=f"borrow_log_{now_local():%Y%m%d}.csv",
        )
        if not path:
            return
        with open(path, "w", newline="", encoding="utf-8") as handle:
            handle.write(borrow_log_csv())
        write_system_log(
            self.user["username"], "EXPORT", f"Borrow log exported to {os.path.basename(path)}"
        )
        show_toast(self.root, "Borrow log exported.", "success")

    # -- students -----------------------------------------------------------

    def page_students(self):
        body = self.scroll_area()
        self.page_header(body, "Students", "Borrowing history grouped by school ID.")

        search_row = tk.Frame(body, bg=C("bg"))
        search_row.pack(fill="x", pady=(0, s(14)))
        self.student_search = RoundedInput(search_row, placeholder="Search by school ID or name")
        self.student_search.pack(side="left", fill="x", expand=True)
        self.student_search.entry.bind("<KeyRelease>", lambda e: self.load_students())

        panel = self.panel(body, "Borrower directory")
        self.student_tree = self.build_table(
            panel,
            ("School ID", "Student", "Total loans", "Still out", "Overdue", "Last borrowed"),
            (130, 200, 100, 90, 90, 150),
            height=15,
            anchors=("w", "w", "center", "center", "center", "w"),
        )
        self.load_students()

    def load_students(self):
        table_rows = []
        for row in list_students(self.student_search.get()):
            if row["overdue"]:
                tag = "danger"
            elif row["still_out"]:
                tag = "warn"
            else:
                tag = None
            table_rows.append(
                (
                    (
                        row["school_id"], row["borrower_name"], row["total"], row["still_out"],
                        row["overdue"], (row["last_borrow"] or "")[:16],
                    ),
                    tag,
                )
            )
        self.fill_table(self.student_tree, table_rows, "No student has borrowed anything yet.")

    # -- admin pages --------------------------------------------------------

    def page_users(self):
        body = self.scroll_area()
        self.page_header(body, "Users", "Unlock accounts and review password reset requests.")

        accounts = self.panel(body, "Accounts")
        self.user_tree = self.build_table(
            accounts,
            ("ID", "Username", "Email", "Role", "Failed", "Locked", "Active"),
            (50, 150, 240, 90, 70, 80, 80),
            height=8,
            anchors=("center", "w", "w", "center", "center", "center", "center"),
        )
        row = tk.Frame(accounts, bg=C("card"))
        row.pack(fill="x", pady=(s(16), 0))
        RoundButton(row, "Unlock account", command=self.unlock_user, width=142, height=38).pack(
            side="left"
        )
        RoundButton(
            row, "Toggle active", command=self.toggle_user_active, outline_only=True,
            width=130, height=38,
        ).pack(side="left", padx=s(8))

        requests = self.panel(body, "Password reset requests")
        self.request_tree = self.build_table(
            requests,
            ("ID", "Username", "Email", "Requested", "Status", "Reviewed by"),
            (50, 150, 240, 150, 100, 130),
            height=7,
            anchors=("center", "w", "w", "w", "center", "w"),
        )
        row = tk.Frame(requests, bg=C("card"))
        row.pack(fill="x", pady=(s(16), 0))
        RoundButton(
            row, "Approve", command=lambda: self.review_request(True), bg=C("green"),
            width=110, height=38,
        ).pack(side="left")
        RoundButton(
            row, "Reject", command=lambda: self.review_request(False), bg=C("red"),
            width=110, height=38,
        ).pack(side="left", padx=s(8))

        self.load_users()
        self.load_requests()

    def load_users(self):
        rows = [
            (
                (
                    user["id"], user["username"], user["email"], user["role"].title(),
                    user["failed_attempts"], "Yes" if user["is_locked"] else "No",
                    "Yes" if user["is_active"] else "No",
                ),
                "danger" if user["is_locked"] or not user["is_active"] else None,
            )
            for user in list_users()
        ]
        self.fill_table(self.user_tree, rows)

    def load_requests(self):
        rows = []
        for request in list_reset_requests():
            tag = {"PENDING": "warn", "APPROVED": "ok"}.get(request["status"], "danger")
            rows.append(
                (
                    (
                        request["request_id"], request["username"], request["email"],
                        request["requested_at"][:16], request["status"].title(),
                        request["reviewed_by"] or "—",
                    ),
                    tag,
                )
            )
        self.fill_table(self.request_tree, rows, "No reset requests.")

    def _selected_user_id(self):
        selection = self.user_tree.selection()
        if not selection:
            show_toast(self.root, "Select an account first.", "warning")
            return None
        values = self.user_tree.item(selection[0], "values")
        return int(values[0]) if str(values[0]).isdigit() else None

    def unlock_user(self):
        user_id = self._selected_user_id()
        if user_id is None:
            return
        ok, message = unlock_user(user_id, self.user["username"])
        self.load_users()
        show_toast(self.root, message, "success" if ok else "error")

    def toggle_user_active(self):
        user_id = self._selected_user_id()
        if user_id is None:
            return
        ok, message = toggle_user_active(user_id, self.user["username"], self.user["id"])
        if not ok:
            messagebox.showerror("Users", message)
            return
        self.load_users()

    def review_request(self, approve: bool):
        selection = self.request_tree.selection()
        if not selection:
            show_toast(self.root, "Select a request first.", "warning")
            return
        request_id = self.request_tree.item(selection[0], "values")[0]
        if not str(request_id).isdigit():
            return
        ok, message = review_reset_request(int(request_id), approve, self.user["username"])
        if not ok:
            show_toast(self.root, message, "warning")
            return
        self.load_requests()
        self.load_users()
        show_toast(self.root, message, "success" if approve else "info")

    def page_activity(self):
        body = self.scroll_area()
        self.page_header(body, "Activity", "The 300 most recent system events.")

        panel = self.panel(body, "System log")
        tree = self.build_table(
            panel, ("Time", "User", "Action", "Details"), (150, 130, 150, 420), height=17
        )
        self.fill_table(
            tree,
            [
                ((log["timestamp"], log["username"] or "SYSTEM", log["action"], log["details"] or ""), None)
                for log in list_activity(300)
            ],
            "No activity recorded yet.",
        )

    def page_settings(self):
        body = self.scroll_area()
        self.page_header(body, "Settings", "Applies to every user of this installation.")

        panel = self.panel(body, "Stock alerts")
        tk.Label(
            panel, text="Low stock threshold", font=F("label", True), fg=C("muted"), bg=C("card")
        ).pack(anchor="w", pady=(0, s(5)))
        self.threshold_input = RoundedInput(panel)
        self.threshold_input.pack(fill="x", pady=(0, s(5)))
        self.threshold_input.set(str(LOW_STOCK_THRESHOLD))
        tk.Label(
            panel,
            text="Items at or below this quantity are flagged Low Stock on the dashboard.",
            font=F("micro"), fg=C("muted"), bg=C("card"),
        ).pack(anchor="w", pady=(0, s(18)))
        RoundButton(panel, "Save threshold", command=self.save_threshold, width=160, height=40).pack(
            anchor="w"
        )

    def save_threshold(self):
        ok, message = save_low_stock_threshold(self.threshold_input.get(), self.user["username"])
        if not ok:
            messagebox.showerror("Settings", message)
            return
        show_toast(self.root, message, "success")

    def sign_out(self):
        if not messagebox.askyesno("Sign out", "Sign out of this session?"):
            return
        write_system_log(self.user["username"], "LOGOUT", "Signed out")
        self.app.open_login()


# ---------------------------------------------------------------------------
# App controller (desktop)
# ---------------------------------------------------------------------------


class InventoryApp:
    """Owns the window and swaps between the auth screen and the dashboard."""

    def __init__(self, root):
        self.root = root
        self.user: dict | None = None
        self.screen = None

        enable_hidpi(root)
        resolve_font_family(root)
        self.root.geometry(f"{s(1280)}x{s(780)}")
        self.root.minsize(s(1100), s(700))
        apply_ttk_styles(root)
        self.open_login()

    def _clear(self):
        self.root.unbind_all("<MouseWheel>")
        for widget in self.root.winfo_children():
            widget.destroy()

    def open_login(self):
        self.user = None
        self._clear()
        self.root.configure(bg=C("bg"))
        self.screen = AuthenticationWindow(self)

    def open_dashboard(self, user: dict):
        self.user = user
        self._clear()
        self.root.configure(bg=C("bg"))
        self.screen = DashboardWindow(self, user)

    def toggle_theme(self):
        STATE["theme"] = "dark" if STATE["theme"] == "light" else "light"
        set_setting("theme", STATE["theme"])
        apply_ttk_styles(self.root)
        if self.user:
            self.open_dashboard(self.user)
        else:
            self.open_login()


def run_desktop() -> None:
    if not TKINTER_AVAILABLE:
        print("Tkinter is not available on this computer. Run the web version instead: python app.py")
        return
    initialize_database()
    load_runtime_settings()
    root = tk.Tk()
    InventoryApp(root)
    root.mainloop()


if __name__ == "__main__":
    run_desktop()
