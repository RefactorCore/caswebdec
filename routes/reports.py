from flask import Blueprint, render_template, request, abort, Response
from flask_login import login_required
# Add CompanyProfile, Customer, Supplier, CreditMemo
from models import db, JournalEntry, Account, Sale, Purchase, Product, ARInvoice, APInvoice, CompanyProfile, Customer, Supplier, CreditMemo, Payment, SaleItem, PurchaseItem, StockAdjustment
from collections import defaultdict
import json
from sqlalchemy import func, extract, cast, Date, or_, and_
from datetime import datetime, date, timedelta
from routes.decorators import role_required
import io
import csv
from routes.utils import get_system_account_code  # add this near your other imports at top of file
from decimal import Decimal, ROUND_HALF_UP

reports_bp = Blueprint('reports', __name__, url_prefix='/reports')


def parse_date(date_str):
    """Helper to safely parse YYYY-MM-DD format strings."""
    try:
        return datetime.strptime(date_str, "%Y-%m-%d")
    except (ValueError, TypeError):
        return None

def to_decimal(value):
    """Coerce value (None, float, int, str, Decimal) -> Decimal quantized to 2dp."""
    if value is None:
        return Decimal('0.00')
    if isinstance(value, Decimal):
        return value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    try:
        d = Decimal(str(value))
    except Exception:
        return Decimal('0.00')
    return d.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

# register money/num filters at blueprint registration time to ensure templates can use them
@reports_bp.record
def _register_jinja_filters(state):
    app = state.app

    def _money_filter(value):
        """Format Decimal/number to 2-decimal string for display (no currency symbol)."""
        try:
            return format(to_decimal(value), '0.2f')
        except Exception:
            return "0.00"

    def _num_filter(value):
        """Return native float suitable for tojson / JS usage."""
        try:
            return float(to_decimal(value))
        except Exception:
            return 0.0

    app.jinja_env.filters['money'] = _money_filter
    app.jinja_env.filters['num'] = _num_filter

@reports_bp.route('/trial-balance')
@login_required
@role_required('Admin', 'Accountant')
def trial_balance():
    start_date_str = request.args.get('start_date', '')
    end_date_str = request.args.get('end_date', '')
    start_date = parse_date(start_date_str)
    end_date = parse_date(end_date_str)
    
    agg = aggregate_account_balances(start_date, end_date)
    
    tb = []
    total_debit = Decimal('0.00')
    total_credit = Decimal('0.00')
    
    for acc_code, data in agg.items():
        # EXTRACT NET FROM DATA
        val = data['net'] 
        
        acc_details = Account.query.filter_by(code=acc_code).first()
        acc_name = acc_details.name if acc_details else f"Unknown ({acc_code})"
        
        if val >= Decimal('0.00'):
            tb.append({'code': acc_code, 'name': acc_name, 'debit': val, 'credit': Decimal('0.00')})
            total_debit += val
        else:
            tb.append({'code': acc_code, 'name': acc_name, 'debit': Decimal('0.00'), 'credit': -val})
            total_credit += -val
            
    tb.sort(key=lambda x: x['code'])
    
    return render_template('trial_balance.html', tb=tb, 
                           total_debit=total_debit, total_credit=total_credit,
                           start_date=start_date_str, end_date=end_date_str)


@reports_bp.route('/ledger/<code>')
@login_required
@role_required('Admin', 'Accountant')
def ledger(code):
    account = Account.query.filter_by(code=code).first_or_404()
    
    # --- MODIFIED: Get dates from URL ---
    start_date_str = request.args.get('start_date', '')
    end_date_str = request.args.get('end_date', '')

    start_date = parse_date(start_date_str)
    end_date = parse_date(end_date_str)
    
    # --- MODIFIED: Filter the Journal Entry query by date ---
    query = JournalEntry.query.order_by(JournalEntry.created_at)
    if start_date:
        query = query.filter(JournalEntry.created_at >= start_date)
    if end_date:
        end_date_inclusive = end_date + timedelta(days=1)
        query = query.filter(JournalEntry.created_at < end_date_inclusive)
    
    rows = []
    balance = Decimal('0.00')
    
    # --- MODIFIED: Get running balance *before* the start date (if one exists) ---
    if start_date:
        opening_balance_query = JournalEntry.query.filter(JournalEntry.created_at < start_date)
        for je in opening_balance_query.all():
            for line in je.entries():
                if line.get('account_code') == code:
                    debit = to_decimal(line.get('debit', 0))
                    credit = to_decimal(line.get('credit', 0))
                    balance += debit - credit
        
        # Add the opening balance as the first row
        rows.append({'date': start_date, 'desc': 'Opening Balance', 'debit': Decimal('0.00'), 'credit': Decimal('0.00'), 'balance': balance})


    # --- MODIFIED: Loop through the *filtered* query ---
    for je in query.all():
        for line in je.entries():
            if line.get('account_code') == code:
                debit = to_decimal(line.get('debit', 0))
                credit = to_decimal(line.get('credit', 0))
                balance += debit - credit
                rows.append({'date': je.created_at, 'desc': je.description, 'debit': debit, 'credit': credit, 'balance': balance})
    
    # --- MODIFIED: Pass dates back to the template ---
    return render_template('ledger.html', account=account, rows=rows, 
                           balance=balance, start_date=start_date_str, end_date=end_date_str)


@reports_bp.route('/balance-sheet')
@login_required
@role_required('Admin', 'Accountant')
def balance_sheet():
    default_end_date = datetime.utcnow().strftime('%Y-%m-%d')
    end_date_str = request.args.get('end_date', default_end_date)
    end_date = parse_date(end_date_str)
    
    agg = aggregate_account_balances(start_date=None, end_date=end_date)
    
    assets, liabilities, equity = [], [], []
    
    for acc_code, data in agg.items():
        # EXTRACT NET
        bal = data['net']
        
        acct_rec = Account.query.filter_by(code=acc_code).first()
        if not acct_rec: continue  
            
        acc_name = acct_rec.name
        acc_type = acct_rec.type

        if acc_type == 'Asset':
            assets.append((acc_name, bal))
        elif acc_type == 'Liability':
            liabilities.append((acc_name, -bal))
        elif acc_type == 'Equity':
            equity.append((acc_name, -bal))

    # Calculate Net Income
    net_income = Decimal('0.00')
    # Re-run aggregation just for P&L logic
    is_agg = aggregate_account_balances(start_date=None, end_date=end_date)
    revenues = {code: -data['net'] for code, data in is_agg.items() if Account.query.filter_by(code=code, type='Revenue').first()}
    expenses = {code: data['net'] for code, data in is_agg.items() if Account.query.filter_by(code=code, type='Expense').first()}
    
    total_revenue = sum(revenues.values()) if revenues else Decimal('0.00')
    total_expense = sum(expenses.values()) if expenses else Decimal('0.00')
    net_income = total_revenue - total_expense
    
    equity.append(("Current Period Net Income", net_income))

    total_assets = sum(b for a, b in assets) if assets else Decimal('0.00')
    total_liabilities = sum(b for a, b in liabilities) if liabilities else Decimal('0.00')
    total_equity = sum(b for a, b in equity) if equity else Decimal('0.00')
    
    return render_template('balance_sheet.html', assets=assets, liabilities=liabilities, equity=equity,
                           total_assets=total_assets, total_liabilities=total_liabilities, total_equity=total_equity,
                           end_date=end_date_str)


