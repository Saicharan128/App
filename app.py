from flask import (
    Flask, render_template, request, redirect, url_for, flash, session, send_from_directory
)
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import func, or_, and_
from datetime import datetime, timedelta
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
import os
from pathlib import Path
import json

# ===== App & Config =====
app = Flask(__name__)
app.secret_key = "dev-secret"  # change in prod
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///ims.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

# uploads
APP_ROOT = Path(__file__).resolve().parent
UPLOAD_ROOT = APP_ROOT / "uploads" / "reports"
UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)
app.config["UPLOAD_FOLDER"] = str(UPLOAD_ROOT)
ALLOWED_REPORT_EXT = {"pdf", "doc", "docx"}

# simple depreciation schedule (cap at 70%)
# You can tune these numbers without code changes.
app.config["DEPR_RULE"] = dict(Y1=10.0, Y2=8.0, Y3=7.0, Y4PLUS=5.0, CAP=70.0)

db = SQLAlchemy(app)

# ===== Enums =====
class Role:
    ADMIN = "ADMIN"
    ENGINEER = "ENGINEER"
    ACCOUNTANT = "ACCOUNTANT"
    ALL = [ADMIN, ENGINEER, ACCOUNTANT]

class InspectionType:
    PSIC = "PSIC"
    CE_VAL = "CE_VAL"   # CE – Valuation & Depreciation
    CE_FIT = "CE_FIT"   # CE – Fitness / Condition
    ALL = [PSIC, CE_VAL, CE_FIT]

    CODE_TO_LABEL = {
        PSIC: "PSIC",
        CE_VAL: "CE – Valuation & Depreciation",
        CE_FIT: "CE – Fitness / Condition Certificate"
    }
    CODE_TO_ID_TOKEN = {
        PSIC: "PSIC",
        CE_VAL: "CE-VAL",
        CE_FIT: "CE-FIT",
    }

class InspectionStatus:
    DRAFT = "DRAFT"
    UNDER_REVIEW = "UNDER_REVIEW"
    COMPLETED = "COMPLETED"
    REPORT_UPLOADED = "REPORT_UPLOADED"
    INVOICED = "INVOICED"
    # Legacy values kept for backward compatibility with older data
    PENDING = "PENDING"
    REPORT_GENERATED = "REPORT_GENERATED"
    ALL = [DRAFT, UNDER_REVIEW, COMPLETED, REPORT_UPLOADED, INVOICED]

class InvoiceStatus:
    DRAFT = "DRAFT"
    SENT = "SENT"
    PAID = "PAID"
    ALL = [DRAFT, SENT, PAID]

class ReportStatus:
    DRAFT = "DRAFT"
    FINAL = "FINAL"
    ALL = [DRAFT, FINAL]

class CommissionStatus:
    DUE = "DUE"
    PARTIAL = "PARTIAL"
    PAID = "PAID"
    ALL = [DUE, PARTIAL, PAID]

# ===== Models =====
class User(db.Model):
    __tablename__ = "user"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), default=Role.ENGINEER, nullable=False)
    phone = db.Column(db.String(40))
    signature_key = db.Column(db.String(255))

class Client(db.Model):
    __tablename__ = "client"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(160), nullable=False)
    gst_number = db.Column(db.String(40))
    billing_address = db.Column(db.Text)

class CHA(db.Model):
    __tablename__ = "cha"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(160), nullable=False)
    contact = db.Column(db.String(160))
    commission_rate = db.Column(db.Float, default=0.0)  # %

class Inspection(db.Model):
    __tablename__ = "inspection"
    id = db.Column(db.Integer, primary_key=True)

    # Unique public id like INS-PSIC-2025-001
    public_id = db.Column(db.String(40), unique=True, index=True)
    seq_num = db.Column(db.Integer)  # sequence within type-year

    date = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    inspection_type = db.Column(db.String(20), default=InspectionType.PSIC, nullable=False)

    client_id = db.Column(db.Integer, db.ForeignKey("client.id"))
    client = db.relationship("Client")

    location = db.Column(db.String(200))
    asset = db.Column(db.String(200))

    # Universal: CHA / Forwarder + commission override
    cha_id = db.Column(db.Integer, db.ForeignKey("cha.id"))
    cha = db.relationship("CHA")
    forwarder_name = db.Column(db.String(160))
    cha_commission_pct = db.Column(db.Float)  # optional override of CHA.commission_rate

    status = db.Column(db.String(40), default=InspectionStatus.DRAFT)
    engineer_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    engineer = db.relationship("User")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # ---- PSIC fields ----
    scrap_type = db.Column(db.String(40))  # Metal / Plastic / Paper / Other
    container_count = db.Column(db.Integer)
    container_weight = db.Column(db.Float)  # tons
    container_notes = db.Column(db.Text)

    # ---- CE – Valuation & Depreciation ----
    machinery_type = db.Column(db.String(160))
    year_of_manufacture = db.Column(db.Integer)
    original_cif_value = db.Column(db.Float)
    depreciation_pct = db.Column(db.Float)  # auto
    residual_value = db.Column(db.Float)    # auto
    balance_useful_life = db.Column(db.String(40))  # manual text like "3 years"

    # ---- CE – Fitness / Condition ----
    goods_details = db.Column(db.Text)
    condition_notes = db.Column(db.Text)
    fair_market_value = db.Column(db.Float)

class Report(db.Model):
    __tablename__ = "report"
    id = db.Column(db.Integer, primary_key=True)
    inspection_id = db.Column(db.Integer, db.ForeignKey("inspection.id"), unique=True)
    inspection = db.relationship("Inspection", backref=db.backref("report", uselist=False))
    status = db.Column(db.String(20), default=ReportStatus.DRAFT)
    body = db.Column(db.Text)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow)

