# --- FIX: Import the necessary functions ---
from flask import Flask, redirect, url_for, request
# --- FIX: Import current_user ---
from flask_login import LoginManager, current_user
from routes.accounts import accounts_bp


from models import db, User, CompanyProfile, Account
from config import Config
from datetime import datetime
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from extensions import limiter
from passlib.hash import pbkdf2_sha256
from routes.void_transactions import void_bp 

def create_app():
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_object(Config)

    limiter.init_app(app)

    app.register_blueprint(accounts_bp)

    db.init_app(app)

    # --- Login Manager ---
    login_manager = LoginManager()
    # --- FIX: Point to the correct blueprint endpoint ---
    login_manager.login_view = 'core.login'
    login_manager.init_app(app)

    @login_manager.user_loader
    def load_user(user_id):
        return db.session.get(User, int(user_id))

    # --- 'money' filter ---
    @app.template_filter('money')
    def money(value):
        """Format a number as currency."""
        try:
            return f"₱{float(value):,.2f}"
        except (ValueError, TypeError):
            return "₱0.00"

    @app.before_request
    def check_setup():
        # Allow access to setup pages and static files without redirection
        if request.endpoint and request.endpoint.startswith(('core.setup', 'static')):
            return

        # If user is not authenticated and is trying to access anything else, let login handle it
        if not current_user.is_authenticated and request.endpoint != 'core.login':
             # Check for Company Profile first
            if not CompanyProfile.query.first():
                return redirect(url_for('core.setup_license'))
            # Check for Admin User next
            elif not User.query.filter_by(role='Admin').first():
                 return redirect(url_for('core.setup_license')) # Start from step 1




    # --- Context Processor ---
    @app.context_processor
    def inject_company_profile():
        """Injects company profile data into all templates."""
        company = CompanyProfile.query.first()
        return dict(company=company)

    # --- Blueprints ---
    from routes.core import core_bp
    from routes.ar_ap import ar_ap_bp
    from routes.reports import reports_bp
    from routes.users import user_bp
    from routes.consignment import consignment_bp
    from routes.void_transactions import void_bp


    app.register_blueprint(core_bp)
    app.register_blueprint(ar_ap_bp)
    app.register_blueprint(reports_bp)
    app.register_blueprint(user_bp)
    app.register_blueprint(consignment_bp)
    app.register_blueprint(void_bp)

    return app

def seed_essential_data(app):
    """Seeds essential data (Admin user and COA) if the database is empty."""
    
    # Define the Chart of Accounts list here
    accounts_to_seed = [
        ('101','Cash','Asset'),
        ('102','Petty Cash','Asset'),
        ('110', 'Accounts Receivable', 'Asset'),
        ('120','Inventory','Asset'),
        ('121', 'Creditable Withholding Tax', 'Asset'),
        ('132', 'Consignment Goods on Hand', 'Asset'),
        ('201','Accounts Payable','Liability'),
        ('220', 'Consignment Payable', 'Liability'), 
        ('301','Capital','Equity'),
        ('302', 'Opening Balance Equity', 'Equity'),
        ('401','Sales Revenue','Revenue'),
        ('402','Other Revenue','Revenue'),
        ('405', 'Sales Returns', 'Revenue'),
        ('407', 'Discounts Allowed', 'Expense'),
        ('408', 'Consignment Commission Revenue', 'Revenue'),
        ('501','COGS','Expense'),
        ('601','VAT Payable','Liability'),
        ('602','VAT Input','Asset'),
        ('505', 'Inventory Loss', 'Expense'), 
        ('406', 'Inventory Gain', 'Revenue'),
        ('510', 'Rent Expense', 'Expense'),
        ('511', 'Utilities Expense', 'Expense'),
        ('512', 'Communication Expense', 'Expense'),
        ('520', 'Salaries and Wages', 'Expense'),
        ('521', 'Employee Benefits', 'Expense'),
        ('530', 'Repairs and Maintenance', 'Expense'),
    ]

    with app.app_context():
        # Check 1: Check for existing accounts
        if Account.query.count() == 0:
            print("🌱 Seeding Chart of Accounts...")
            try:
                for code, name, typ in accounts_to_seed:
                    a = Account(code=code, name=name, type=typ)
                    db.session.add(a)
                db.session.commit()
                print("✅ Chart of Accounts seeded.")
            except Exception as e:
                db.session.rollback()
                print(f"❌ Error seeding COA: {e}")
        

if __name__ == '__main__':
    app = create_app()
    with app.app_context():
        # 1. Create all tables
        db.create_all()
        
        # 2. Seed the essential data (Pass the app object to the function)
        seed_essential_data(app)
        
    app.run(debug=True)