@reports_bp.route('/income-statement')
@login_required
@role_required('Admin', 'Accountant')
def income_statement():
    start_date_str = request.args.get('start_date', '')
    end_date_str = request.args.get('end_date', '')
    start_date = parse_date(start_date_str)
    end_date = parse_date(end_date_str)

    agg = aggregate_account_balances(start_date, end_date)
    
    revenues, expenses = {}, {}
    cogs_amount = Decimal('0.00')

    try:
        cogs_code = get_system_account_code('COGS')
    except Exception:
        cogs_code = None

    for acc_code, data in agg.items():
        # EXTRACT NET
        bal = data['net']
        
        acct_rec = Account.query.filter_by(code=acc_code).first()
        if not acct_rec: continue

        if acct_rec.type == 'Revenue':
            revenues[acct_rec.name] = -bal
        elif acct_rec.type == 'Expense':
            if cogs_code and acc_code == cogs_code:
                cogs_amount += bal
            elif acct_rec.name.lower() in ('cogs', 'cost of goods sold') and not cogs_code:
                cogs_amount += bal
            else:
                expenses[acct_rec.name] = bal

    total_revenue = sum(revenues.values()) if revenues else Decimal('0.00')
    total_expense = sum(expenses.values()) if expenses else Decimal('0.00')
    gross_profit = total_revenue - cogs_amount
    net_income = gross_profit - total_expense

    return render_template('income_statement.html',
                           revenues=revenues, expenses=expenses, cogs=cogs_amount,
                           total_revenue=total_revenue, total_expense=total_expense,
                           gross_profit=gross_profit, net_income=net_income,
                           start_date=start_date_str, end_date=end_date_str)


@reports_bp.route('/vat-report')
@login_required
@role_required('Admin', 'Accountant')
def vat_report():
    # --- Get dates from URL ---
    start_date_str = request.args.get('start_date', '')
    end_date_str = request.args.get('end_date', '')

    start_date = parse_date(start_date_str)
    end_date = parse_date(end_date_str)

    # --- OUTPUT VAT ---
    output_vat_query = db.session.query(
        func.sum(Sale.vat)
    ).filter(
        Sale.is_vatable == True,
        Sale.voided_at == None
    )
    
    # --- INPUT VAT ---
    input_vat_query = db.session.query(
        func.sum(Purchase.vat)
    ).filter(
        Purchase.is_vatable == True,
        Purchase.voided_at == None
    )

    # --- NON-VAT SALES ---
    # Query for sales that are (is_vatable=False) OR (is_vatable=True AND vat=0)
    non_vat_sales_query = db.session.query(
        func.sum(Sale.total)
    ).filter(
        or_(
            Sale.is_vatable == False,
            and_(
                Sale.is_vatable == True,
                Sale.vat == 0.00
            )
        ),
        Sale.voided_at == None
    )

    # --- NON-VAT AR INVOICES ---
    non_vat_ar_query = db.session.query(
        func.sum(ARInvoice.total)
    ).filter(
        or_(
            ARInvoice.is_vatable == False,
            and_(
                ARInvoice.is_vatable == True,
                ARInvoice.vat == 0.00
            )
        ),
        ARInvoice.voided_at == None
    )

    # --- NON-VAT PURCHASES ---
    non_vat_purchases_query = db.session.query(
        func.sum(Purchase.total)
    ).filter(
        Purchase.is_vatable == False,
        Purchase.voided_at == None
    )

    # --- NON-VAT AP INVOICES ---
    non_vat_ap_query = db.session.query(
        func.sum(APInvoice.total)
    ).filter(
        APInvoice.is_vatable == False,
        APInvoice.voided_at == None
    )

    # Apply date filters
    if start_date:
        start_datetime = datetime.combine(start_date, datetime.min.time())
        output_vat_query = output_vat_query.filter(Sale.created_at >= start_datetime)
        input_vat_query = input_vat_query.filter(Purchase.created_at >= start_datetime)
        non_vat_sales_query = non_vat_sales_query.filter(Sale.created_at >= start_datetime)
        non_vat_ar_query = non_vat_ar_query.filter(ARInvoice.created_at >= start_datetime)
        non_vat_purchases_query = non_vat_purchases_query.filter(Purchase.created_at >= start_datetime)
        non_vat_ap_query = non_vat_ap_query.filter(APInvoice.created_at >= start_datetime)

    if end_date:
        end_datetime = datetime.combine(end_date, datetime.max.time())
        output_vat_query = output_vat_query.filter(Sale.created_at <= end_datetime)
        input_vat_query = input_vat_query.filter(Purchase.created_at <= end_datetime)
        non_vat_sales_query = non_vat_sales_query.filter(Sale.created_at <= end_datetime)
        non_vat_ar_query = non_vat_ar_query.filter(ARInvoice.created_at <= end_datetime)
        non_vat_purchases_query = non_vat_purchases_query.filter(Purchase.created_at <= end_datetime)
        non_vat_ap_query = non_vat_ap_query.filter(APInvoice.created_at <= end_datetime)

    # Execute all queries
    total_output_vat = to_decimal(output_vat_query.scalar())
    total_input_vat = to_decimal(input_vat_query.scalar())
    total_non_vat_sales = to_decimal(non_vat_sales_query.scalar())
    total_non_vat_ar = to_decimal(non_vat_ar_query.scalar())
    total_non_vat_purchases = to_decimal(non_vat_purchases_query.scalar())
    total_non_vat_ap = to_decimal(non_vat_ap_query.scalar())

    # Combine totals
    vat_payable = total_output_vat - total_input_vat
    total_non_vat_sales_combined = total_non_vat_sales + total_non_vat_ar
    total_non_vat_purchases_combined = total_non_vat_purchases + total_non_vat_ap

    # ✅ FIX: The variable names here now match vat_report.html
    return render_template(
        'vat_report.html',
        total_output_vat=total_output_vat,
        total_input_vat=total_input_vat,
        vat_payable=vat_payable,
        total_nonvat_sales=total_non_vat_sales_combined,  # <--- No underscore
        total_nonvat_purchases=total_non_vat_purchases_combined, # <--- No underscore
        start_date=start_date_str,
        end_date=end_date_str
    )


