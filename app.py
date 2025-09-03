from flask import (
    Flask, render_template, request, redirect, url_for, flash, session
)
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import func, or_
from datetime import datetime, timedelta
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = "dev-secret"  # change in prod
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///ims.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db = SQLAlchemy(app)

# ===== Enums =====
class Role:
    ADMIN = "ADMIN"
    ENGINEER = "ENGINEER"
    ACCOUNTANT = "ACCOUNTANT"
    ALL = [ADMIN, ENGINEER, ACCOUNTANT]

class InspectionStatus:
    PENDING = "PENDING"
    COMPLETED = "COMPLETED"
    INVOICED = "INVOICED"
    REPORT_GENERATED = "REPORT_GENERATED"
    ALL = [PENDING, COMPLETED, INVOICED, REPORT_GENERATED]

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
    date = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    client_id = db.Column(db.Integer, db.ForeignKey("client.id"))
    client = db.relationship("Client")
    location = db.Column(db.String(200))
    asset = db.Column(db.String(200))
    status = db.Column(db.String(40), default=InspectionStatus.PENDING)
    engineer_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    engineer = db.relationship("User")
    cha_id = db.Column(db.Integer, db.ForeignKey("cha.id"))
    cha = db.relationship("CHA")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class Report(db.Model):
    __tablename__ = "report"
    id = db.Column(db.Integer, primary_key=True)
    inspection_id = db.Column(db.Integer, db.ForeignKey("inspection.id"), unique=True)
    inspection = db.relationship("Inspection", backref=db.backref("report", uselist=False))
    status = db.Column(db.String(20), default=ReportStatus.DRAFT)
    body = db.Column(db.Text)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow)

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

@app.context_processor
def inject_globals():
    return dict(
        Role=Role, InspectionStatus=InspectionStatus, InvoiceStatus=InvoiceStatus,
        ReportStatus=ReportStatus, CommissionStatus=CommissionStatus, now=datetime.utcnow, User=User
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
    if date_from: qry = qry.filter(Inspection.date >= datetime.fromisoformat(date_from))
    if date_to:   qry = qry.filter(Inspection.date <= datetime.fromisoformat(date_to))
    if status:    qry = qry.filter(Inspection.status == status)
    if cha_id:    qry = qry.filter(Inspection.cha_id == int(cha_id))
    if eng_id:    qry = qry.filter(Inspection.engineer_id == int(eng_id))
    if q:
        like = f"%{q}%"
        qry = qry.join(Client).filter(or_(Client.name.ilike(like), Inspection.location.ilike(like), Inspection.asset.ilike(like)))

    inspections = qry.order_by(Inspection.date.desc()).all()
    groups = {s: [] for s in InspectionStatus.ALL}
    for i in inspections: groups[i.status].append(i)

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
        or_(Client.name.ilike(like), Inspection.location.ilike(like), Inspection.asset.ilike(like))
    ).all()
    clients = Client.query.filter(Client.name.ilike(like)).all()
    invoices = Invoice.query.join(Inspection).join(Client).filter(Client.name.ilike(like)).all()
    return render_template("search_results.html", q=q, inspections=inspections, clients=clients, invoices=invoices)

# ===== Inspections =====
# UPDATED: allow ADMIN and ENGINEER to create; default engineer to self for ENGINEER
@app.route("/inspections/new", methods=["POST"])
@role_required(Role.ADMIN, Role.ENGINEER)
def inspection_create():
    i = Inspection(
        date=datetime.fromisoformat(request.form["date"]),
        client_id=int(request.form["client_id"]),
        location=request.form.get("location",""),
        asset=request.form.get("asset",""),
        status=request.form.get("status", InspectionStatus.PENDING),
        engineer_id=(
            int(request.form["engineer_id"])
            if request.form.get("engineer_id")
            else session.get("user_id") if session.get("role") == Role.ENGINEER else None
        ),
        cha_id=int(request.form["cha_id"]) if request.form.get("cha_id") else None
    )
    db.session.add(i); db.session.commit()
    flash("Inspection created.", "success")
    return redirect(url_for("inspection_detail", inspection_id=i.id))

@app.route("/inspections/<int:inspection_id>")
@role_required(Role.ADMIN, Role.ENGINEER, Role.ACCOUNTANT)
def inspection_detail(inspection_id):
    i = Inspection.query.get_or_404(inspection_id)
    rep = Report.query.filter_by(inspection_id=inspection_id).first()
    inv = Invoice.query.filter_by(inspection_id=inspection_id).first()
    return render_template("inspection_detail.html", i=i, rep=rep, inv=inv)