class ReportFile(db.Model):
    __tablename__ = "report_file"
    id = db.Column(db.Integer, primary_key=True)
    inspection_id = db.Column(db.Integer, db.ForeignKey("inspection.id"))
    inspection = db.relationship("Inspection", backref=db.backref("files", lazy="dynamic"))
    uploader_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    uploader = db.relationship("User")
    stored_name = db.Column(db.String(255))   # on-disk filename
    original_name = db.Column(db.String(255))
    mimetype = db.Column(db.String(80))
    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow)

class Invoice(db.Model):
    __tablename__ = "invoice"
    id = db.Column(db.Integer, primary_key=True)
    inspection_id = db.Column(db.Integer, db.ForeignKey("inspection.id"), unique=True)
    inspection = db.relationship("Inspection", backref=db.backref("invoice", uselist=False))
    fee = db.Column(db.Float, default=0.0)
    tax_pct = db.Column(db.Float, default=18.0)
    total = db.Column(db.Float, default=0.0)
    status = db.Column(db.String(20), default=InvoiceStatus.DRAFT)
    notes = db.Column(db.Text)

class Commission(db.Model):
    __tablename__ = "commission"
    id = db.Column(db.Integer, primary_key=True)
    inspection_id = db.Column(db.Integer, db.ForeignKey("inspection.id"), unique=True)
    inspection = db.relationship("Inspection", backref=db.backref("commission", uselist=False))
    cha_id = db.Column(db.Integer, db.ForeignKey("cha.id"))
    cha = db.relationship("CHA")
    amount = db.Column(db.Float, default=0.0)
    status = db.Column(db.String(20), default=CommissionStatus.DUE)

class Template(db.Model):
    __tablename__ = "template"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(160), nullable=False)
    active = db.Column(db.Boolean, default=True)
    ai_prompt = db.Column(db.Text)
    html_snippet = db.Column(db.Text)