@reports_bp.route('/sales')
@login_required
def sales():
    sales = Sale.query.order_by(Sale.created_at.desc()).all()
    return render_template('sales.html', sales=sales)


@reports_bp.route('/purchases')
@role_required('Admin', 'Accountant')
@login_required
def purchases():
    purchases = Purchase.query.order_by(Purchase.created_at.desc()).all()
    return render_template('purchases.html', purchases=purchases)

@reports_bp.route('/vat-return')
@login_required
@role_required('Admin', 'Accountant')
def vat_return():
    """Generates data for BIR Form 2550M/Q."""
    month = request.args.get('month', datetime.now().strftime('%Y-%m'))
    year, month_num = map(int, month.split('-'))

    # Output Tax (from Sales)
    sales_in_month = ARInvoice.query.filter(
        extract('year', ARInvoice.date) == year,
        extract('month', ARInvoice.date) == month_num
    ).all()

    # --- ADD THIS QUERY ---
    cash_sales_in_month = Sale.query.filter(
        extract('year', Sale.created_at) == year,
        extract('month', Sale.created_at) == month_num,
        Sale.is_vatable == True
    ).all()
    # --- END ADD ---
    
    # Adjustments to Output Tax (from Credit Memos)
    returns_in_month = CreditMemo.query.filter(
        extract('year', CreditMemo.date) == year,
        extract('month', CreditMemo.date) == month_num
    ).all()

    # --- UPDATE THESE TWO LINES ---
    total_sales_net = sum((to_decimal(s.total) - to_decimal(s.vat)) for s in sales_in_month) + \
                      sum((to_decimal(s.total) - to_decimal(s.vat)) for s in cash_sales_in_month)
    total_output_vat = sum(to_decimal(s.vat) for s in sales_in_month) + \
                       sum(to_decimal(s.vat) for s in cash_sales_in_month)
    # --- END UPDATE ---
    
    total_returns_net = sum(to_decimal(cm.amount_net) for cm in returns_in_month)
    total_returns_vat = sum(to_decimal(cm.vat) for cm in returns_in_month)

    # Input Tax (from Purchases)
    purchases_in_month = APInvoice.query.filter(
        extract('year', APInvoice.date) == year,
        extract('month', APInvoice.date) == month_num
    ).all()

    # --- ADD THIS QUERY ---
    cash_purchases_in_month = Purchase.query.filter(
        extract('year', Purchase.created_at) == year,
        extract('month', Purchase.created_at) == month_num,
        Purchase.is_vatable == True
    ).all()
    # --- END ADD ---
    
    # --- UPDATE THESE TWO LINES ---
    total_purchases_net = sum((to_decimal(p.total) - to_decimal(p.vat)) for p in purchases_in_month) + \
                          sum((to_decimal(p.total) - to_decimal(p.vat)) for p in cash_purchases_in_month)
    total_input_vat = sum(to_decimal(p.vat) for p in purchases_in_month) + \
                      sum(to_decimal(p.vat) for p in cash_purchases_in_month)
    # --- END UPDATE ---
    
    # Calculation
    net_sales = total_sales_net - total_returns_net
    net_output_vat = total_output_vat - total_returns_vat
    vat_payable = net_output_vat - total_input_vat

    return render_template('vat_return.html', month=month,
                           net_sales=net_sales, net_output_vat=net_output_vat,
                           total_purchases_net=total_purchases_net, total_input_vat=total_input_vat,
                           vat_payable=vat_payable)


@reports_bp.route('/summary-list-sales')
@login_required
@role_required('Admin', 'Accountant')
def summary_list_sales():
    """Generates Summary List of Sales (SLS)."""
    month = request.args.get('month', datetime.now().strftime('%Y-%m'))
    year, month_num = map(int, month.split('-'))

    sales = db.session.query(
        Customer.tin,
        Customer.name,
        func.sum(ARInvoice.total - ARInvoice.vat).label('net_sales'),
        func.sum(ARInvoice.vat).label('output_vat')
    ).join(Customer, ARInvoice.customer_id == Customer.id).filter(
        extract('year', ARInvoice.date) == year,
        extract('month', ARInvoice.date) == month_num
    ).group_by(Customer.tin, Customer.name).order_by(Customer.name).all()

    grand_total_net = sum(to_decimal(s.net_sales) for s in sales)
    grand_total_vat = sum(to_decimal(s.output_vat) for s in sales)

    return render_template('sls.html', month=month, sales=sales,
                           grand_total_net=grand_total_net, grand_total_vat=grand_total_vat)


@reports_bp.route('/summary-list-purchases')
@login_required
@role_required('Admin', 'Accountant')
def summary_list_purchases():
    """Generates Summary List of Purchases (SLP)."""
    month = request.args.get('month', datetime.now().strftime('%Y-%m'))
    year, month_num = map(int, month.split('-'))

    purchases = db.session.query(
        Supplier.tin,
        Supplier.name,
        func.sum(APInvoice.total - APInvoice.vat).label('net_purchases'),
        func.sum(APInvoice.vat).label('input_vat')
    ).join(Supplier, APInvoice.supplier_id == Supplier.id).filter(
        extract('year', APInvoice.date) == year,
        extract('month', APInvoice.date) == month_num
    ).group_by(Supplier.tin, Supplier.name).order_by(Supplier.name).all()

    grand_total_net = sum(to_decimal(p.net_purchases) for p in purchases)
    grand_total_vat = sum(to_decimal(p.input_vat) for p in purchases)

    return render_template('slp.html', month=month, purchases=purchases,
                           grand_total_net=grand_total_net, grand_total_vat=grand_total_vat)

# (insert into reports.py) - replace the existing form_2307_report function