# NEW: edit inspection (ADMIN or assigned ENGINEER)
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
        if session.get("role") == Role.ADMIN:
            i.status = request.form.get("status", i.status)
            i.engineer_id = int(request.form["engineer_id"]) if request.form.get("engineer_id") else i.engineer_id
            i.cha_id = int(request.form["cha_id"]) if request.form.get("cha_id") else None
        db.session.commit()
        flash("Inspection updated.", "success")
        return redirect(url_for("inspection_detail", inspection_id=inspection_id))

    clients = Client.query.order_by(Client.name.asc()).all()
    chas = CHA.query.order_by(CHA.name.asc()).all()
    engineers = User.query.filter(User.role == Role.ENGINEER).all()
    return render_template("inspection_edit.html", i=i, clients=clients, chas=chas, engineers=engineers)

# NEW: admin delete inspection (cleans dependents)
@app.route("/inspections/<int:inspection_id>/delete", methods=["POST"])
@role_required(Role.ADMIN)
def inspection_delete(inspection_id):
    i = Inspection.query.get_or_404(inspection_id)
    Report.query.filter_by(inspection_id=inspection_id).delete()
    Invoice.query.filter_by(inspection_id=inspection_id).delete()
    Commission.query.filter_by(inspection_id=inspection_id).delete()
    db.session.delete(i)
    db.session.commit()
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
    flash("Status updated.", "success")
    return redirect(url_for("inspection_detail", inspection_id=inspection_id))

@app.route("/users/assign/<int:inspection_id>", methods=["POST"])
@role_required(Role.ADMIN)
def assign_engineer(inspection_id):
    i = Inspection.query.get_or_404(inspection_id)
    i.engineer_id = int(request.form["engineer_id"]) if request.form.get("engineer_id") else None
    db.session.commit()
    flash("Engineer assigned.", "success")
    return redirect(url_for("inspection_detail", inspection_id=inspection_id))

# ===== Reports =====
@app.route("/reports/<int:inspection_id>/edit", methods=["GET","POST"])
@role_required(Role.ADMIN, Role.ENGINEER)
def report_edit(inspection_id):
    i = Inspection.query.get_or_404(inspection_id)
    rep = ensure_report(inspection_id)
    if request.method == "POST":
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
            i.status = InspectionStatus.REPORT_GENERATED
        else:
            rep.status = ReportStatus.DRAFT
        rep.updated_at = datetime.utcnow()
        db.session.commit()
        flash("Report saved.", "success")
        return redirect(url_for("inspection_detail", inspection_id=inspection_id))
    return render_template("report_edit.html", i=i, rep=rep)

@app.route("/reports/<int:inspection_id>/view")
@role_required(Role.ADMIN, Role.ENGINEER, Role.ACCOUNTANT)
def report_view(inspection_id):
    rep = Report.query.filter_by(inspection_id=inspection_id).first_or_404()
    i = Inspection.query.get_or_404(inspection_id)
    return render_template("report_view.html", i=i, rep=rep)

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

@app.route("/commissions/generate/<int:inspection_id>", methods=["POST"])
@role_required(Role.ADMIN, Role.ACCOUNTANT)
def commission_generate(inspection_id):
    i = Inspection.query.get_or_404(inspection_id)
    if not i.cha:
        flash("No CHA linked.", "warning"); return redirect(url_for("inspection_detail", inspection_id=inspection_id))
    rate = i.cha.commission_rate or 0.0
    fee = i.invoice.fee if i.invoice else 0.0
    amount = round(fee * rate / 100.0, 2)
    com = Commission.query.filter_by(inspection_id=inspection_id).first()
    if not com:
        com = Commission(inspection_id=inspection_id, cha_id=i.cha.id, amount=amount, status=CommissionStatus.DUE)
        db.session.add(com)
    else:
        com.amount = amount
    db.session.commit()
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

# NEW: inline amount update (Admin/Accountant)
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
        Inspection.status.in_([InspectionStatus.COMPLETED, InspectionStatus.REPORT_GENERATED])
    ).filter(~Inspection.id.in_(db.session.query(Invoice.inspection_id))).all()
    commission_due = Commission.query.filter(Commission.status == CommissionStatus.DUE).all()
    return render_template("notifications.html",
                           overdue_reports=overdue_reports,
                           missing_invoice=missing_invoice,
                           commission_due=commission_due)

# ===== Boot =====
if __name__ == "__main__":
    with app.app_context():
        db.create_all()
    app.run(debug=True)