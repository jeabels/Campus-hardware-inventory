"""
app.py - Flask web bridge for the Campus Hardware Inventory System (Lab 8)

The browser talks to these routes. The routes do NOT contain business logic;
they read the form, call the existing controllers in Laboratorysystem.py,
and render an HTML template with the result.

Local run : python app.py              -> http://127.0.0.1:5000
Render    : gunicorn app:app

Environment variables
    DATABASE_URL  Supabase PostgreSQL URI. Not set -> local SQLite file is used.
    SECRET_KEY    Long random value used by Flask to sign the session cookie.
"""

from __future__ import annotations

import os
import secrets
import time
from datetime import timedelta
from functools import wraps

from flask import (
    Flask, Response, abort, flash, g, redirect, render_template, request, session, url_for,
)
from pydantic import ValidationError
from werkzeug.middleware.proxy_fix import ProxyFix

import Laboratorysystem as core

# ---------------------------------------------------------------------------
# Flask setup
# ---------------------------------------------------------------------------

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)  # Render runs behind a proxy

SECRET_KEY = os.getenv("SECRET_KEY")
if not SECRET_KEY:
    # Fine for local testing; on Render always set SECRET_KEY so logins survive restarts.
    SECRET_KEY = secrets.token_hex(32)
    app.logger.warning("SECRET_KEY is not set - using a temporary key for this run only.")

app.config.update(
    SECRET_KEY=SECRET_KEY,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=bool(os.getenv("RENDER")),  # HTTPS-only cookie on Render
    PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
    MAX_CONTENT_LENGTH=1 * 1024 * 1024,
)

# Create tables (if missing) and load settings when the server starts.
core.initialize_database()
core.load_runtime_settings()

_settings_loaded_at = time.monotonic()


# ---------------------------------------------------------------------------
# Helpers: current user, login/admin guards, CSRF protection
# ---------------------------------------------------------------------------

def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if g.user is None:
            flash("Please sign in first.", "warning")
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapper


def admin_required(view):
    @wraps(view)
    @login_required
    def wrapper(*args, **kwargs):
        if g.user["role"] != "ADMIN":
            flash("Only administrators can do that.", "error")
            return redirect(url_for("dashboard"))
        return view(*args, **kwargs)
    return wrapper


def csrf_token() -> str:
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_urlsafe(32)
    return session["_csrf"]


@app.before_request
def load_user_and_check_csrf():
    global _settings_loaded_at
    # Several Render workers may exist; re-read shared settings every 30 seconds.
    if time.monotonic() - _settings_loaded_at > 30:
        core.load_runtime_settings()
        _settings_loaded_at = time.monotonic()

    g.user = None
    user_id = session.get("user_id")
    if user_id is not None:
        g.user = core.get_user(user_id)  # disabled/locked accounts are signed out
        if g.user is None:
            session.clear()

    if request.method == "POST":
        sent = request.form.get("csrf_token", "")
        if not sent or not secrets.compare_digest(sent, session.get("_csrf", "")):
            abort(400, "The form expired. Go back, refresh the page and try again.")


@app.context_processor
def template_globals():
    return {
        "csrf_token": csrf_token,
        "current_user": g.get("user"),
        "is_admin": bool(g.get("user")) and g.user["role"] == "ADMIN",
        "database_label": core.database_label(),
        "low_stock_threshold": core.LOW_STOCK_THRESHOLD,
        "today": core.today_str(),
    }


@app.template_filter("peso")
def peso(value) -> str:
    return f"₱{float(value or 0):,.2f}"


def form_int(name: str, default: int | None = None) -> int | None:
    raw = (request.form.get(name) or "").strip()
    if raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return None


def flash_result(result: tuple, success_kind: str = "success") -> bool:
    ok, message = result[0], result[1]
    flash(message, success_kind if ok else "error")
    return ok


def back(default_tab: str = "overview", **params):
    """Return to the dashboard tab the form came from."""
    tab = request.form.get("return_tab") or default_tab
    return redirect(url_for("dashboard", tab=tab, **params))