@reports_bp.route('/form-2307-report')
@login_required
@role_required('Admin', 'Accountant')
def form_2307_report():
    """Generates data for BIR Form 2307 from payments received."""
    customers = Customer.query.order_by(Customer.name).all()
    selected_customer_id = request.args.get('customer_id', type=int)
    month = request.args.get('month', datetime.now().strftime('%Y-%m'))
    year, month_num = map(int, month.split('-'))
    
    payments_list = []
    customer = None
    if selected_customer_id:
        customer = Customer.query.get(selected_customer_id)
        payments_query = Payment.query.join(ARInvoice, Payment.ref_id == ARInvoice.id).filter(
            Payment.ref_type == 'AR',
            Payment.wht_amount > 0,
            ARInvoice.customer_id == selected_customer_id,
            extract('year', Payment.date) == year,
            extract('month', Payment.date) == month_num
        )
        payments = payments_query.all()

        # Pre-compute Decimal-safe fields for the template (gross = amount / 1.12)
        DIV_VAT = Decimal('1.12')
        for p in payments:
            amt = to_decimal(p.amount)
            # gross amount (net of 12% VAT) -> amount / 1.12
            try:
                gross = (amt / DIV_VAT).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            except Exception:
                gross = Decimal('0.00')
            wht = to_decimal(p.wht_amount)
            payments_list.append({
                'date': p.date,
                'ref_id': p.ref_id,
                'amount': amt,
                'gross': gross,
                'wht_amount': wht
            })

    company = CompanyProfile.query.first()

    # Totals for display (Decimal)
    total_gross = sum((p['gross'] for p in payments_list), Decimal('0.00')) if payments_list else Decimal('0.00')
    total_wht = sum((p['wht_amount'] for p in payments_list), Decimal('0.00')) if payments_list else Decimal('0.00')
    
    return render_template('form_2307_report.html', customers=customers, 
                           selected_customer_id=selected_customer_id,
                           month=month, payments=payments_list, customer=customer, company=company,
                           total_gross=total_gross, total_wht=total_wht)

@reports_bp.route('/ar-aging')
@login_required
@role_required('Admin', 'Accountant')
def ar_aging():
    """Generates an Accounts Receivable Aging report."""
    today = date.today()
    invoices = ARInvoice.query.filter(
        (ARInvoice.total - ARInvoice.paid) > Decimal('0.01'),
        ARInvoice.voided_at.is_(None) # Explicitly exclude voided invoices
    ).all()
    
    aging_data = {
        'current': [], '1-30': [], '31-60': [], '61-90': [], '91+': []
    }
    totals = { 'current': Decimal('0.00'), '1-30': Decimal('0.00'), '31-60': Decimal('0.00'), '61-90': Decimal('0.00'), '91+': Decimal('0.00'), 'total': Decimal('0.00') }

    for inv in invoices:
        age_date = inv.due_date.date() if inv.due_date else inv.date.date()
        age = (today - age_date).days
        balance = to_decimal(inv.total) - to_decimal(inv.paid)
        totals['total'] += balance

        if age <= 0:
            aging_data['current'].append(inv)
            totals['current'] += balance
        elif 1 <= age <= 30:
            aging_data['1-30'].append(inv)
            totals['1-30'] += balance
        elif 31 <= age <= 60:
            aging_data['31-60'].append(inv)
            totals['31-60'] += balance
        elif 61 <= age <= 90:
            aging_data['61-90'].append(inv)
            totals['61-90'] += balance
        else:
            aging_data['91+'].append(inv)
            totals['91+'] += balance
            
    return render_template('ar_aging.html', aging_data=aging_data, totals=totals)

@reports_bp.route('/ap-aging')
@login_required
@role_required('Admin', 'Accountant')
def ap_aging():
    """Generates an Accounts Payable Aging report."""
    today = date.today()
    invoices = APInvoice.query.filter(
        (APInvoice.total - APInvoice.paid) > Decimal('0.01'),
        APInvoice.voided_at.is_(None)
    ).all()

    aging_data = {
        'current': [], '1-30': [], '31-60': [], '61-90': [], '91+': []
    }
    totals = { 'current': Decimal('0.00'), '1-30': Decimal('0.00'), '31-60': Decimal('0.00'), '61-90': Decimal('0.00'), '91+': Decimal('0.00'), 'total': Decimal('0.00') }

    for inv in invoices:
        age_date = inv.due_date.date() if inv.due_date else inv.date.date()
        age = (today - age_date).days
        balance = to_decimal(inv.total) - to_decimal(inv.paid)
        totals['total'] += balance

        if age <= 0:
            aging_data['current'].append(inv)
            totals['current'] += balance
        elif 1 <= age <= 30:
            aging_data['1-30'].append(inv)
            totals['1-30'] += balance
        elif 31 <= age <= 60:
            aging_data['31-60'].append(inv)
            totals['31-60'] += balance
        elif 61 <= age <= 90:
            aging_data['61-90'].append(inv)
            totals['61-90'] += balance
        else:
            aging_data['91+'].append(inv)
            totals['91+'] += balance

    return render_template('ap_aging.html', aging_data=aging_data, totals=totals)

