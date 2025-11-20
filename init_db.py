# --- START OF CORRECTED init_db.py ---

# Consolidate ALL imports here:
from app import create_app
from config import Config
# Import ALL models needed for the whole script (including Sale, Purchase, Account, JournalEntry)
from models import db, Product, Sale, Purchase, Account, JournalEntry, CompanyProfile, User, Customer, Supplier
import os
import json # You need this for json.dumps later in the script
from models import CompanyProfile
from passlib.hash import pbkdf2_sha256

app = create_app()
app.app_context().push()

VAT_RATE = Config.VAT_RATE

db_path = os.path.join(app.instance_path, 'app.db')
if os.path.exists(db_path):
    os.remove(db_path)

db.create_all()

from models import User, Customer, Supplier
from passlib.hash import pbkdf2_sha256
admin = User(username='admin', password_hash=pbkdf2_sha256.hash('admin123'), role='Admin')
db.session.add(admin)
c1 = Customer(name='Juan dela Cruz', tin='123-456-789', address='Manila')
s1 = Supplier(name='ABC Supplier', tin='987-654-321', address='Cebu')
db.session.add(c1); db.session.add(s1)
db.session.commit()
print('Admin user created (admin/admin123). Sample customer and supplier added.')

# seed products with cost and sale price
products = [
# ... (rest of your product list)
    dict(sku='SKU-001', name='Blue T-Shirt', sale_price=350.0, cost_price=200.0, quantity=20),
    dict(sku='SKU-002', name='Red Cap', sale_price=150.0, cost_price=80.0, quantity=35),
    dict(sku='SKU-003', name='Travel Water Bottle', sale_price=480.0, cost_price=250.0, quantity=12),
]

for p in products:
    prod = Product(sku=p['sku'], name=p['name'], sale_price=p['sale_price'], cost_price=p['cost_price'], quantity=p['quantity'])
    db.session.add(prod)

db.session.commit()
print('Initialized DB with sample products. VAT_RATE =', VAT_RATE)

profile = CompanyProfile(name="Your Company Name Inc.", business_style="Retail",
                         tin="000-000-000-00000 VAT", address="123 Business St., Makati City")
db.session.add(profile)

# Create basic chart of accounts
# REMOVE: from models import Account, JournalEntry (They are already imported at the top)
accounts = [
    ('101','Cash','Asset'),
    ('110', 'Accounts Receivable', 'Asset'),
    ('120','Inventory','Asset'),
    ('121', 'Creditable Withholding Tax', 'Asset'),
    ('201','Accounts Payable','Liability'),
    ('301','Capital','Equity'),
    ('401','Sales Revenue','Revenue'),
    ('402','Other Revenue','Revenue'),
    ('405', 'Sales Returns', 'Revenue'),
    ('501','COGS','Expense'),
    ('601','VAT Payable','Liability'),
    ('602','VAT Input','Asset'),
    ('505', 'Inventory Loss', 'Expense'), 
    ('406', 'Inventory Gain', 'Revenue'),
]
for code,name,typ in accounts:
    a = Account(code=code, name=name, type=typ)
    db.session.add(a)
db.session.commit()

print('Company profile and new accounts created.')

# Optionally create a sample journal from existing sales/purchases to populate accounts
# Sale, Purchase, and json are now defined from the imports at the top
print('Creating sample journal entries from sales/purchases...')
for s in Sale.query.all():
    je_lines = [
        dict(account_code='101', debit=s.total, credit=0), # 101: Cash
        dict(account_code='401', debit=0, credit=round(s.total - s.vat,2)), # 401: Sales Revenue
        dict(account_code='601', debit=0, credit=round(s.vat,2)), # 601: VAT Payable
    ]
    je = JournalEntry(description=f'Auto-import Sale #{s.id}', entries_json=json.dumps(je_lines))
    db.session.add(je)
for p in Purchase.query.all():
    je_lines = [
        dict(account_code='120', debit=round(p.total - p.vat,2), credit=0), # 120: Inventory
        dict(account_code='602', debit=round(p.vat,2), credit=0), # 602: VAT Input
        dict(account_code='201', debit=0, credit=round(p.total,2)), # 201: Accounts Payable
    ]
    je = JournalEntry(description=f'Auto-import Purchase #{p.id}', entries_json=json.dumps(je_lines))
    db.session.add(je)
db.session.commit()
print('Sample journal entries created.')

if not hasattr(profile, 'next_invoice_number') or profile.next_invoice_number is None:
    # If you already used OR/SI, try to pick a safe starting number:
    # start from the max of existing OR/SI counters or 1
    starting = max(getattr(profile, 'next_or_number', 1) or 1, getattr(profile, 'next_si_number', 1) or 1)
    profile.next_invoice_number = starting
    db.session.add(profile)
    db.session.commit()