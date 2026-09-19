# Campus Hardware Inventory System — Flask Web Application

Software Design Experiment No. 8 — **GitHub → Supabase PostgreSQL → Local Test → Render**

| Service  | Role |
|----------|------|
| GitHub   | Stores the source code. Render pulls the code from here. |
| Supabase | PostgreSQL database — the permanent home of the data. |
| Render   | Runs the Flask app (`gunicorn app:app`) and gives it a public URL. |

## Project structure

```
LaboratorySystem/
├── Laboratorysystem.py              # ALL original logic: database layer, validation, controllers, Tkinter desktop UI
├── app.py                           # Flask web bridge: routes -> controllers -> templates
├── migrate_sqlite_to_postgresql.py  # One-time copy of the SQLite data into Supabase
├── requirements.txt
├── .python-version                  # Tells Render to use Python 3.12
├── .gitignore
├── templates/  base.html, dashboard.html, login.html, register.html, reset.html
└── static/     style.css
```

`models/hardware_inventory.db` and `app_logging/` are created at run time and are **not** uploaded (see `.gitignore`).

## Default account

| Username | Password    | Role  |
|----------|-------------|-------|
| `admin`  | `Admin@123` | ADMIN |

Change it after the first sign-in (Reset tab → an admin approves the request).

## Tables (Part D note: "depends on the tables you have created")

| Table | Purpose |
|-------|---------|
| `users` | Accounts, bcrypt password hash, role, failed attempts, locked/active flags |
| `inventory` | Hardware items, category, stock, unit price, stock status |
| `borrow_log` | Borrow requests, loans and returns (status: `PENDING_BORROW`, `BORROWED`, `RETURN_PENDING`, `RETURNED`, `REJECTED`) |
| `password_reset_requests` | Reset/unlock requests reviewed by an admin |
| `system_logs` | Audit trail (who did what, when) |
| `app_settings` | Low-stock threshold and theme |

## Web workflow

1. A **USER** submits a borrow request (item, quantity, student name, school ID, days). Stock is **not** deducted yet.
2. An **ADMIN** opens **Approvals** and approves (stock is deducted) or rejects it.
3. The USER clicks **Request return** on the loan.
4. The ADMIN approves the return (stock is restored) or rejects it (the item stays on loan).

Admins can also do a direct staff checkout from **Borrow log** (tick "Check out immediately").
Public registration always creates a USER; an admin can promote an account on the **Users** page.

## Commands (Windows CMD)

```bat
:: Install packages
python -m pip install -r requirements.txt

:: Set the Supabase connection (this CMD window only)
set "DATABASE_URL=YOUR_SUPABASE_SESSION_POOLER_URI"

:: Verify only the beginning (never print the whole value)
python -c "import os; print(os.getenv('DATABASE_URL','')[:13])"

:: Copy SQLite -> Supabase (run once)
python migrate_sqlite_to_postgresql.py

:: Run the web app locally -> http://127.0.0.1:5000
python app.py

:: The desktop version still works (uses Supabase too when DATABASE_URL is set)
python Laboratorysystem.py
```

Render settings: Build `pip install -r requirements.txt` · Start `gunicorn app:app` ·
Environment `DATABASE_URL`, `SECRET_KEY`.