# Replace the existing stock_card route with this implementation
# Replace the existing stock_card route with this implementation
@reports_bp.route('/stock-card/<int:product_id>')
@login_required
@role_required('Admin', 'Accountant')
def stock_card(product_id):
    """Generates an inventory stock card for a specific product."""
    # Local imports to avoid circulars at module import time
    from models import ARInvoiceItem, InventoryMovementItem, InventoryMovement, Branch

    product = Product.query.get_or_404(product_id)

    # Gather transaction sources
    sales = SaleItem.query.join(Sale).filter(SaleItem.product_id == product.id).all()
    purchases = PurchaseItem.query.filter_by(product_id=product.id).all()
    adjustments = StockAdjustment.query.filter_by(product_id=product.id).all()
    ar_invoice_items = ARInvoiceItem.query.join(ARInvoice).filter(ARInvoiceItem.product_id == product.id).all()

    # Inventory movements (receive/transfer)
    movement_items = InventoryMovementItem.query.join(InventoryMovement).filter(InventoryMovementItem.product_id == product.id).all()

    transactions = []

    # POS Sales
    for s in sales:
        is_voided = getattr(s.sale, 'voided_at', None) is not None
        transactions.append({
            'date': s.sale.created_at,
            'type': '[VOID] Sale (POS)' if is_voided else 'Sale (POS)',
            'ref_id': s.sale_id,
            'qty_in': s.qty if is_voided else 0,
            'qty_out': 0 if is_voided else s.qty,
            'cost': getattr(s, 'cogs', None) or product.cost_price,
            'voided': is_voided,
            'void_reason': s.sale.void_reason if is_voided else None
        })

    # Purchases
    for p in purchases:
        is_voided = getattr(p.purchase, 'voided_at', None) is not None
        transactions.append({
            'date': p.purchase.created_at,
            'type': '[VOID] Purchase' if is_voided else 'Purchase',
            'ref_id': p.purchase_id,
            'qty_in': 0 if is_voided else p.qty,
            'qty_out': p.qty if is_voided else 0,
            'cost': p.unit_cost,
            'voided': is_voided,
            'void_reason': p.purchase.void_reason if is_voided else None
        })

    # Stock Adjustments
    for adj in adjustments:
        is_voided = adj.voided_at is not None
        if is_voided:
            # reverse direction on void
            qty_in = abs(adj.quantity_changed) if adj.quantity_changed < 0 else 0
            qty_out = adj.quantity_changed if adj.quantity_changed > 0 else 0
            transaction_type = f'[VOID] Adjustment ({adj.reason})'
        else:
            qty_in = adj.quantity_changed if adj.quantity_changed > 0 else 0
            qty_out = abs(adj.quantity_changed) if adj.quantity_changed < 0 else 0
            transaction_type = f'Adjustment ({adj.reason})'

        transactions.append({
            'date': adj.created_at,
            'type': transaction_type,
            'ref_id': adj.id,
            'qty_in': qty_in,
            'qty_out': qty_out,
            'cost': product.cost_price,
            'voided': is_voided,
            'void_reason': adj.void_reason if is_voided else None
        })

    # Billing / AR invoice items (treated like sales)
    for ar_item in ar_invoice_items:
        is_voided = getattr(ar_item.ar_invoice, 'voided_at', None) is not None
        invoice_num = ar_item.ar_invoice.invoice_number or f"AR-{ar_item.ar_invoice.id}"
        transactions.append({
            'date': ar_item.ar_invoice.date,
            'type': f'[VOID] Billing Invoice - {invoice_num}' if is_voided else f'Billing Invoice - {invoice_num}',
            'ref_id': ar_item.ar_invoice_id,
            'qty_in': ar_item.qty if is_voided else 0,
            'qty_out': 0 if is_voided else ar_item.qty,
            'cost': (to_decimal(ar_item.cogs) / Decimal(ar_item.qty)) if (getattr(ar_item, 'cogs', None) and ar_item.qty) else product.cost_price,
            'voided': is_voided,
            'void_reason': ar_item.ar_invoice.void_reason if is_voided else None
        })

    # Inventory Movements (receive / transfer)
    # Inventory Movements (receive / transfer)
    for mi in movement_items:
        m = mi.movement
        m_date = getattr(m, 'created_at', datetime.utcnow())
        is_voided = False  # InventoryMovement model currently doesn't have void fields; adapt if you add them

        if m.movement_type == 'receive':
            # Receive -> incoming row only
            transactions.append({
                'date': m_date,
                'type': f'Movement: Receive (#{m.id})',
                'ref_id': m.id,
                'qty_in': mi.quantity,
                'qty_out': 0,
                'cost': mi.unit_cost,
                'voided': is_voided,
                'void_reason': None,
                'from_branch_id': m.from_branch_id,
                'to_branch_id': m.to_branch_id
            })

        elif m.movement_type == 'transfer':
            # Transfer -> outgoing row only (Qty Out). Do NOT set qty_in here.
            # Use from_branch if available to indicate the source of the outflow.
            from_branch_name = m.from_branch.name if getattr(m, 'from_branch', None) else None
            transactions.append({
                'date': m_date,
                'type': f'Transfer Out (#{m.id}) {from_branch_name or ""}',
                'ref_id': m.id,
                'qty_in': 0,
                'qty_out': mi.quantity,
                'cost': mi.unit_cost,
                'voided': is_voided,
                'void_reason': None,
                'from_branch_id': m.from_branch_id,
                'to_branch_id': m.to_branch_id
            })

        else:
            # Fallback: unknown movement type — record as incoming by default
            transactions.append({
                'date': m_date,
                'type': f'Movement: {m.movement_type or "Unknown"} (#{m.id})',
                'ref_id': m.id,
                'qty_in': mi.quantity,
                'qty_out': 0,
                'cost': mi.unit_cost,
                'voided': is_voided,
                'void_reason': None,
                'from_branch_id': m.from_branch_id,
                'to_branch_id': m.to_branch_id
            })

    # Sort ascending by date (oldest first)
    transactions.sort(key=lambda x: x['date'] or datetime.utcnow())

    # Compute net delta (non-voided transactions)
    net_delta = sum((t.get('qty_in', 0) - t.get('qty_out', 0)) for t in transactions if not t.get('voided', False))

    current_quantity = product.quantity or 0
    opening_balance = int(current_quantity - net_delta)

    # Build report rows: opening balance then transactions with running balance
    report_transactions = []

    first_date = transactions[0]['date'] if transactions else datetime.utcnow()
    report_transactions.append({
        'date': first_date - timedelta(seconds=1),
        'type': 'Opening Balance',
        'ref_id': 'N/A',
        'qty_in': opening_balance if opening_balance > 0 else 0,
        'qty_out': abs(opening_balance) if opening_balance < 0 else 0,
        'cost': product.cost_price,
        'balance': opening_balance,
        'voided': False,
        'void_reason': None
    })

    running_balance = opening_balance

    for t in transactions:
        # apply transaction to running balance (voided txns are recorded as their displayed qty_in/qty_out)
        running_balance += (t.get('qty_in', 0) - t.get('qty_out', 0))
        # copy keys we need in template
        row = {
            'date': t['date'],
            'type': t['type'],
            'ref_id': t['ref_id'],
            'qty_in': t.get('qty_in', 0),
            'qty_out': t.get('qty_out', 0),
            'cost': t.get('cost', product.cost_price),
            'balance': running_balance,
            'voided': t.get('voided', False),
            'void_reason': t.get('void_reason')
        }
        report_transactions.append(row)

    return render_template('stock_card.html', product=product, transactions=report_transactions)