# ---------------------------------------------------------------------------
# Authentication pages
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return redirect(url_for("dashboard" if g.user else "login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if g.user:
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        ok, message, user = core.authenticate_user(
            request.form.get("username", ""), request.form.get("password", "")
        )
        if ok:
            session.clear()
            session.permanent = True
            session["user_id"] = user["id"]
            flash(message, "success")
            target = request.args.get("next", "")
            if not target.startswith("/") or target.startswith("//"):
                target = url_for("dashboard")
            return redirect(target)
        flash(message, "error")
    return render_template("login.html", auth_page=True, username=request.form.get("username", ""))


@app.route("/register", methods=["GET", "POST"])
def register():
    if g.user:
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        # Public sign-ups are always USER accounts. An ADMIN can promote them later.
        ok, message = core.register_user(
            request.form.get("username", ""),
            request.form.get("email", ""),
            request.form.get("password", ""),
            "USER",
        )
        flash(message, "success" if ok else "error")
        if ok:
            return redirect(url_for("login"))
    return render_template("register.html", auth_page=True, form=request.form)


@app.route("/reset", methods=["GET", "POST"])
def reset():
    if request.method == "POST":
        ok, message = core.submit_password_reset(
            request.form.get("username", ""),
            request.form.get("email", ""),
            request.form.get("password", ""),
            request.form.get("confirm", ""),
        )
        flash(message, "info" if ok else "error")
        if ok:
            return redirect(url_for("login"))
    return render_template("reset.html", auth_page=True, form=request.form)


@app.route("/logout", methods=["POST"])
def logout():
    if g.user:
        core.write_system_log(g.user["username"], "LOGOUT", "Signed out (web)")
    session.clear()
    flash("You have been signed out.", "info")
    return redirect(url_for("login"))


# ---------------------------------------------------------------------------
# Dashboard (one template, several tabs)
# ---------------------------------------------------------------------------

USER_TABS = ("overview", "inventory", "borrow")
ADMIN_TABS = ("overview", "inventory", "borrow", "approvals", "students", "users", "activity", "settings")


@app.route("/dashboard")
@login_required
def dashboard():
    is_admin = g.user["role"] == "ADMIN"
    tabs = ADMIN_TABS if is_admin else USER_TABS
    tab = request.args.get("tab", "overview")
    if tab not in tabs:
        tab = "overview"

    context = {"tab": tab, "tabs": tabs, "categories": core.CATEGORIES,
               "log_filters": core.LOG_FILTERS, "q": request.args.get("q", "").strip()}
    metrics = core.dashboard_metrics()
    context["metrics"] = metrics

    if tab == "overview":
        if not is_admin:
            context["my_loans"] = core.list_borrow_log(requested_by=g.user["username"])[:8]
    elif tab == "inventory":
        context["items"] = core.list_inventory(context["q"])
        edit_id = request.args.get("edit", type=int)
        context["edit_item"] = core.get_item(edit_id) if (is_admin and edit_id) else None
    elif tab == "borrow":
        status = request.args.get("status", "All")
        context["status"] = status if status in core.LOG_FILTERS else "All"
        context["borrowable"] = core.list_borrowable_items()
        context["preset_item"] = request.args.get("item", type=int)
        context["loans"] = core.list_borrow_log(
            context["q"], context["status"], None if is_admin else g.user["username"]
        )
    elif tab == "approvals":
        context["pending"] = core.list_pending_requests()
    elif tab == "students":
        context["students"] = core.list_students(context["q"])
        school_id = request.args.get("school_id", "").strip()
        context["history_id"] = school_id
        context["history"] = core.student_history(school_id) if school_id else []
        for entry in context["history"]:
            entry["status_label"], entry["status_tag"] = core.loan_status(entry)
    elif tab == "users":
        context["users"] = core.list_users()
        context["resets"] = core.list_reset_requests()
    elif tab == "activity":
        context["logs"] = core.list_activity(300)

    return render_template("dashboard.html", **context)


# ---------------------------------------------------------------------------
# Inventory actions (ADMIN)
# ---------------------------------------------------------------------------

def _item_from_form():
    stock = form_int("stock", 0)
    try:
        price = float((request.form.get("unit_price") or "0").strip())
    except ValueError:
        price = None
    if stock is None or price is None:
        return None, "Stock and unit price must be numbers."
    try:
        return core.ItemSchema(
            item_name=request.form.get("item_name", ""),
            category=request.form.get("category", ""),
            stock=stock,
            unit_price=price,
        ), ""
    except ValidationError as error:
        return None, core.first_error(error)


@app.route("/inventory/add", methods=["POST"])
@admin_required
def inventory_add():
    data, problem = _item_from_form()
    if data is None:
        flash(problem, "error")
        return back("inventory")
    existing = core.find_item_by_name(data.item_name)
    if existing and request.form.get("restock_existing"):
        flash_result(core.restock_item(existing["item_id"], data.stock, g.user["username"]))
    elif existing:
        flash(
            f'"{existing["item_name"]}" already exists (ID {existing["item_id"]}, '
            f'{existing["stock"]} unit(s)). Tick "add to existing stock" to restock it, or edit it.',
            "warning",
        )
    else:
        flash_result(core.create_item(data, g.user["username"]))
    return back("inventory")


@app.route("/inventory/<int:item_id>/edit", methods=["POST"])
@admin_required
def inventory_edit(item_id):
    data, problem = _item_from_form()
    if data is None:
        flash(problem, "error")
        return redirect(url_for("dashboard", tab="inventory", edit=item_id))
    if not flash_result(core.update_item(item_id, data, g.user["username"])):
        return redirect(url_for("dashboard", tab="inventory", edit=item_id))
    return back("inventory")


@app.route("/inventory/<int:item_id>/delete", methods=["POST"])
@admin_required
def inventory_delete(item_id):
    flash_result(core.delete_item(item_id, g.user["username"]))
    return back("inventory")


# ---------------------------------------------------------------------------
# Borrowing actions
# ---------------------------------------------------------------------------

@app.route("/borrow/request", methods=["POST"])
@login_required
def borrow_request():
    item_id = form_int("item_id")
    quantity = form_int("quantity")
    loan_days = form_int("loan_days", core.DEFAULT_LOAN_DAYS)
    if not item_id:
        flash("Choose an item to borrow.", "error")
        return back("borrow")
    if quantity is None or loan_days is None:
        flash("Quantity and loan days must be whole numbers.", "error")
        return back("borrow")
    try:
        data = core.BorrowSchema(
            borrower_name=request.form.get("borrower_name", ""),
            school_id=request.form.get("school_id", ""),
            quantity=quantity,
            loan_days=loan_days,
            notes=request.form.get("notes", ""),
        )
    except ValidationError as error:
        flash(core.first_error(error), "error")
        return back("borrow")

    if g.user["role"] == "ADMIN" and request.form.get("checkout_now"):
        flash_result(core.record_borrow(item_id, data, g.user["username"]))
    else:
        flash_result(core.request_borrow(item_id, data, g.user["username"]))
    return back("borrow")


@app.route("/borrow/<int:log_id>/<action>", methods=["POST"])
@login_required
def borrow_action(log_id, action):
    who = g.user["username"]
    is_admin = g.user["role"] == "ADMIN"
    if action == "return-request":
        flash_result(core.request_return(log_id, who, is_admin), "info")
        return back("borrow")

    admin_actions = {
        "approve": core.approve_borrow,
        "reject": core.reject_borrow,
        "return-approve": core.record_return,
        "return-reject": core.reject_return,
        "mark-returned": core.record_return,
    }
    if action not in admin_actions:
        abort(404)
    if not is_admin:
        flash("Only administrators can do that.", "error")
        return back("borrow")
    flash_result(admin_actions[action](log_id, who), "success")
    return back("approvals")


# ---------------------------------------------------------------------------
# Users, reset requests, settings (ADMIN)
# ---------------------------------------------------------------------------

@app.route("/users/<int:user_id>/<action>", methods=["POST"])
@admin_required
def user_action(user_id, action):
    who, me = g.user["username"], g.user["id"]
    if action == "unlock":
        flash_result(core.unlock_user(user_id, who))
    elif action == "toggle":
        flash_result(core.toggle_user_active(user_id, who, me))
    elif action == "role":
        flash_result(core.set_user_role(user_id, request.form.get("role", ""), who, me))
    else:
        abort(404)
    return back("users")


@app.route("/resets/<int:request_id>/<action>", methods=["POST"])
@admin_required
def reset_action(request_id, action):
    if action not in ("approve", "reject"):
        abort(404)
    flash_result(core.review_reset_request(request_id, action == "approve", g.user["username"]),
                 "success" if action == "approve" else "info")
    return back("users")


@app.route("/settings/threshold", methods=["POST"])
@admin_required
def settings_threshold():
    flash_result(core.save_low_stock_threshold(request.form.get("threshold", ""), g.user["username"]))
    return back("settings")


# ---------------------------------------------------------------------------
# CSV exports, health check, errors
# ---------------------------------------------------------------------------

def _csv_response(text: str, name: str, what: str) -> Response:
    core.write_system_log(g.user["username"], "EXPORT", f"{what} exported to {name} (web)")
    return Response(
        "﻿" + text,  # BOM so Excel opens the peso sign / accents correctly
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={name}"},
    )


@app.route("/export/inventory.csv")
@login_required
def export_inventory():
    return _csv_response(core.inventory_csv(), f"inventory_{core.now_local():%Y%m%d}.csv", "Inventory")


@app.route("/export/borrow_log.csv")
@admin_required
def export_borrow_log():
    return _csv_response(core.borrow_log_csv(), f"borrow_log_{core.now_local():%Y%m%d}.csv", "Borrow log")


@app.route("/healthz")
def healthz():
    """Used to confirm that Render can reach Supabase. Shows no secrets."""
    try:
        with core.connect_db() as connection:
            connection.execute("SELECT 1 AS ok").fetchone()
        return {"status": "ok", "database": core.database_label()}
    except Exception as error:  # pragma: no cover - shown only when the DB is down
        app.logger.exception("Health check failed")
        return {"status": "error", "detail": type(error).__name__}, 503


@app.errorhandler(400)
@app.errorhandler(404)
@app.errorhandler(500)
def error_page(error):
    code = getattr(error, "code", 500)
    message = getattr(error, "description", "Something went wrong.")
    if code == 500:
        message = "Something went wrong on the server. Check the Render logs for details."
    return render_template("base.html", error_code=code, error_message=message), code


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    print(f" * Database: {core.database_label()}")
    app.run(host="127.0.0.1", port=port, debug=os.getenv("FLASK_DEBUG") == "1")