class Annexure(db.Model):
    __tablename__ = "annexure"
    id = db.Column(db.Integer, primary_key=True)
    inspection_id = db.Column(db.Integer, db.ForeignKey("inspection.id"))
    inspection = db.relationship("Inspection", backref=db.backref("annexures", lazy="dynamic"))

    sno = db.Column(db.Integer)
    description = db.Column(db.Text)
    qty = db.Column(db.Integer)
    manufacturer = db.Column(db.String(200))
    markings = db.Column(db.String(200))
    yom = db.Column(db.Integer)
    unit_invoice_value = db.Column(db.Float)
    total_invoice_value = db.Column(db.Float)
    unit_fob_price = db.Column(db.Float)
    unit_present_assessed_value = db.Column(db.Float)
    total_present_assessed_value = db.Column(db.Float)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class AuditLog(db.Model):
    __tablename__ = "audit_log"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, nullable=True)
    action = db.Column(db.String(20), nullable=False)         # create | update | delete | status | assign | annexure_add | annexure_delete
    entity = db.Column(db.String(40), nullable=False)         # 'inspection'
    entity_id = db.Column(db.Integer, nullable=False)
    changes = db.Column(db.Text, nullable=True)               # JSON string of changes/diff
    ip = db.Column(db.String(64), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

# ===== Helpers =====
def role_required(*roles):
    def deco(fn):
        from functools import wraps
        @wraps(fn)
        def wrap(*a, **kw):
            if not session.get("user_id"):
                flash("Please login.", "warning")
                return redirect(url_for("login"))
            if roles and session.get("role") not in roles:
                flash("Unauthorized.", "danger")
                return redirect(url_for("dashboard"))
            return fn(*a, **kw)
        return wrap
    return deco

def calc_total(fee, tax_pct):
    return round((fee or 0.0) * (1 + (tax_pct or 0)/100.0), 2)

def ensure_report(inspection_id):
    rep = Report.query.filter_by(inspection_id=inspection_id).first()
    if not rep:
        rep = Report(inspection_id=inspection_id, status=ReportStatus.DRAFT, body="")
        db.session.add(rep); db.session.commit()
    return rep

def ensure_invoice(inspection_id, fee=0.0, tax_pct=18.0):
    inv = Invoice.query.filter_by(inspection_id=inspection_id).first()
    if not inv:
        inv = Invoice(inspection_id=inspection_id, fee=fee, tax_pct=tax_pct, total=calc_total(fee, tax_pct))
        db.session.add(inv); db.session.commit()
    return inv

def allowed_report_file(filename: str) -> bool:
    if not filename or "." not in filename:
        return False
    ext = filename.rsplit(".", 1)[1].lower()
    return ext in ALLOWED_REPORT_EXT

def generate_public_id(i: "Inspection"):
    """Generate and assign public_id and seq_num for an inspection if missing."""
    if i.public_id:
        return
    year = (i.date or datetime.utcnow()).year
    type_token = InspectionType.CODE_TO_ID_TOKEN.get(i.inspection_type, "GEN")
    # Find max seq for this type-year
    max_seq = (
        db.session.query(func.max(Inspection.seq_num))
        .filter(and_(Inspection.inspection_type == i.inspection_type,
                     func.strftime('%Y', Inspection.date) == str(year)))
        .scalar()
    )
    next_seq = (max_seq or 0) + 1
    i.seq_num = next_seq
    i.public_id = f"INS-{type_token}-{year}-{next_seq:03d}"

def compute_depreciation_pct(year_of_manufacture: int, on_date: datetime) -> float:
    """Very simple piecewise yearly schedule, capped. Tunable via app.config['DEPR_RULE']."""
    if not year_of_manufacture:
        return 0.0
    years = max(0, (on_date.year - int(year_of_manufacture)))
    r = app.config["DEPR_RULE"]
    pct = 0.0
    if years >= 1:
        pct += r["Y1"]
    if years >= 2:
        pct += r["Y2"]
    if years >= 3:
        pct += r["Y3"]
    if years >= 4:
        pct += r["Y4PLUS"] * (years - 3)
    return min(pct, r["CAP"])

def upsert_commission_from_inspection(i: "Inspection"):
    """Create or update a Commission row based on invoice fee and CHA rate / override."""
    rate = None
    if i.cha_commission_pct is not None:
        rate = i.cha_commission_pct
    elif i.cha and i.cha.commission_rate is not None:
        rate = i.cha.commission_rate
    else:
        rate = 0.0
    fee = i.invoice.fee if getattr(i, "invoice", None) else 0.0
    amount = round((fee or 0.0) * (rate or 0.0) / 100.0, 2)
    com = Commission.query.filter_by(inspection_id=i.id).first()
    if not com:
        com = Commission(inspection_id=i.id, cha_id=i.cha_id, amount=amount, status=CommissionStatus.DUE)
        db.session.add(com)
    else:
        com.cha_id = i.cha_id
        com.amount = amount
    db.session.commit()

def _client_ip():
    # works behind proxies/load balancers if you set X-Forwarded-For
    return request.headers.get("X-Forwarded-For", request.remote_addr)

def log_action(action, entity, entity_id, changes=None):
    try:
        log = AuditLog(
            user_id=session.get("user_id"),
            action=action,
            entity=entity,
            entity_id=entity_id,
            changes=(json.dumps(changes, ensure_ascii=False) if changes else None),
            ip=_client_ip(),
        )
        db.session.add(log)
        db.session.commit()
    except Exception as e:
        app.logger.warning(f"AuditLog failed: {e}")

def dict_diff(before: dict, after: dict):
    """Return {field: [old, new]} for changed fields only."""
    diff = {}
    for k in set(before.keys()) | set(after.keys()):
        if before.get(k) != after.get(k):
            diff[k] = [before.get(k), after.get(k)]
    return diff

@app.context_processor
def inject_globals():
    return dict(
        Role=Role, InspectionStatus=InspectionStatus, InvoiceStatus=InvoiceStatus,
        ReportStatus=ReportStatus, CommissionStatus=CommissionStatus, now=datetime.utcnow,
        User=User, InspectionType=InspectionType
    )

# ===== Auth =====
@app.route("/register", methods=["GET","POST"])
def register():
    first_user = (User.query.first() is None)
    is_admin = (session.get("role") == Role.ADMIN)
    can_choose_role = first_user or is_admin

    if request.method == "POST":
        name = request.form["name"].strip()
        email = request.form["email"].strip().lower()
        pwd = request.form["password"]

        if first_user:
            role = Role.ADMIN
        elif is_admin:
            role = request.form.get("role", Role.ENGINEER)
        else:
            role = Role.ENGINEER

        if User.query.filter_by(email=email).first():
            flash("Email already registered.", "danger")
            return redirect(url_for("register"))

        u = User(name=name, email=email,
                 password_hash=generate_password_hash(pwd),
                 role=role)
        db.session.add(u); db.session.commit()
        flash("Registered. Please login.", "success")
        return redirect(url_for("login"))

    roles = Role.ALL if can_choose_role else [Role.ENGINEER]
    return render_template("register.html", roles=roles, can_choose_role=can_choose_role)

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email","").strip().lower()
        pwd = request.form.get("password","")
        u = User.query.filter_by(email=email).first()
        if u and check_password_hash(u.password_hash, pwd):
            session["user_id"] = u.id
            session["role"] = u.role
            session["user_name"] = u.name
            flash(f"Welcome {u.name}", "success")
            return redirect(url_for("dashboard"))
        flash("Invalid credentials.", "danger")
    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear()
    flash("Logged out.", "info")
    return redirect(url_for("login"))

# ===== Users Admin =====
@app.route("/users", methods=["GET","POST"])
@role_required(Role.ADMIN)
def users_admin():
    if request.method == "POST":
        uid = int(request.form["user_id"])
        role = request.form["role"]
        u = User.query.get_or_404(uid)
        u.role = role
        db.session.commit()
        flash("Role updated.", "success")
        return redirect(url_for("users_admin"))
    users = User.query.order_by(User.id.asc()).all()
    return render_template("users.html", users=users)

# ===== Clients =====
@app.route("/clients", methods=["GET","POST"], endpoint="clients_list")
@role_required(Role.ADMIN)
def clients_list():
    if request.method == "POST":
        name = request.form["name"].strip()
        gst = request.form.get("gst_number","")
        addr = request.form.get("billing_address","")
        if not name:
            flash("Name required.", "warning"); return redirect(url_for("clients_list"))
        db.session.add(Client(name=name, gst_number=gst, billing_address=addr))
        db.session.commit()
        flash("Client added.", "success")
        return redirect(url_for("clients_list"))
    data = Client.query.order_by(Client.name.asc()).all()
    return render_template("clients.html", data=data)

@app.route("/clients/<int:cid>/delete", methods=["POST"], endpoint="clients_delete")
@role_required(Role.ADMIN)
def clients_delete(cid):
    obj = Client.query.get_or_404(cid)
    db.session.delete(obj); db.session.commit()
    flash("Client deleted.", "info")
    return redirect(url_for("clients_list"))

@app.route("/clients/<int:cid>/update", methods=["POST"], endpoint="clients_update")
@role_required(Role.ADMIN)
def clients_update(cid):
    c = Client.query.get_or_404(cid)
    # pull fields, keep current values if blank/missing
    name = request.form.get("name", c.name).strip()
    gst  = request.form.get("gst_number", c.gst_number or "").strip()
    addr = request.form.get("billing_address", c.billing_address or "")

    if not name:
        flash("Client name is required.", "warning")
        return redirect(url_for("clients_list"))

    c.name = name
    c.gst_number = gst
    c.billing_address = addr
    db.session.commit()
    flash("Client updated.", "success")
    return redirect(url_for("clients_list"))

# ===== CHAs (mgmt list) =====
@app.route("/chas", methods=["GET","POST"], endpoint="chas_list")
@role_required(Role.ADMIN)
def chas_list():
    if request.method == "POST":
        name = request.form["name"].strip()
        contact = request.form.get("contact","")
        rate = float(request.form.get("commission_rate", 0))
        if not name:
            flash("Name required.", "warning"); return redirect(url_for("chas_list"))
        db.session.add(CHA(name=name, contact=contact, commission_rate=rate))
        db.session.commit()
        flash("CHA added.", "success")
        return redirect(url_for("chas_list"))
    data = CHA.query.order_by(CHA.name.asc()).all()
    return render_template("chas.html", data=data)

@app.route("/chas/<int:cha_id>/delete", methods=["POST"], endpoint="chas_delete")
@role_required(Role.ADMIN)
def chas_delete(cha_id):
    obj = CHA.query.get_or_404(cha_id)
    db.session.delete(obj); db.session.commit()
    flash("CHA deleted.", "info")
    return redirect(url_for("chas_list"))

# NEW: Admin can update CHA fields
@app.route("/chas/<int:cha_id>/update", methods=["POST"], endpoint="chas_update")
@role_required(Role.ADMIN)
def chas_update(cha_id):
    obj = CHA.query.get_or_404(cha_id)
    obj.name = request.form.get("name", obj.name).strip()
    obj.contact = request.form.get("contact", obj.contact)
    try:
        rate = float(request.form.get("commission_rate", obj.commission_rate or 0))
        obj.commission_rate = max(0.0, min(rate, 100.0))
    except ValueError:
        flash("Invalid commission rate.", "warning"); return redirect(url_for("chas_list"))
    db.session.commit()
    flash("CHA updated.", "success")
    return redirect(url_for("chas_list"))

# ===== Dashboard / Search =====
@app.route("/")
@role_required(Role.ADMIN, Role.ENGINEER, Role.ACCOUNTANT)
def dashboard():
    date_from = request.args.get("from")
    date_to = request.args.get("to")
    status = request.args.get("status")
    cha_id = request.args.get("cha_id")
    eng_id = request.args.get("engineer_id")
    q = request.args.get("q")

    qry = Inspection.query
    # Inclusive start
    if date_from:
        df = datetime.fromisoformat(date_from)
        qry = qry.filter(Inspection.date >= df)
    # Inclusive end for whole-day inputs
    if date_to:
        dt = datetime.fromisoformat(date_to)
        if len(date_to) == 10:  # yyyy-mm-dd
            dt = dt + timedelta(days=1)
            qry = qry.filter(Inspection.date < dt)
        else:
            qry = qry.filter(Inspection.date <= dt)

    if status:
        qry = qry.filter(Inspection.status == status)
    if cha_id:
        qry = qry.filter(Inspection.cha_id == int(cha_id))
    if eng_id:
        qry = qry.filter(Inspection.engineer_id == int(eng_id))
    if q:
        like = f"%{q}%"
        qry = qry.join(Client).filter(or_(
            Client.name.ilike(like), Inspection.location.ilike(like),
            Inspection.asset.ilike(like), Inspection.public_id.ilike(like)
        ))

    inspections = qry.order_by(Inspection.date.desc()).all()
    groups = {s: [] for s in InspectionStatus.ALL}
    for i in inspections:
        if i.status in groups:
            groups[i.status].append(i)
        else:
            groups[InspectionStatus.DRAFT].append(i)  # bucket legacy

    chas = CHA.query.all()
    engineers = User.query.filter(User.role == Role.ENGINEER).all()
    clients = Client.query.all()
    return render_template("dashboard.html", groups=groups, chas=chas, engineers=engineers, clients=clients)

@app.route("/search")
@role_required(Role.ADMIN, Role.ENGINEER, Role.ACCOUNTANT)
def global_search():
    q = request.args.get("q", "")
    like = f"%{q}%"
    inspections = Inspection.query.join(Client).filter(
        or_(Client.name.ilike(like), Inspection.location.ilike(like), Inspection.asset.ilike(like), Inspection.public_id.ilike(like))
    ).all()
    clients = Client.query.filter(Client.name.ilike(like)).all()
    invoices = Invoice.query.join(Inspection).join(Client).filter(Client.name.ilike(like)).all()
    return render_template("search_results.html", q=q, inspections=inspections, clients=clients, invoices=invoices)

# ===== Inspections =====
# allow ADMIN and ENGINEER to create; default engineer to self for ENGINEER
# allow ADMIN and ENGINEER to create; default engineer to self for ENGINEER
@app.route("/inspections/new", methods=["POST"])
@role_required(Role.ADMIN, Role.ENGINEER)
def inspection_create():
    i = Inspection(
        date=datetime.fromisoformat(request.form["date"]),
        client_id=int(request.form["client_id"]),
        location=request.form.get("location",""),
        asset=request.form.get("asset",""),
        status=request.form.get("status", InspectionStatus.DRAFT),
        engineer_id=(
            int(request.form["engineer_id"])
            if request.form.get("engineer_id")
            else session.get("user_id") if session.get("role") == Role.ENGINEER else None
        ),
        cha_id=int(request.form["cha_id"]) if request.form.get("cha_id") else None,
        inspection_type=request.form.get("inspection_type", InspectionType.PSIC),
        forwarder_name=request.form.get("forwarder_name","").strip() or None,
        cha_commission_pct=(float(request.form.get("cha_commission_pct")) if request.form.get("cha_commission_pct") else None),
    )

    # type-specific fields
    t = i.inspection_type
    if t == InspectionType.PSIC:
        i.scrap_type = request.form.get("scrap_type") or None
        i.container_count = (int(request.form.get("container_count")) if request.form.get("container_count") else None)
        i.container_weight = (float(request.form.get("container_weight")) if request.form.get("container_weight") else None)
        i.container_notes = request.form.get("container_notes") or None

    elif t == InspectionType.CE_VAL:
        i.machinery_type = request.form.get("machinery_type") or None
        i.year_of_manufacture = (int(request.form.get("year_of_manufacture")) if request.form.get("year_of_manufacture") else None)
        i.original_cif_value = (float(request.form.get("original_cif_value")) if request.form.get("original_cif_value") else None)
        # auto depreciation and residual
        dep_pct = compute_depreciation_pct(i.year_of_manufacture or 0, i.date)
        i.depreciation_pct = dep_pct
        if i.original_cif_value is not None:
            i.residual_value = round(max(0.0, (i.original_cif_value or 0.0) * (1 - dep_pct/100.0)), 2)
        i.balance_useful_life = request.form.get("balance_useful_life") or None

    elif t == InspectionType.CE_FIT:
        i.goods_details = request.form.get("goods_details") or None
        i.condition_notes = request.form.get("condition_notes") or None
        i.fair_market_value = (float(request.form.get("fair_market_value")) if request.form.get("fair_market_value") else None)

    db.session.add(i)
    db.session.commit()

    # assign public id
    generate_public_id(i)
    db.session.commit()
    log_action("create", "inspection", i.id, changes={"form": request.form.to_dict(flat=True), "public_id": i.public_id})

    # ===== Annexure handling (for CE inspections only) =====
    if i.inspection_type in [InspectionType.CE_VAL, InspectionType.CE_FIT]:
        annexure_snos = request.form.getlist("annexure_sno[]")
        annexure_descs = request.form.getlist("annexure_description[]")
        annexure_qtys = request.form.getlist("annexure_qty[]")
        annexure_mfrs = request.form.getlist("annexure_manufacturer[]")
        annexure_marks = request.form.getlist("annexure_markings[]")
        annexure_yoms = request.form.getlist("annexure_yom[]")
        annexure_unit_vals = request.form.getlist("annexure_unit_invoice_value[]")
        annexure_total_vals = request.form.getlist("annexure_total_invoice_value[]")

        for idx in range(len(annexure_descs)):
            if annexure_descs[idx].strip():  # only save if description is filled
                a = Annexure(
                    inspection_id=i.id,
                    sno=(int(annexure_snos[idx]) if annexure_snos[idx] else None),
                    description=annexure_descs[idx],
                    qty=(int(annexure_qtys[idx]) if annexure_qtys[idx] else None),
                    manufacturer=annexure_mfrs[idx] if idx < len(annexure_mfrs) else None,
                    markings=annexure_marks[idx] if idx < len(annexure_marks) else None,
                    yom=(int(annexure_yoms[idx]) if idx < len(annexure_yoms) and annexure_yoms[idx] else None),
                    unit_invoice_value=(float(annexure_unit_vals[idx]) if idx < len(annexure_unit_vals) and annexure_unit_vals[idx] else None),
                    total_invoice_value=(float(annexure_total_vals[idx]) if idx < len(annexure_total_vals) and annexure_total_vals[idx] else None),
                )
                db.session.add(a)
        db.session.commit()
    # ===== End Annexure handling =====

    flash("Inspection created.", "success")
    return redirect(url_for("inspection_detail", inspection_id=i.id))

@app.route("/inspections/<int:inspection_id>")
@role_required(Role.ADMIN, Role.ENGINEER, Role.ACCOUNTANT)
def inspection_detail(inspection_id):
    i = Inspection.query.get_or_404(inspection_id)
    rep = Report.query.filter_by(inspection_id=inspection_id).first()
    inv = Invoice.query.filter_by(inspection_id=inspection_id).first()
    latest_file = ReportFile.query.filter_by(inspection_id=inspection_id).order_by(ReportFile.uploaded_at.desc()).first()
    return render_template("inspection_detail.html", i=i, rep=rep, inv=inv, latest_file=latest_file)

# engineer can also update CHA for their own inspection
@app.route("/inspections/<int:inspection_id>/edit", methods=["GET","POST"])
@role_required(Role.ADMIN, Role.ENGINEER)
def inspection_edit(inspection_id):
    i = Inspection.query.get_or_404(inspection_id)
    if session.get("role") == Role.ENGINEER and i.engineer_id != session.get("user_id"):
        flash("Unauthorized.", "danger"); return redirect(url_for("inspection_detail", inspection_id=inspection_id))

    if request.method == "POST":
        i.date = datetime.fromisoformat(request.form["date"])
        i.client_id = int(request.form["client_id"])
        i.location = request.form.get("location","")
        i.asset = request.form.get("asset","")
        i.forwarder_name = request.form.get("forwarder_name","").strip() or None
        i.cha_commission_pct = (float(request.form.get("cha_commission_pct")) if request.form.get("cha_commission_pct") else None)

        # Allow both roles to update CHA (engineer only on their own inspection)
        if request.form.get("cha_id") is not None:
            i.cha_id = int(request.form["cha_id"]) if request.form.get("cha_id") else None

        if session.get("role") == Role.ADMIN:
            i.status = request.form.get("status", i.status)
            i.engineer_id = int(request.form["engineer_id"]) if request.form.get("engineer_id") else i.engineer_id
            i.inspection_type = request.form.get("inspection_type", i.inspection_type)

        # type-specific updates
        t = i.inspection_type
        if t == InspectionType.PSIC:
            i.scrap_type = request.form.get("scrap_type") or None
            i.container_count = (int(request.form.get("container_count")) if request.form.get("container_count") else None)
            i.container_weight = (float(request.form.get("container_weight")) if request.form.get("container_weight") else None)
            i.container_notes = request.form.get("container_notes") or None

        elif t == InspectionType.CE_VAL:
            i.machinery_type = request.form.get("machinery_type") or None
            i.year_of_manufacture = (int(request.form.get("year_of_manufacture")) if request.form.get("year_of_manufacture") else None)
            i.original_cif_value = (float(request.form.get("original_cif_value")) if request.form.get("original_cif_value") else None)
            dep_pct = compute_depreciation_pct(i.year_of_manufacture or 0, i.date)
            i.depreciation_pct = dep_pct
            if i.original_cif_value is not None:
                i.residual_value = round(max(0.0, (i.original_cif_value or 0.0) * (1 - dep_pct/100.0)), 2)
            i.balance_useful_life = request.form.get("balance_useful_life") or None

        elif t == InspectionType.CE_FIT:
            i.goods_details = request.form.get("goods_details") or None
            i.condition_notes = request.form.get("condition_notes") or None
            i.fair_market_value = (float(request.form.get("fair_market_value")) if request.form.get("fair_market_value") else None)

        # ensure public id exists (e.g., if previously legacy entry)
        if not i.public_id:
            generate_public_id(i)

        db.session.commit()
        flash("Inspection updated.", "success")
        return redirect(url_for("inspection_detail", inspection_id=inspection_id))

    clients = Client.query.order_by(Client.name.asc()).all()
    chas = CHA.query.order_by(CHA.name.asc()).all()
    engineers = User.query.filter(User.role == Role.ENGINEER).all()
    logs = AuditLog.query.filter_by(entity="inspection", entity_id=i.id)\
                     .order_by(AuditLog.created_at.desc())\
                     .limit(20).all()
    return render_template("inspection_edit.html", i=i, clients=clients, chas=chas, engineers=engineers)

# admin delete inspection (cleans dependents)
@app.route("/inspections/<int:inspection_id>/delete", methods=["POST"])
@role_required(Role.ADMIN)
def inspection_delete(inspection_id):
    i = Inspection.query.get_or_404(inspection_id)
    Report.query.filter_by(inspection_id=inspection_id).delete()
    Invoice.query.filter_by(inspection_id=inspection_id).delete()
    Commission.query.filter_by(inspection_id=inspection_id).delete()
    # delete files
    files = ReportFile.query.filter_by(inspection_id=inspection_id).all()
    for f in files:
        try:
            (UPLOAD_ROOT / f.stored_name).unlink(missing_ok=True)
        except Exception:
            pass
        db.session.delete(f)
    db.session.delete(i)
    db.session.commit()
    log_action("delete", "inspection", inspection_id)
    flash("Inspection deleted.", "info")
    return redirect(url_for("dashboard"))

@app.route("/inspections/<int:inspection_id>/status", methods=["POST"])
@role_required(Role.ADMIN, Role.ENGINEER)
def inspection_status(inspection_id):
    i = Inspection.query.get_or_404(inspection_id)
    if session.get("role") == Role.ENGINEER and i.engineer_id != session.get("user_id"):
        flash("Unauthorized.", "danger"); return redirect(url_for("inspection_detail", inspection_id=inspection_id))
    i.status = request.form["status"]
    db.session.commit()
    log_action("status", "inspection", inspection_id, changes={"status": i.status})
    flash("Status updated.", "success")
    return redirect(url_for("inspection_detail", inspection_id=inspection_id))

@app.route("/users/assign/<int:inspection_id>", methods=["POST"])
@role_required(Role.ADMIN)
def assign_engineer(inspection_id):
    i = Inspection.query.get_or_404(inspection_id)
    i.engineer_id = int(request.form["engineer_id"]) if request.form.get("engineer_id") else None
    db.session.commit()
    log_action("assign", "inspection", inspection_id, changes={"engineer_id": i.engineer_id})
    flash("Engineer assigned.", "success")
    return redirect(url_for("inspection_detail", inspection_id=inspection_id))

# ===== Reports =====
@app.route("/reports/<int:inspection_id>/edit", methods=["GET","POST"])
@role_required(Role.ADMIN, Role.ENGINEER)
def report_edit(inspection_id):
    i = Inspection.query.get_or_404(inspection_id)
    rep = ensure_report(inspection_id)
    if request.method == "POST":
        # File upload path
        if "report_file" in request.files and request.files["report_file"].filename:
            file = request.files["report_file"]
            if not allowed_report_file(file.filename):
                flash("Invalid file type. Allowed: PDF, DOC, DOCX.", "warning")
                return redirect(url_for("report_edit", inspection_id=inspection_id))
            safe = secure_filename(file.filename)
            stored = f"{inspection_id}_{int(datetime.utcnow().timestamp())}_{safe}"
            save_path = UPLOAD_ROOT / stored
            file.save(save_path)
            rf = ReportFile(
                inspection_id=inspection_id,
                uploader_id=session.get("user_id"),
                stored_name=stored,
                original_name=file.filename,
                mimetype=file.mimetype
            )
            db.session.add(rf)
            # auto status update
            i.status = InspectionStatus.REPORT_UPLOADED
            # mark report as final for clarity if body exists
            rep.status = ReportStatus.FINAL
            rep.updated_at = datetime.utcnow()
            db.session.commit()
            flash("Report uploaded.", "success")
            return redirect(url_for("inspection_detail", inspection_id=inspection_id))

        action = request.form.get("action")
        body = request.form.get("body", "")
        if action == "ai":
            t = Template.query.filter_by(active=True).first()
            if t and t.html_snippet:
                body = (t.html_snippet
                        .replace("{{client}}", i.client.name if i.client else "")
                        .replace("{{location}}", i.location or "")
                        .replace("{{asset}}", i.asset or "")
                        .replace("{{engineer}}", i.engineer.name if i.engineer else "")
                        .replace("{{findings}}", "All primary checks completed. No critical deviations."))
        rep.body = body
        if request.form.get("save_as") == "final":
            rep.status = ReportStatus.FINAL
            # keep workflow separate from external upload; don't force REPORT_UPLOADED
            if i.status == InspectionStatus.UNDER_REVIEW:
                i.status = InspectionStatus.COMPLETED
        else:
            rep.status = ReportStatus.DRAFT
        rep.updated_at = datetime.utcnow()
        db.session.commit()
        flash("Report saved.", "success")
        return redirect(url_for("inspection_detail", inspection_id=inspection_id))
    latest_file = ReportFile.query.filter_by(inspection_id=inspection_id).order_by(ReportFile.uploaded_at.desc()).first()
    return render_template("report_edit.html", i=i, rep=rep, latest_file=latest_file)

from flask import render_template_string

@app.route("/inspections/<int:inspection_id>/report/view")
@role_required(Role.ADMIN, Role.ENGINEER, Role.ACCOUNTANT)
def report_view(inspection_id):
    i = Inspection.query.get_or_404(inspection_id)
    rep = Report.query.filter_by(inspection_id=inspection_id).first()

    if not rep or not rep.body:
        return render_template("report_view.html", i=i, rep=rep)

    # Render template placeholders with inspection values
    rendered_body = render_template_string(rep.body, i=i)

    return render_template("report_view.html", i=i, rep=rep, rendered_body=rendered_body)

@app.route("/inspections/<int:inspection_id>/report/export")
@role_required(Role.ADMIN, Role.ENGINEER, Role.ACCOUNTANT)
def report_export(inspection_id):
    i = Inspection.query.get_or_404(inspection_id)

    # Choose template based on inspection type
    if i.inspection_type == InspectionType.PSIC:
        template_name = "psic_certificate.html"
    elif i.inspection_type in [InspectionType.CE_VAL, InspectionType.CE_FIT]:
        template_name = "ce_certificate.html"
    else:
        flash("No dynamic report template available for this inspection type.", "warning")
        return redirect(url_for("inspection_detail", inspection_id=inspection_id))

    return render_template(template_name, i=i)

@app.route("/reports/download/<int:file_id>")
@role_required(Role.ADMIN, Role.ENGINEER, Role.ACCOUNTANT)
def report_download(file_id):
    f = ReportFile.query.get_or_404(file_id)
    return send_from_directory(app.config["UPLOAD_FOLDER"], f.stored_name, as_attachment=True, download_name=f.original_name)

# ===== Report Library (Admin only) =====
@app.route("/report-library")
@role_required(Role.ADMIN)
def report_library():
    q = request.args.get("q","").strip()
    itype = request.args.get("type","").strip()
    date_from = request.args.get("from")
    date_to = request.args.get("to")

    qry = db.session.query(ReportFile, Inspection, Client).join(Inspection, ReportFile.inspection_id == Inspection.id).join(Client, Inspection.client_id == Client.id)

    if q:
        like = f"%{q}%"
        qry = qry.filter(or_(Inspection.public_id.ilike(like),
                             Client.name.ilike(like)))
    if itype:
        qry = qry.filter(Inspection.inspection_type == itype)
    if date_from:
        df = datetime.fromisoformat(date_from)
        qry = qry.filter(Inspection.date >= df)
    if date_to:
        dt = datetime.fromisoformat(date_to)
        if len(date_to) == 10:
            dt = dt + timedelta(days=1)
            qry = qry.filter(Inspection.date < dt)
        else:
            qry = qry.filter(Inspection.date <= dt)

    rows = qry.order_by(ReportFile.uploaded_at.desc()).all()
    # We'll render to 'report_library.html'
    return render_template("report_library.html", rows=rows, InspectionType=InspectionType, q=q, type=itype)

# ===== Invoices =====
@app.route("/invoices/<int:inspection_id>", methods=["GET","POST"])
@role_required(Role.ADMIN, Role.ACCOUNTANT)
def invoice_edit(inspection_id):
    i = Inspection.query.get_or_404(inspection_id)
    inv = ensure_invoice(inspection_id)
    if request.method == "POST":
        inv.fee = float(request.form.get("fee", inv.fee or 0))
        inv.tax_pct = float(request.form.get("tax_pct", inv.tax_pct or 0))
        inv.total = calc_total(inv.fee, inv.tax_pct)
        inv.status = request.form.get("status", inv.status)
        inv.notes = request.form.get("notes", inv.notes)
        if inv.status in [InvoiceStatus.SENT, InvoiceStatus.PAID]:
            i.status = InspectionStatus.INVOICED
        db.session.commit()
        # recalc commission automatically if override / CHA rate present
        upsert_commission_from_inspection(i)
        flash("Invoice updated.", "success")
        return redirect(url_for("inspection_detail", inspection_id=inspection_id))
    return render_template("invoice_edit.html", i=i, inv=inv)

# ===== Commissions / CHA Tracker =====
@app.route("/cha-tracker")
@role_required(Role.ADMIN, Role.ACCOUNTANT)
def cha_tracker():
    rows = (
        db.session.query(Commission, Inspection, CHA)
        .join(Inspection, Commission.inspection_id == Inspection.id)
        .join(CHA, Commission.cha_id == CHA.id)
        .all()
    )
    summary = (
        db.session.query(CHA.name, Commission.status, func.sum(Commission.amount))
        .join(Commission, Commission.cha_id == CHA.id)
        .group_by(CHA.name, Commission.status)
        .all()
    )
    return render_template("cha.html", rows=rows, summary=summary)

# generate / recalc from invoice × rate (respect override if present)
@app.route("/commissions/generate/<int:inspection_id>", methods=["POST"])
@role_required(Role.ADMIN, Role.ACCOUNTANT)
def commission_generate(inspection_id):
    i = Inspection.query.get_or_404(inspection_id)
    if not i.cha and i.cha_commission_pct is None:
        flash("No CHA or commission % provided.", "warning"); return redirect(url_for("inspection_detail", inspection_id=inspection_id))
    upsert_commission_from_inspection(i)
    flash("Commission calculated.", "success")
    return redirect(url_for("inspection_detail", inspection_id=inspection_id))

@app.route("/commissions/<int:commission_id>/mark", methods=["POST"])
@role_required(Role.ADMIN, Role.ACCOUNTANT)
def commission_mark(commission_id):
    com = Commission.query.get_or_404(commission_id)
    com.status = request.form.get("status", CommissionStatus.PAID)
    db.session.commit()
    flash("Commission updated.", "success")
    return redirect(url_for("cha_tracker"))

# inline amount update
@app.route("/commissions/<int:commission_id>/update", methods=["POST"])
@role_required(Role.ADMIN, Role.ACCOUNTANT)
def commission_update(commission_id):
    com = Commission.query.get_or_404(commission_id)
    amt_raw = request.form.get("amount", "").strip()
    try:
        amount = float(amt_raw)
    except (TypeError, ValueError):
        flash("Invalid amount.", "warning")
        return redirect(url_for("cha_tracker"))
    com.amount = round(max(0.0, amount), 2)
    db.session.commit()
    flash("Commission amount updated.", "success")
    return redirect(url_for("cha_tracker"))

# ===== Templates =====
@app.route("/templates", methods=["GET","POST"])
@role_required(Role.ADMIN)
def templates_mgmt():
    if request.method == "POST":
        if "create" in request.form:
            db.session.add(Template(
                name=request.form["name"],
                active=("active" in request.form),
                ai_prompt=request.form.get("ai_prompt",""),
                html_snippet=request.form.get("html_snippet","")
            ))
        elif "toggle" in request.form:
            t = Template.query.get(int(request.form["template_id"]))
            t.active = not t.active
        db.session.commit()
        return redirect(url_for("templates_mgmt"))
    templates = Template.query.order_by(Template.active.desc(), Template.name.asc()).all()
    return render_template("templates.html", templates=templates)

# ===== Notifications =====
@app.route("/notifications")
@role_required(Role.ADMIN, Role.ENGINEER, Role.ACCOUNTANT)
def notifications():
    overdue_reports = Inspection.query.filter(
        Inspection.status == InspectionStatus.COMPLETED,
        Inspection.date < datetime.utcnow() - timedelta(days=3)
    ).all()
    missing_invoice = Inspection.query.filter(
        Inspection.status.in_([InspectionStatus.COMPLETED, InspectionStatus.REPORT_UPLOADED])
    ).filter(~Inspection.id.in_(db.session.query(Invoice.inspection_id))).all()
    commission_due = Commission.query.filter(Commission.status == CommissionStatus.DUE).all()
    return render_template("notifications.html",
                           overdue_reports=overdue_reports,
                           missing_invoice=missing_invoice,
                           commission_due=commission_due)

# ===== Lightweight auto-migration for SQLite (adds new columns if missing) =====
def _ensure_sqlite_columns():
    import sqlite3
    engine = db.get_engine()
    if "sqlite" not in str(engine.url):
        return
    con = engine.raw_connection()
    cur = con.cursor()
    def table_cols(table):
        cur.execute(f"PRAGMA table_info({table})")
        return {row[1] for row in cur.fetchall()}

    # inspection new columns
    want = {
        ("inspection","public_id","TEXT"),
        ("inspection","seq_num","INTEGER"),
        ("inspection","inspection_type","TEXT"),
        ("inspection","forwarder_name","TEXT"),
        ("inspection","cha_commission_pct","REAL"),
        ("inspection","scrap_type","TEXT"),
        ("inspection","container_count","INTEGER"),
        ("inspection","container_weight","REAL"),
        ("inspection","container_notes","TEXT"),
        ("inspection","machinery_type","TEXT"),
        ("inspection","year_of_manufacture","INTEGER"),
        ("inspection","original_cif_value","REAL"),
        ("inspection","depreciation_pct","REAL"),
        ("inspection","residual_value","REAL"),
        ("inspection","balance_useful_life","TEXT"),
        ("inspection","goods_details","TEXT"),
        ("inspection","condition_notes","TEXT"),
        ("inspection","fair_market_value","REAL"),
    }
    cols = table_cols("inspection")
    for tbl, col, typ in want:
        if col not in cols:
            cur.execute(f"ALTER TABLE {tbl} ADD COLUMN {col} {typ}")
    con.commit()
    cur.close(); con.close()

@app.route("/inspections/<int:inspection_id>/annexures/add", methods=["POST"])
@role_required(Role.ADMIN, Role.ENGINEER)
def annexure_add(inspection_id):
    i = Inspection.query.get_or_404(inspection_id)

    a = Annexure(
        inspection_id=inspection_id,
        sno=(int(request.form.get("sno")) if request.form.get("sno") else None),
        description=request.form.get("description") or None,
        qty=(int(request.form.get("qty")) if request.form.get("qty") else None),
        manufacturer=request.form.get("manufacturer") or None,
        markings=request.form.get("markings") or None,
        yom=(int(request.form.get("yom")) if request.form.get("yom") else None),
        unit_invoice_value=(float(request.form.get("unit_invoice_value")) if request.form.get("unit_invoice_value") else None),
        total_invoice_value=(float(request.form.get("total_invoice_value")) if request.form.get("total_invoice_value") else None),
        unit_fob_price=(float(request.form.get("unit_fob_price")) if request.form.get("unit_fob_price") else None),
        unit_present_assessed_value=(float(request.form.get("unit_present_assessed_value")) if request.form.get("unit_present_assessed_value") else None),
        total_present_assessed_value=(float(request.form.get("total_present_assessed_value")) if request.form.get("total_present_assessed_value") else None),
    )

    db.session.add(a)
    db.session.commit()
    log_action("annexure_add", "inspection", inspection_id, changes={"annexure": request.form.to_dict(flat=True)})
    flash("Annexure row added.", "success")
    return redirect(url_for("inspection_detail", inspection_id=inspection_id))


@app.route("/annexure/<int:annexure_id>/delete", methods=["POST"])
@role_required(Role.ADMIN, Role.ENGINEER)
def annexure_delete(annexure_id):
    a = Annexure.query.get_or_404(annexure_id)
    inspection_id = a.inspection_id
    db.session.delete(a); db.session.commit()
    log_action("annexure_delete", "inspection", inspection_id, changes={"annexure_id": annexure_id})
    flash("Annexure row deleted.", "info")
    return redirect(url_for("inspection_detail", inspection_id=inspection_id))

# ===== Boot =====
if __name__ == "__main__":
    with app.app_context():
        db.create_all()
        _ensure_sqlite_columns()
    app.run(debug=True)