@reports_bp.route('/export/balance-sheet')
@login_required
@role_required('Admin', 'Accountant')
def export_balance_sheet():
    """Exports the balance sheet to CSV."""
    
    default_end_date = datetime.utcnow().strftime('%Y-%m-%d')
    end_date_str = request.args.get('end_date', default_end_date)
    end_date = parse_date(end_date_str)

    # 1. Get the new dictionary structure
    agg = aggregate_account_balances(start_date=None, end_date=end_date)
    
    assets, liabilities, equity = [], [], []

    for acc_code, data in agg.items():
        # --- FIX: Extract 'net' from the data dictionary ---
        bal = to_decimal(data['net'])
        
        acct_rec = Account.query.filter_by(code=acc_code).first()
        if not acct_rec: continue
        
        acc_name = acct_rec.name
        acc_type = acct_rec.type

        if acc_type == 'Asset':
            assets.append((acc_name, bal))
        elif acc_type == 'Liability':
            liabilities.append((acc_name, -bal))
        elif acc_type == 'Equity':
            equity.append((acc_name, -bal))

    # 2. Re-calculate Net Income using the new structure
    # We re-run aggregation to ensure we catch revenue/expenses correctly up to end_date
    is_agg_net_income = aggregate_account_balances(start_date=None, end_date=end_date)
    
    # --- FIX: Extract 'net' inside the list comprehensions ---
    revenues = {code: -to_decimal(d['net']) for code, d in is_agg_net_income.items() if Account.query.filter_by(code=code, type='Revenue').first()}
    expenses = {code: to_decimal(d['net']) for code, d in is_agg_net_income.items() if Account.query.filter_by(code=code, type='Expense').first()}

    total_revenue = sum(revenues.values(), Decimal('0.00'))
    total_expense = sum(expenses.values(), Decimal('0.00'))
    net_income = total_revenue - total_expense
    
    equity.append(("Current Period Net Income", net_income))
    
    total_assets = sum(b for a, b in assets) if assets else Decimal('0.00')
    total_liabilities = sum(b for a, b in liabilities) if liabilities else Decimal('0.00')
    total_equity = sum(b for a, b in equity) if equity else Decimal('0.00')
    total_liabilities_and_equity = total_liabilities + total_equity

    output = io.StringIO()
    writer = csv.writer(output)
    
    writer.writerow([f"Balance Sheet as of {end_date_str}", ""])
    writer.writerow([])

    writer.writerow(["ASSETS", "Amount"])
    for name, balance in assets:
        writer.writerow([name, f"{balance:.2f}"])
    writer.writerow(["TOTAL ASSETS", f"{total_assets:.2f}"])
    writer.writerow([])
    
    writer.writerow(["LIABILITIES", "Amount"])
    for name, balance in liabilities:
        writer.writerow([name, f"{balance:.2f}"])
    writer.writerow(["TOTAL LIABILITIES", f"{total_liabilities:.2f}"])
    writer.writerow([])
    
    writer.writerow(["EQUITY", "Amount"])
    for name, balance in equity:
        writer.writerow([name, f"{balance:.2f}"])
    writer.writerow(["TOTAL EQUITY", f"{total_equity:.2f}"])
    writer.writerow([])
    
    writer.writerow(["TOTAL LIABILITIES & EQUITY", f"{total_liabilities_and_equity:.2f}"])

    output.seek(0)
    filename = f"balance_sheet_as_of_{end_date_str}.csv"
    return Response(output.getvalue(), mimetype="text/csv", headers={"Content-Disposition": f"attachment; filename={filename}"})


@reports_bp.route('/export/income-statement')
@login_required
@role_required('Admin', 'Accountant')
def export_income_statement():
    """Exports the income statement to CSV."""
    start_date_str = request.args.get('start_date', '')
    end_date_str = request.args.get('end_date', '')
    start_date = parse_date(start_date_str)
    end_date = parse_date(end_date_str)

    agg = aggregate_account_balances(start_date, end_date)

    revenues, expenses = {}, {}
    cogs_amount = Decimal('0.00')

    try:
        cogs_code = get_system_account_code('COGS')
    except Exception:
        cogs_code = None

    for acc_code, data in agg.items():
        bal = to_decimal(data['net'])
        acct_rec = Account.query.filter_by(code=acc_code).first()
        if not acct_rec:
            continue

        if acct_rec.type == 'Revenue':
            revenues[acct_rec.name] = -bal
        elif acct_rec.type == 'Expense':
            if cogs_code and acc_code == cogs_code:
                cogs_amount += bal
            elif acct_rec.name.lower() in ('cogs', 'cost of goods sold') and not cogs_code:
                cogs_amount += bal
            else:
                expenses[acct_rec.name] = bal

    total_revenue = sum(revenues.values(), Decimal('0.00'))
    total_expense = sum(expenses.values(), Decimal('0.00'))
    gross_profit = total_revenue - cogs_amount
    net_income = gross_profit - total_expense

    output = io.StringIO()
    writer = csv.writer(output)

    date_range_label = f"For the period {start_date_str} to {end_date_str}"
    if not start_date_str or not end_date_str:
        date_range_label = "For All Time" # Fallback

    writer.writerow(["Income Statement", ""])
    writer.writerow([date_range_label, ""])
    writer.writerow([])

    writer.writerow(["REVENUES", "Amount"])
    for name, balance in revenues.items():
        writer.writerow([name, f"{balance:.2f}"])
    writer.writerow(["Total Revenue", f"{total_revenue:.2f}"])
    writer.writerow([])

    # Insert COGS as single line item right after revenues (Xero style)
    writer.writerow(["Cost of Goods Sold (COGS)", f"({cogs_amount:.2f})"])
    writer.writerow(["Gross Profit", f"{gross_profit:.2f}"])
    writer.writerow([])

    writer.writerow(["EXPENSES", "Amount"])
    for name, balance in expenses.items():
        writer.writerow([name, f"({balance:.2f})"])
    writer.writerow(["Total Expenses", f"({total_expense:.2f})"])
    writer.writerow([])

    writer.writerow(["NET INCOME", f"{net_income:.2f}"])

    output.seek(0)
    filename = f"income_statement_{datetime.now().strftime('%Y%m%d')}.csv"
    return Response(output.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={filename}"})



@reports_bp.route('/export/vat-report')
@login_required
@role_required('Admin', 'Accountant')
def export_vat_report():
    start_date = parse_date(request.args.get("start_date"))
    end_date = parse_date(request.args.get("end_date"))

    sale_query = Sale.query
    ar_invoice_query = ARInvoice.query
    purchase_query = Purchase.query
    ap_invoice_query = APInvoice.query

    if start_date:
        sale_query = sale_query.filter(Sale.created_at >= start_date)
        ar_invoice_query = ar_invoice_query.filter(ARInvoice.date >= start_date)
        purchase_query = purchase_query.filter(Purchase.created_at >= start_date)
        ap_invoice_query = ap_invoice_query.filter(APInvoice.date >= start_date)
    if end_date:
        end_date_inclusive = end_date + timedelta(days=1)
        sale_query = sale_query.filter(Sale.created_at < end_date_inclusive)
        ar_invoice_query = ar_invoice_query.filter(ARInvoice.date < end_date_inclusive)
        purchase_query = purchase_query.filter(Purchase.created_at < end_date_inclusive)
        ap_invoice_query = ap_invoice_query.filter(APInvoice.date < end_date_inclusive)

    sales_vat = to_decimal(sale_query.filter(Sale.is_vatable == True).with_entities(func.coalesce(func.sum(Sale.vat), 0)).scalar())
    ar_invoice_vat = to_decimal(ar_invoice_query.filter(ARInvoice.vat != None, ARInvoice.vat > 0).with_entities(func.coalesce(func.sum(ARInvoice.vat), 0)).scalar())
    total_output_vat = sales_vat + ar_invoice_vat

    purchases_vat = to_decimal(purchase_query.filter(Purchase.is_vatable == True).with_entities(func.coalesce(func.sum(Purchase.vat), 0)).scalar())
    ap_invoice_vat = to_decimal(ap_invoice_query.filter(APInvoice.vat != None, APInvoice.vat > 0).with_entities(func.coalesce(func.sum(APInvoice.vat), 0)).scalar())
    total_input_vat = purchases_vat + ap_invoice_vat

    nonvat_sales = to_decimal(sale_query.filter((Sale.is_vatable == False) | (Sale.is_vatable == None)).with_entities(func.coalesce(func.sum(Sale.total), 0)).scalar())
    nonvat_ar = to_decimal(ar_invoice_query.filter((ARInvoice.vat == 0) | (ARInvoice.vat == None)).with_entities(func.coalesce(func.sum(ARInvoice.total), 0)).scalar())
    total_nonvat_sales = nonvat_sales + nonvat_ar

    nonvat_purchases = to_decimal(purchase_query.filter((Purchase.is_vatable == False) | (Purchase.is_vatable == None)).with_entities(func.coalesce(func.sum(Purchase.total), 0)).scalar())
    nonvat_ap = to_decimal(ap_invoice_query.filter((APInvoice.vat == 0) | (APInvoice.vat == None)).with_entities(func.coalesce(func.sum(APInvoice.total), 0)).scalar())
    total_nonvat_purchases = nonvat_purchases + nonvat_ap

    vat_payable = total_output_vat - total_input_vat

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Type", "Amount (₱)"])
    writer.writerow(["Total Input VAT (from all vatable purchases)", f"{total_input_vat:.2f}"])
    writer.writerow(["Total Output VAT (from all vatable sales)", f"{total_output_vat:.2f}"])
    writer.writerow(["VAT Payable", f"{vat_payable:.2f}"])
    writer.writerow([])

    # Add Non-VAT details
    writer.writerow(["Non-VAT Sales (Cash + AR)", f"{total_nonvat_sales:.2f}"])
    writer.writerow(["Non-VAT Purchases (Cash + AP)", f"{total_nonvat_purchases:.2f}"])

    output.seek(0)
    filename = f"vat_report_{datetime.now().strftime('%Y%m%d')}.csv"
    return Response(output.getvalue(), mimetype="text/csv", headers={"Content-Disposition": f"attachment; filename={filename}"})


@reports_bp.route('/export/trial-balance')
@login_required
@role_required('Admin', 'Accountant')
def export_trial_balance():
    """Exports the trial balance to CSV."""
    
    start_date_str = request.args.get('start_date', '')
    end_date_str = request.args.get('end_date', '')
    start_date = parse_date(start_date_str)
    end_date = parse_date(end_date_str)

    # 1. Get the new dictionary structure
    agg = aggregate_account_balances(start_date, end_date)
    
    tb = []
    total_debit = Decimal('0.00')
    total_credit = Decimal('0.00')

    # 2. Iterate through the dictionary items
    for acc_code, data in agg.items():
        # --- FIX: Extract 'net' from the data dictionary ---
        val = to_decimal(data['net'])
        
        acc_details = Account.query.filter_by(code=acc_code).first()
        acc_name = acc_details.name if acc_details else f"Unknown ({acc_code})"
        
        if val >= Decimal('0.00'):
            tb.append({'code': acc_code, 'name': acc_name, 'debit': val, 'credit': Decimal('0.00')})
            total_debit += val
        else:
            tb.append({'code': acc_code, 'name': acc_name, 'debit': Decimal('0.00'), 'credit': -val})
            total_credit += -val
            
    tb.sort(key=lambda x: x['code'])
    
    output = io.StringIO()
    writer = csv.writer(output)
    
    date_range_label = f"For the period {start_date_str} to {end_date_str}"
    if not start_date_str or not end_date_str:
        date_range_label = "For All Time" 

    writer.writerow(["Trial Balance", ""])
    writer.writerow([date_range_label, "", ""])
    writer.writerow([])
    
    writer.writerow(["Code", "Account Name", "Debit", "Credit"])
    for row in tb:
        writer.writerow([row['code'], row['name'], f"{row['debit']:.2f}", f"{row['credit']:.2f}"])
    writer.writerow([])
    writer.writerow(["Totals", "", f"{total_debit:.2f}", f"{total_credit:.2f}"])

    output.seek(0)
    filename = f"trial_balance_{datetime.now().strftime('%Y%m%d')}.csv"
    return Response(output.getvalue(), mimetype="text/csv", headers={"Content-Disposition": f"attachment; filename={filename}"})


    # --- MODIFIED: The core function now accepts dates ---
def aggregate_account_balances(start_date=None, end_date=None):
    """
    Aggregates Journal Entries.
    Returns: { account_code: {'debit': Decimal, 'credit': Decimal, 'net': Decimal} }
    """
    from collections import defaultdict
    from datetime import timedelta

    # Initialize with a dictionary structure instead of a single Decimal
    balances = defaultdict(lambda: {'debit': Decimal('0.00'), 'credit': Decimal('0.00'), 'net': Decimal('0.00')})

    query = JournalEntry.query.filter(JournalEntry.voided_at.is_(None))

    if start_date:
        query = query.filter(JournalEntry.created_at >= start_date)

    if end_date:
        try:
            # Inclusive end date logic
            end_exclusive = end_date + timedelta(days=1)
            query = query.filter(JournalEntry.created_at < end_exclusive)
        except Exception:
            query = query.filter(JournalEntry.created_at <= end_date)

    all_journal_entries = query.all()

    for je in all_journal_entries:
        try:
            entries = []
            if hasattr(je, 'entries'):
                entries = je.entries()
            else:
                entries = json.loads(getattr(je, 'entries_json', '[]') or '[]')
        except Exception:
            continue

        for entry in entries:
            account_code = entry.get('account_code')
            if not account_code:
                continue
            
            debit = to_decimal(entry.get('debit', '0.00'))
            credit = to_decimal(entry.get('credit', '0.00'))
            
            # Store distinct totals
            balances[account_code]['debit'] += debit
            balances[account_code]['credit'] += credit
            balances[account_code]['net'] += (debit - credit)

    return balances


@reports_bp.route('/general-ledger')
@login_required
@role_required('Admin', 'Accountant')
def general_ledger():
    start_date_str = request.args.get('start_date')
    end_date_str = request.args.get('end_date')

    start_date = parse_date(start_date_str)
    end_date = parse_date(end_date_str)
    
    # Correct the end date to include the entire day selected
    if end_date:
        end_date += timedelta(days=1)

    # 1. Aggregate Balances (This MUST use the patched aggregate_account_balances function)
    agg = aggregate_account_balances(start_date, end_date)

    # 2. Fetch all unique account codes involved in transactions (Filter voided here too)
    # We query all JEs that are NOT voided to ensure we capture relevant account codes.
    account_codes_in_active_jes = db.session.query(JournalEntry.entries_json).filter(
        JournalEntry.voided_at.is_(None)
    ).all()
    
    unique_account_codes = set()
    for row in account_codes_in_active_jes:
        try:
            # Assuming row[0] contains the JSON string
            entries = json.loads(row[0]) 
            for entry in entries:
                if entry.get('account_code'):
                    unique_account_codes.add(entry['account_code'])
        except:
            pass 

    # Fetch Account records for lookup
    all_accounts = {acc.code: acc for acc in Account.query.all()}
    
    # Prepare the data structure
    gl_data = []
    
    for acc_code in sorted(list(unique_account_codes)):
        account = all_accounts.get(acc_code)
        if not account:
            continue

        acc_data = agg.get(acc_code, {})

        # 3. Fetch detailed entries for this specific account
        account_entries_query = JournalEntry.query
        
        # --- CRITICAL FIX: Filter out voided JEs for the detailed view ---
        account_entries_query = account_entries_query.filter(JournalEntry.voided_at.is_(None))
        
        if start_date:
            account_entries_query = account_entries_query.filter(JournalEntry.created_at >= start_date)
        if end_date:
            account_entries_query = account_entries_query.filter(JournalEntry.created_at < end_date)

        # Filter by account_code being present in the entries_json string/column
        account_entries_query = account_entries_query.filter(JournalEntry.entries_json.like(f'%\"account_code\": \"{acc_code}\"%'))

        account_entries = account_entries_query.order_by(JournalEntry.created_at.asc()).all()

        # 4. Calculate running balance and prepare rows
        running_balance = Decimal('0.00')
        rows = []
        
        for je in account_entries:
            entries = je.entries()
            debit = Decimal('0.00')
            credit = Decimal('0.00')
            
            for entry in entries:
                if entry.get('account_code') == acc_code:
                    debit = to_decimal(entry.get('debit', '0.00'))
                    credit = to_decimal(entry.get('credit', '0.00'))
                    break
            
            # Balances are tracked as DR - CR
            running_balance += debit
            running_balance -= credit
            
            rows.append({
                'date': je.created_at,
                'description': je.description,
                'debit': debit,
                'credit': credit,
                'running_balance': running_balance,
                'balance_type': 'DR' if running_balance >= 0 else 'CR',
                'je_id': je.id
            })

        gl_data.append({
            'account': f"{account.code} - {account.name}", # FIX: Concatenate code and name
            'account_type': account.type,
            'balance': acc_data.get('net', Decimal('0.00')),
            'debit': acc_data.get('debit', Decimal('0.00')),
            'credit': acc_data.get('credit', Decimal('0.00')),
            'rows': rows,
        })
        
    return render_template('general_ledger.html', 
                           gl_data=gl_data,
                           start_date=start_date_str,
                           end_date=end_date_str,
                           all_accounts=all_accounts)


@reports_bp.route('/export/general-ledger')
@login_required
@role_required('Admin', 'Accountant')
def export_general_ledger():
    start_date_str = request.args.get('start_date', '')
    end_date_str = request.args.get('end_date', '')
    start_date = parse_date(start_date_str)
    end_date = parse_date(end_date_str)

    agg = aggregate_account_balances(start_date, end_date)
    
    gl_data = []
    
    for acc_code, data in agg.items():
        acc_details = Account.query.filter_by(code=acc_code).first()
        if not acc_details: continue

        # --- FIX: Use the distinct debit/credit totals from aggregation ---
        total_debit = data['debit']
        total_credit = data['credit']
        balance = data['net']
        
        # Determine Balance Type label based on Account Type
        is_debit_normal = acc_details.type in ['Asset', 'Expense']
        if is_debit_normal:
            balance_type = 'Debit' if balance >= Decimal('0.00') else 'Credit'
        else:
            balance_type = 'Credit' if balance < Decimal('0.00') else 'Debit'

        gl_data.append({
            'account': f"{acc_code} - {acc_details.name}",
            'debit': total_debit,     # Now explicitly passed
            'credit': total_credit,   # Now explicitly passed
            'balance': abs(balance),
            'balance_type': balance_type
        })
        
    gl_data.sort(key=lambda x: x['account'])

    output = io.StringIO()
    writer = csv.writer(output)
    
    # ... (CSV Header writing remains same) ...
    date_range_label = f"For the period {start_date_str} to {end_date_str}" if (start_date_str and end_date_str) else "For All Time"
    
    writer.writerow(["General Ledger Summary", ""])
    writer.writerow([date_range_label, ""])
    writer.writerow([])
    writer.writerow(["Account", "Net Debits", "Net Credits", "Balance", "Balance Type"])
    
    for row in gl_data:
        writer.writerow([
            row['account'], 
            f"{row['debit']:.2f}", 
            f"{row['credit']:.2f}", 
            f"{row['balance']:.2f}", 
            row['balance_type']
        ])
    
    # ... (Filename logic remains same) ...
    output.seek(0)
    filename = f"general_ledger_summary.csv"
    return Response(output.getvalue(), mimetype="text/csv", headers={"Content-Disposition": f"attachment; filename={filename}"})
