from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, send_file, Response, session
from models import db, User, Product, Purchase, PurchaseItem, Sale, SaleItem, JournalEntry, StockAdjustment, Account, Supplier, Branch, InventoryMovement, InventoryMovementItem
import json
from config import Config
from datetime import datetime, timedelta
from sqlalchemy import func, exc
from models import Sale, CompanyProfile, User, AuditLog, Customer
import io, csv, json
from io import StringIO
from routes.utils import paginate_query, log_action, get_system_account_code
from passlib.hash import pbkdf2_sha256
from flask_login import login_user, logout_user, login_required, current_user
from routes.decorators import role_required
from .utils import log_action
from extensions import limiter
from routes.sku_utils import generate_sku
from routes.fifo_utils import create_inventory_lot, consume_inventory_fifo

from decimal import Decimal, ROUND_HALF_UP, getcontext, InvalidOperation
getcontext().prec = 28

core_bp = Blueprint('core', __name__)
VAT_RATE = Config.VAT_RATE
VAT_DEC = Decimal(str(VAT_RATE)) if VAT_RATE is not None else Decimal('0.12')  # fallback if config missing


def to_decimal(value):
    """Coerce value (None, float, int, str, Decimal) -> Decimal quantized to 2dp."""
    if value is None:
        return Decimal('0.00')
    if isinstance(value, Decimal):
        return value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    if isinstance(value, int):
        return Decimal(value).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    if isinstance(value, float):
        try:
            d = Decimal(str(value))
        except Exception:
            return Decimal('0.00')
        return d.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    # fallback for string-like values
    try:
        d = Decimal(value)
    except Exception:
        try:
            d = Decimal(str(value))
        except Exception:
            return Decimal('0.00')
    return d.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

def _money_filter(value):
    """Format Decimal/number to string with two decimals for display in templates."""
    try:
        return format(to_decimal(value), '0.2f')
    except Exception:
        return "0.00"

def _num_filter(value):
    """Return a native float suitable for JSON/JS usage (use with tojson in templates)."""
    try:
        return float(to_decimal(value))
    except Exception:
        return 0.0

@core_bp.record
def _register_jinja_filters(state):
    """
    Register Jinja filters at blueprint registration time (avoids import-time app access).
    Usage in templates:
      - Display money: ₱{{ value | money }}
      - Embed number for JS: data-cost='{{ value | num | tojson }}'
    """
    app = state.app
    app.jinja_env.filters['money'] = _money_filter
    app.jinja_env.filters['num'] = _num_filter


@core_bp.route('/setup/license', methods=['GET', 'POST'])
def setup_license():
    if request.method == 'POST':
        license_key = request.form.get('license_key')
        if license_key == 'test123':
            session['validated_license_key'] = license_key
            return redirect(url_for('core.setup_company'))
        else:
            flash('Invalid license key. Please use the testing key.', 'danger')
    return render_template('setup/license.html')


@core_bp.route('/setup/company', methods=['GET', 'POST'])
def setup_company():
    if request.method == 'POST':
        name = request.form.get('name')
        tin = request.form.get('tin')
        address = request.form.get('address')
        style = request.form.get('business_style')
        branch = request.form.get('branch')

        if not name or not tin or not address:
            flash('Please fill out all company details.', 'warning')
            return redirect(url_for('core.setup_company'))

        license_key = session.pop('validated_license_key', None)

        profile = CompanyProfile(name=name, tin=tin, address=address, business_style=style, branch=branch)
        db.session.add(profile)

        # Auto-create Branch record if branch is provided
        if branch:
            from models import Branch
            existing_branch = Branch.query.filter_by(name=branch).first()
            if not existing_branch:
                new_branch = Branch(name=branch, address='')
                db.session.add(new_branch)

        db.session.commit()
        return redirect(url_for('core.setup_admin'))
    return render_template('setup/company.html')


@core_bp.route('/setup/admin', methods=['GET', 'POST'])
def setup_admin():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')

        if not username or not password:
            flash('Username and password cannot be empty.', 'warning')
            return redirect(url_for('core.setup_admin'))

        hashed_password = pbkdf2_sha256.hash(password)
        admin_user = User(username=username, password_hash=hashed_password, role='Admin')
        db.session.add(admin_user)
        db.session.commit()

        # Log the new admin in automatically
        login_user(admin_user)
        flash('Setup complete! Welcome to your new accounting system.', 'success')
        return redirect(url_for('core.index'))
    return render_template('setup/admin.html')


@core_bp.route('/')
@login_required
def index():
    # --- Base data ---
    products = Product.query.all()
    low_stock = [p for p in products if p.quantity <= 5 and p.is_active]

    # --- 📊 INVENTORY VALUE: Calculate from FIFO lots (not product.cost_price) ---
    from models import InventoryLot
    total_inventory_value = to_decimal(db.session.query(
        func.coalesce(func.sum(InventoryLot.quantity_remaining * InventoryLot.unit_cost), 0)
    ).join(Product).filter(
        Product.is_active == True,
        InventoryLot.quantity_remaining > 0
    ).scalar())

    products_in_stock = Product.query.filter(Product.quantity > 0, Product.is_active == True).count()

    # --- Get period filter (default to '7' days) ---
    period = request.args.get('period', '7')
    today = datetime.utcnow()
    start_date = None
    current_filter_label = ''

    # --- Set start_date and label based on the period ---
    if period == '12':
        start_date = today - timedelta(hours=12)
        current_filter_label = 'Last 12 Hours'
    elif period == '30':
        start_date = today - timedelta(days=30)
        current_filter_label = 'Last 30 Days'
    elif period == 'all':
        current_filter_label = 'All Time'
    else:
        period = '7'
        start_date = today - timedelta(days=7)
        current_filter_label = 'Last 7 Days'

    # --- 📊 SALES: Include both Cash (POS) and Credit (AR Invoices) ---
    from models import ARInvoice, APInvoice

    cash_sales_query = db.session.query(func.coalesce(func.sum(Sale.total), 0))
    ar_sales_query = db.session.query(func.coalesce(func.sum(ARInvoice.total), 0))

    cash_purchases_query = db.session.query(func.coalesce(func.sum(Purchase.total), 0))
    ap_purchases_query = db.session.query(func.coalesce(func.sum(APInvoice.total), 0))

    if start_date:
        cash_sales_query = cash_sales_query.filter(Sale.created_at >= start_date)
        ar_sales_query = ar_sales_query.filter(ARInvoice.date >= start_date)
        cash_purchases_query = cash_purchases_query.filter(Purchase.created_at >= start_date)
        ap_purchases_query = ap_purchases_query.filter(APInvoice.date >= start_date)

    # --- Execute FILTERED queries ---
    total_cash_sales = to_decimal(cash_sales_query.scalar())
    total_ar_sales = to_decimal(ar_sales_query.scalar())
    total_cash_purchases = to_decimal(cash_purchases_query.scalar())
    total_ap_purchases = to_decimal(ap_purchases_query.scalar())

    # 📊 COMBINED TOTALS (Cash + Credit)
    total_sales = (total_cash_sales + total_ar_sales).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    total_purchases = (total_cash_purchases + total_ap_purchases).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

    # --- 📊 CALCULATE TRUE NET INCOME FROM ACCOUNTING RECORDS ---
    from routes.reports import aggregate_account_balances

    end_date = None
    if period != 'all':
        end_date = today

    agg = aggregate_account_balances(start_date, end_date)

    total_revenue = Decimal('0.00')
    total_expenses = Decimal('0.00')
    total_cogs = Decimal('0.00')

    try:
        cogs_code = get_system_account_code('COGS')
    except:
        cogs_code = None

    for acc_code, bal in agg.items():
        acct_rec = Account.query.filter_by(code=acc_code).first()
        if not acct_rec:
            continue

        bal_dec = to_decimal(bal)
        if acct_rec.type == 'Revenue':
            # Revenue accounts have credit balances (negative in agg)
            total_revenue += abs(bal_dec)
        elif acct_rec.type == 'Expense':
            if cogs_code and acc_code == cogs_code:
                total_cogs += abs(bal_dec)
            else:
                total_expenses += abs(bal_dec)

    # 📊 TRUE NET INCOME (Revenue - COGS - Expenses)
    gross_profit = total_revenue - total_cogs
    net_income = gross_profit - total_expenses

    # --- Charting Logic (unchanged but Decimal safe) ---
    sales_by_period = []
    labels = []

    if period == '12':
        intervals = [today - timedelta(hours=i) for i in range(11, -1, -1)]
        for hour_start in intervals:
            hour_end = hour_start + timedelta(hours=1)
            hour_total = to_decimal(db.session.query(func.coalesce(func.sum(Sale.total), 0))
                                    .filter(Sale.created_at >= hour_start)
                                    .filter(Sale.created_at < hour_end)
                                    .filter(Sale.voided_at == None)
                                    .scalar())
            sales_by_period.append(hour_total)
            labels.append(hour_start.strftime('%I%p'))
    else:
        if period == '30':
            days = 30
        elif period == '7':
            days = 7
        else:
            days = 90

        today_date = datetime.utcnow().date()
        last_n_days = [today_date - timedelta(days=i) for i in range(days - 1, -1, -1)]
        for day in last_n_days:
            day_total = to_decimal(db.session.query(func.coalesce(func.sum(Sale.total), 0))
                                   .filter(func.date(Sale.created_at) == day)
                                   .filter(Sale.voided_at == None)
                                   .scalar())
            sales_by_period.append(day_total)
            labels.append(day.strftime('%b %d'))

    # --- Top Sellers (unchanged) ---
    top_sellers = (
        db.session.query(
            Product.name,
            func.sum(SaleItem.qty).label('total_qty_sold')
        )
            .join(SaleItem, Product.id == SaleItem.product_id)
            .join(Sale, Sale.id == SaleItem.sale_id)
            .filter(Sale.voided_at == None)
            .group_by(Product.name)
            .order_by(func.sum(SaleItem.qty).desc())
            .limit(10)
            .all()
    )

    # --- ✅ FIXED: DUE DATES DASHBOARD DATA (AR & AP Invoices) ---
    from models import ARInvoice, APInvoice, Customer, Supplier

    ar_due = ARInvoice.query.filter(
        ARInvoice.status != 'Paid',
        ARInvoice.due_date.isnot(None),
        ARInvoice.voided_at == None
    ).order_by(ARInvoice.due_date.asc()).limit(10).all()

    ap_due = APInvoice.query.filter(
        APInvoice.status != 'Paid',
        APInvoice.due_date.isnot(None),
        APInvoice.voided_at == None
    ).order_by(APInvoice.due_date.asc()).limit(10).all()

    due_items = []
    today_date = today.date()  # Convert to date for comparison

    # Process AR Invoices
    for inv in ar_due:
        # ✅ FIX: Handle both date and datetime objects
        if inv.due_date:
            due_date_obj = inv.due_date.date() if hasattr(inv.due_date, 'date') else inv.due_date
            days_until_due = (due_date_obj - today_date).days
        else:
            days_until_due = 999

        balance = to_decimal(inv.total) - to_decimal(inv.paid)

        if balance <= Decimal('0.00'):
            continue

        urgency = 'overdue' if days_until_due < 0 else ('due_soon' if days_until_due <= 7 else 'upcoming')

        due_items.append({
            'type': 'AR Invoice',
            'id': inv.id,
            'number': inv.invoice_number or f"AR-{inv.id}",
            'party': inv.customer.name if inv.customer else 'N/A',
            'amount': balance,
            'due_date': inv.due_date,
            'days_until_due': days_until_due,
            'urgency': urgency,
            'description': inv.description or '',
            'url': url_for('ar_ap.billing_invoices'),
            'direction': 'receivable'
        })

    # Process AP Invoices
    for inv in ap_due:
        # ✅ FIX: Handle both date and datetime objects
        if inv.due_date:
            due_date_obj = inv.due_date.date() if hasattr(inv.due_date, 'date') else inv.due_date
            days_until_due = (due_date_obj - today_date).days
        else:
            days_until_due = 999

        balance = to_decimal(inv.total) - to_decimal(inv.paid)

        if balance <= Decimal('0.00'):
            continue

        urgency = 'overdue' if days_until_due < 0 else ('due_soon' if days_until_due <= 7 else 'upcoming')

        due_items.append({
            'type': 'AP Invoice',
            'id': inv.id,
            'number': inv.invoice_number or f"AP-{inv.id}",
            'party': inv.supplier.name if inv.supplier else 'N/A',
            'amount': balance,
            'due_date': inv.due_date,
            'days_until_due': days_until_due,
            'urgency': urgency,
            'description': inv.description or '',
            'url': url_for('ar_ap.ap_invoices'),
            'direction': 'payable'
        })

    # Sort by due date
    due_items.sort(key=lambda x: (x['urgency'] != 'overdue', x['urgency'] != 'due_soon', x['days_until_due']))

    overdue_items = [item for item in due_items if item['urgency'] == 'overdue']
    due_soon_items = [item for item in due_items if item['urgency'] == 'due_soon']
    upcoming_items = [item for item in due_items if item['urgency'] == 'upcoming'][:5]
    sales_by_day = [float(v) for v in sales_by_period]


    return render_template(
        'index.html',
        products=products,
        low_stock=low_stock,
        total_sales=total_sales,  # Decimal
        total_purchases=total_purchases,  # Decimal
        net_income=net_income,  # Decimal
        gross_profit=gross_profit,  # Decimal
        total_inventory_value=total_inventory_value,  # Decimal
        products_in_stock=products_in_stock,
        labels=labels,
        sales_by_day=sales_by_day,
        top_sellers=top_sellers,
        current_period_filter=period,
        current_filter_label=current_filter_label,
        overdue_items=overdue_items,
        due_soon_items=due_soon_items,
        upcoming_items=upcoming_items,
        config=Config
    )


@login_required
@core_bp.route('/inventory', methods=['GET', 'POST'])
def inventory():
    from models import Product

    # Handle product form submission
    if request.method == 'POST':
        data = request.form
        new_prod = Product(
            sku=data.get('sku'),
            name=data.get('name'),
            sale_price=to_decimal(data.get('sale_price') or '0'),
            cost_price=to_decimal(data.get('cost_price') or '0'),
            quantity=int(data.get('quantity') or 0)
        )
        db.session.add(new_prod)
        db.session.commit()
        flash('Product added successfully.', 'success')
        return redirect(url_for('core.inventory'))

    # --- Handle GET (view with pagination) ---
    search = request.args.get('search', '').strip()
    query = Product.query

    if search:
        query = query.filter(
            (Product.name.ilike(f"%{search}%")) |
            (Product.sku.ilike(f"%{search}%"))
        )

    # Order and paginate
    query = query.order_by(Product.is_active.desc(), Product.name.asc())
    pagination = paginate_query(query, per_page=12)

    safe_args = {k: v for k, v in request.args.items() if k != 'page'}
    all_active_products = Product.query.filter_by(is_active=True).order_by(Product.name.asc()).all()

    has_opening_balance = db.session.query(JournalEntry.id) \
        .filter(JournalEntry.entries_json.ilike('%"account_code": "302"%')) \
        .first() is not None

    return render_template(
        'inventory.html',
        products=pagination.items,
        pagination=pagination,
        search=search,
        safe_args=safe_args,
        all_active_products=all_active_products,
        has_opening_balance=has_opening_balance
    )


@core_bp.route('/update_product', methods=['POST'])
def update_product():
    sku = request.form.get('sku')
    product = Product.query.filter_by(sku=sku).first()
    if not product:
        flash('Product not found.', 'danger')
        return redirect(request.referrer or url_for('core.inventory'))

    try:
        product.name = request.form.get('name')
        product.sale_price = to_decimal(request.form.get('sale_price') or '0')
        product.cost_price = to_decimal(request.form.get('cost_price') or '0')

        log_action(f'Updated product SKU: {product.sku}, Name: {product.name}.')
        db.session.commit()
        flash(f'Product {product.sku} updated successfully.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Error updating product: {str(e)}', 'danger')

    return redirect(request.referrer or url_for('core.inventory'))


@core_bp.route('/product/toggle-status/<int:product_id>', methods=['POST'])
@login_required
@role_required('Admin', 'Accountant')
def toggle_product_status(product_id):
    product = Product.query.get_or_404(product_id)

    # Toggle the status
    product.is_active = not product.is_active

    if product.is_active:
        log_action(f'Enabled product: {product.sku} ({product.name}).')
        flash(f'Product {product.name} has been enabled.', 'success')
    else:
        log_action(f'Disabled product: {product.sku} ({product.name}).')
        flash(f'Product {product.name} has been disabled.', 'danger')

    db.session.commit()
    return jsonify({'status': 'ok', 'new_is_active': product.is_active})


@core_bp.route('/inventory/bulk-add', methods=['GET', 'POST'])
@login_required
@role_required('Admin', 'Accountant')
def inventory_bulk_add():
    if request.method == 'POST':
        from routes.fifo_utils import create_inventory_lot
        from routes.sku_utils import generate_sku

        if 'csv_file' not in request.files:
            flash('No file part', 'danger')
            return redirect(request.url)

        file = request.files['csv_file']
        if file.filename == '':
            flash('No selected file', 'danger')
            return redirect(request.url)

        if not file.filename.endswith('.csv'):
            flash('Invalid file type. Please upload a .csv file.', 'danger')
            return redirect(request.url)

        try:
            stream = io.StringIO(file.stream.read().decode("UTF-8"), newline=None)
            csv_reader = csv.reader(stream)

            header = next(csv_reader, None)  # Get header row

            products_added = 0
            total_value = Decimal('0.00')
            errors = []
            skipped_count = 0

            try:
                inventory_code = get_system_account_code('Inventory')
                equity_code = get_system_account_code('Opening Balance Equity')
            except Exception as e:
                flash(f'An error occurred finding system accounts: {str(e)}', 'danger')
                return redirect(request.url)

            debug_items = []

            for row_num, row in enumerate(csv_reader, start=2):
                # Skip completely empty rows
                if not row or all(cell.strip() == '' for cell in row):
                    continue

                # Expected format: name, sale_price, cost_price, quantity, [optional: category]
                if len(row) < 4:
                    errors.append(f"Row {row_num}: Not enough columns (expected at least 4: name, sale_price, cost_price, quantity)")
                    skipped_count += 1
                    continue

                try:
                    name = row[0].strip() if len(row) > 0 else ''
                    sale_price = to_decimal(row[1] or '0') if len(row) > 1 else Decimal('0.00')
                    cost_price = to_decimal(row[2] or '0') if len(row) > 2 else Decimal('0.00')
                    quantity = int(row[3] or 0) if len(row) > 3 else 0
                    category = row[4].strip() if len(row) > 4 and row[4].strip() else None

                    if not name:
                        errors.append(f"Row {row_num}: Missing product name")
                        skipped_count += 1
                        continue

                    try:
                        sku = generate_sku(name, category=category)
                    except ValueError as e:
                        errors.append(f"Row {row_num}: {str(e)}")
                        skipped_count += 1
                        continue

                    try:
                        new_prod, sku = create_product_with_retry(
                            name=name,
                            category=category,
                            sale_price=sale_price,
                            cost_price=cost_price,
                            quantity=quantity,
                            max_retries=3
                        )
                    except Exception as e:
                        db.session.rollback()
                        errors.append(f"Row {row_num}: Failed to create product: {str(e)}")
                        skipped_count += 1
                        continue

                    db.session.add(new_prod)
                    db.session.flush()

                    if quantity > 0 and cost_price > Decimal('0.00'):
                        initial_value = (Decimal(quantity) * cost_price).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

                        debug_items.append({
                            'sku': sku,
                            'name': name,
                            'qty': quantity,
                            'cost': str(cost_price),
                            'value': str(initial_value)
                        })

                        total_value += initial_value

                        create_inventory_lot(
                            product_id=new_prod.id,
                            quantity=quantity,
                            unit_cost=cost_price,
                            is_opening_balance=True
                        )

                        je_lines = [
                            {'account_code': inventory_code, 'debit': format(initial_value, '0.2f'), 'credit': "0.00"},
                            {'account_code': equity_code, 'debit': "0.00", 'credit': format(initial_value, '0.2f')}
                        ]
                        je = JournalEntry(
                            description=f'Beginning Balance for {new_prod.sku} ({new_prod.name})',
                            entries_json=json.dumps(je_lines)
                        )
                        db.session.add(je)

                    db.session.commit()
                    products_added += 1

                except ValueError as e:
                    db.session.rollback()
                    errors.append(f"Row {row_num}: Invalid number format - {str(e)}")
                    skipped_count += 1
                except Exception as e:
                    db.session.rollback()
                    errors.append(f"Row {row_num}: {str(e)}")
                    skipped_count += 1

            flash(f'✅ Successfully added {products_added} products with auto-generated SKUs.', 'success')
            if total_value > Decimal('0.00'):
                flash(f'📊 Recorded ₱{total_value:,.2f} in Beginning Inventory Value.', 'info')

                # Debug output
                print("\n=== DEBUG: Auto-SKU Bulk Upload ===")
                for item in debug_items:
                    print(f"{item['sku']}: {item['name']} | {item['qty']} × ₱{item['cost']} = ₱{item['value']}")
                print(f"TOTAL: ₱{total_value}")
                print("=" * 40)

            if errors:
                flash(f'⚠️ {skipped_count} rows were skipped:', 'warning')
                for error in errors[:10]:  # Show first 10 errors
                    flash(error, 'danger')
                if len(errors) > 10:
                    flash(f'... and {len(errors) - 10} more errors', 'danger')

            log_action(f'Bulk-added {products_added} products with auto-generated SKUs. Total value: ₱{total_value:,.2f}.')
            return redirect(url_for('core.inventory'))

        except Exception as e:
            db.session.rollback()
            flash(f'❌ An error occurred processing the file: {str(e)}', 'danger')
            return redirect(request.url)

    from routes.sku_utils import get_category_suggestions
    categories = get_category_suggestions()

    return render_template('inventory_bulk_add.html', categories=categories)


def create_product_with_retry(name, category, sale_price, cost_price, quantity, custom_sku=None, max_retries=3):
    from datetime import datetime
    from routes.sku_utils import generate_sku
    from models import Product
    attempt = 0
    last_exc = None

    # Normalize to Decimal
    sale_price = to_decimal(sale_price)
    cost_price = to_decimal(cost_price)

    if custom_sku:
        sku = None
        try:
            sku = generate_sku(name, category=category, custom_sku=custom_sku)
        except Exception:
            raise

        new_prod = Product(
            sku=sku,
            name=name,
            category=category,
            sale_price=sale_price,
            cost_price=cost_price,
            quantity=quantity
        )
        db.session.add(new_prod)
        try:
            db.session.flush()
            return new_prod, sku
        except exc.IntegrityError as ie:
            db.session.rollback()
            raise ie
        except Exception:
            db.session.rollback()
            raise

    while attempt < max_retries:
        sku = generate_sku(name, category=category)
        new_prod = Product(
            sku=sku,
            name=name,
            category=category,
            sale_price=sale_price,
            cost_price=cost_price,
            quantity=quantity
        )
        db.session.add(new_prod)
        try:
            db.session.flush()
            return new_prod, sku
        except exc.IntegrityError as ie:
            db.session.rollback()
            last_exc = ie
            attempt += 1
            continue
        except Exception as e:
            db.session.rollback()
            raise

    fallback_prefix = (category or 'PRD')[:3].upper()
    timestamp = datetime.now().strftime('%Y%m%d%H%M%S')
    fallback_sku = f"{fallback_prefix}-{timestamp}"
    new_prod = Product(
        sku=fallback_sku,
        name=name,
        category=category,
        sale_price=sale_price,
        cost_price=cost_price,
        quantity=quantity
    )
    db.session.add(new_prod)
    try:
        db.session.flush()
        return new_prod, fallback_sku
    except Exception as final_e:
        db.session.rollback()
        raise last_exc or final_e


@core_bp.route('/api/add_multiple_products', methods=['POST'])
@login_required
@role_required('Admin', 'Accountant')
def api_add_multiple_products():
    data = request.json
    products_data = data.get('products', [])

    if not products_data:
        return jsonify({'error': 'No product data provided'}), 400

    new_product_count = 0
    total_value = Decimal('0.00')
    errors = []

    try:
        inventory_code = get_system_account_code('Inventory')
        equity_code = get_system_account_code('Opening Balance Equity')

        for p_data in products_data:
            sku = p_data.get('sku')
            name = p_data.get('name')

            if not sku or not name:
                errors.append(f"Skipped row (missing SKU or Name): {sku}")
                continue

            if Product.query.filter_by(sku=sku).first():
                errors.append(f"SKU '{sku}' already exists. Skipped.")
                continue

            try:
                initial_cost = to_decimal(p_data.get('cost_price') or '0')
                initial_qty = int(p_data.get('quantity') or 0)

                new_prod = Product(
                    sku=sku,
                    name=name,
                    sale_price=to_decimal(p_data.get('sale_price') or '0'),
                    cost_price=initial_cost,
                    quantity=initial_qty
                )
                db.session.add(new_prod)

                if initial_qty > 0 and initial_cost > Decimal('0.00'):
                    initial_value = (Decimal(initial_qty) * initial_cost).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                    total_value += initial_value

                    je_lines = [
                        {'account_code': inventory_code, 'debit': format(initial_value, '0.2f'), 'credit': "0.00"},
                        {'account_code': equity_code, 'debit': "0.00", 'credit': format(initial_value, '0.2f')}
                    ]
                    je = JournalEntry(
                        description=f'Beginning Balance for {new_prod.sku} ({new_prod.name})',
                        entries_json=json.dumps(je_lines)
                    )
                    db.session.add(je)

                new_product_count += 1

            except ValueError:
                errors.append(f"Invalid number for SKU '{sku}'. Skipped.")

        db.session.commit()

        log_action(f'Bulk-added {new_product_count} products with total beginning value of {total_value}.')

        if errors:
            return jsonify({
                'status': 'partial',
                'count': new_product_count,
                'error': f'Added {new_product_count} products, but some failed. See errors.',
                'errors': errors
            }), 207

        return jsonify({'status': 'ok', 'count': new_product_count})

    except exc.IntegrityError as e:
        db.session.rollback()
        return jsonify({'error': 'A database error occurred (e.g., duplicate SKU).', 'details': str(e)}), 500
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500


@core_bp.route('/api/products/search')
def api_products_search():
    query = request.args.get('q', '').strip()

    if query == '':
        products = Product.query.filter_by(is_active=True) \
            .order_by(Product.name.asc()) \
            .limit(100) \
            .all()
    else:
        products = Product.query.filter(
            (Product.sku.ilike(f'%{query}%')) |
            (Product.name.ilike(f'%{query}%'))
        ).filter_by(is_active=True) \
            .order_by(Product.name.asc()) \
            .limit(50) \
            .all()

    results = [{
        'id': p.id,
        'sku': p.sku,
        'name': p.name,
        'quantity': p.quantity,
        'cost_price': float(to_decimal(p.cost_price)),
        'sale_price': float(to_decimal(p.sale_price))
    } for p in products]

    return jsonify(results)



@core_bp.route('/purchase', methods=['GET', 'POST'])
@login_required
@role_required('Admin', 'Accountant', 'Cashier')
def purchase():
    if request.method == 'POST':
        try:
            from routes.fifo_utils import create_inventory_lot

            supplier_name = request.form.get('supplier', '').strip() or 'Unknown'
            
            # --- START: NEW PAYMENT/DATE CAPTURE LOGIC ---
            date_str = request.form.get('date') # Capture the purchase date
            items_raw = request.form.get('items_json')
            items = json.loads(items_raw) if items_raw else []

            if not items:
                flash("No items added to the purchase. Please add products first.", "warning")
                return redirect(url_for('core.purchase'))

            purchase_is_vatable = 'is_vatable' in request.form
            
            # 1. Capture Payment Type and Due Date
            payment_type = request.form.get('payment_type', 'Credit') # 'Credit' or 'Cash'
            due_date_str = request.form.get('due_date')

            due_date = None
            purchase_status = 'Open' # Default for Credit
            credit_account_code = get_system_account_code('Accounts Payable') # Default to 201
            
            if payment_type == 'Cash':
                # If paid by Cash, the balancing credit goes to Cash (Asset decrease)
                credit_account_code = get_system_account_code('Cash') # Account 101
                purchase_status = 'Paid' # Immediate payment
            
            if payment_type == 'Credit' and due_date_str:
                try:
                    due_date = datetime.strptime(due_date_str, '%Y-%m-%d')
                except ValueError:
                    # Log error, but proceed with None date
                    log_action(f"Warning: Invalid due date format for purchase from {supplier_name}")
            # --- END: NEW PAYMENT/DATE CAPTURE LOGIC ---
            
            supplier = Supplier.query.filter_by(name=supplier_name).first()
            if not supplier and supplier_name != 'Unknown':
                supplier = Supplier(name=supplier_name)
                db.session.add(supplier)
                db.session.flush()

            # 2. Update Purchase Object Creation
            purchase = Purchase(
                total=Decimal('0.00'), 
                vat=Decimal('0.00'), 
                supplier=supplier_name, 
                is_vatable=purchase_is_vatable,
                # New fields
                due_date=due_date,
                payment_type=payment_type,
                status=purchase_status
            )
            db.session.add(purchase)
            db.session.flush()

            total = Decimal('0.00')
            vat_total = Decimal('0.00')

            for item in items:
                raw_sku = item.get('sku', '').strip()
                sku_arg = None if raw_sku == 'AUTO' or raw_sku == '' else raw_sku

                try:
                    qty = int(item.get('qty', 0))
                    unit_cost = to_decimal(item.get('unit_cost', 0))
                except (TypeError, ValueError):
                    continue

                name = item.get('name', 'Unnamed')

                if qty <= 0 or unit_cost < Decimal('0.00'):
                    continue

                line_net = (Decimal(qty) * unit_cost).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                if purchase_is_vatable:
                    # Compute VAT portion from net cost
                    vat = (line_net * VAT_DEC).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                else:
                    vat = Decimal('0.00')
                # Line total in the original code was net, but total/vat_total logic below assumes gross total
                line_total = line_net + vat 

                product = Product.query.filter_by(sku=sku_arg).first() if sku_arg else None
                final_sku = sku_arg

                if not product:
                    try:
                        product, generated_sku = create_product_with_retry(
                            name=name,
                            category=None,
                            sale_price=(unit_cost * Decimal('1.5')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP),
                            cost_price=unit_cost,
                            quantity=qty,
                            custom_sku=sku_arg,
                            max_retries=3
                        )
                        db.session.add(product)
                        db.session.flush()

                        final_sku = generated_sku
                        flash(f'ℹ️ Created new product: {final_sku} ({name})', 'info')

                    except ValueError as ve:
                        db.session.rollback()
                        flash(f'❌ SKU Error for "{name}": {str(ve)}', 'danger')
                        return redirect(url_for('core.purchase'))
                    except Exception as e:
                        db.session.rollback()
                        flash(f'❌ Error creating product "{name}": {str(e)}', 'danger')
                        return redirect(url_for('core.purchase'))
                else:
                    product.quantity += qty
                    final_sku = product.sku

                purchase_item = PurchaseItem(
                    purchase_id=purchase.id,
                    product_id=product.id,
                    product_name=product.name,
                    sku=final_sku,
                    qty=qty,
                    unit_cost=to_decimal(unit_cost),
                    line_total=to_decimal(line_total)
                )
                db.session.add(purchase_item)
                db.session.flush()

                create_inventory_lot(
                    product_id=product.id,
                    quantity=qty,
                    unit_cost=to_decimal(unit_cost),
                    purchase_id=purchase.id,
                    purchase_item_id=purchase_item.id
                )

                total += line_net # Sum of Net Costs
                vat_total += vat # Sum of VAT

            # Recalculate based on final totals (total is now NET COST of all items)
            net_total = total
            total = net_total + vat_total # This is the Gross Total (to be credited)

            purchase.total = total.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            purchase.vat = vat_total.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

            # 3. Journal Entry Logic (Unified)
            
            journal_lines = [
                # DEBIT 1: Inventory (Net Cost of Goods)
                {"account_code": get_system_account_code('Inventory'), "debit": format(net_total.quantize(Decimal('0.01')), '0.2f'), "credit": "0.00"},
            ]
            
            if vat_total > Decimal('0.00'):
                # DEBIT 2: VAT Input (The recoverable tax asset)
                journal_lines.append({"account_code": get_system_account_code('VAT Input'), "debit": format(vat_total.quantize(Decimal('0.01')), '0.2f'), "credit": "0.00"})
                
            # CREDIT: The balancing entry (Cash or Accounts Payable)
            journal_lines.append({
                "account_code": credit_account_code, # Dynamically set to 'Cash' or 'Accounts Payable'
                "debit": "0.00", 
                "credit": format(total.quantize(Decimal('0.01')), '0.2f') # Gross Total
            })

            journal = JournalEntry(
                description=f"Purchase #{purchase.id} - {supplier_name} ({payment_type})",
                entries_json=json.dumps(journal_lines)
            )
            db.session.add(journal)

            log_action(f'Recorded Purchase #{purchase.id} from {supplier_name} for ₱{total:,.2f} ({payment_type}).')
            db.session.commit()

            flash(f"✅ Purchase #{purchase.id} recorded successfully. Payment Type: {payment_type}.", "success")
            return redirect(url_for('core.purchases'))

        except Exception as e:
            db.session.rollback()
            flash(f"❌ Error saving purchase: {str(e)}", "danger")
            return redirect(url_for('core.purchase'))

    products = Product.query.filter_by(is_active=True).order_by(Product.name.asc()).all()
    suppliers = Supplier.query.order_by(Supplier.name).all()
    today = datetime.utcnow().strftime('%Y-%m-%d')

    return render_template('purchase.html', products=products, suppliers=suppliers, today=today)


@core_bp.route('/purchases')
def purchases():
    purchases = Purchase.query.order_by(Purchase.id.desc()).all()
    return render_template('purchases.html', purchases=purchases)


@core_bp.route('/purchase/cancel/<int:purchase_id>', methods=['POST'])
@login_required
@role_required('Admin', 'Accountant')
def cancel_purchase(purchase_id):
    purchase = Purchase.query.get_or_404(purchase_id)

    if purchase.status == 'Canceled':
        flash(f'Purchase #{purchase.id} is already canceled.', 'warning')
        return redirect(url_for('core.purchases'))

    try:
        total_net = to_decimal(purchase.total) - to_decimal(purchase.vat)
        total_vat = to_decimal(purchase.vat)
        total = to_decimal(purchase.total)

        journal_lines = [
            {"account_code": get_system_account_code('Accounts Payable'), "debit": format(total, '0.2f'), "credit": "0.00"},
            {"account_code": get_system_account_code('Inventory'), "debit": "0.00", "credit": format(total_net, '0.2f')},
        ]
        if total_vat and total_vat > Decimal('0.00'):
            journal_lines.append({"account_code": get_system_account_code('VAT Input'), "debit": "0.00", "credit": format(total_vat, '0.2f')})

        journal = JournalEntry(
            description=f"Reversal/Cancel of Purchase #{purchase.id} - {purchase.supplier}",
            entries_json=json.dumps(journal_lines)
        )
        db.session.add(journal)

        for item in purchase.items:
            product = Product.query.get(item.product_id)
            if product:
                product.quantity = max(0, product.quantity - item.qty)

        purchase.status = 'Canceled'

        log_action(f'Canceled Purchase #{purchase.id} (Supplier: {purchase.supplier}, Total: ₱{purchase.total:,.2f}). Reversing JE and stock adjustment created.')

        db.session.commit()
        flash(f'Purchase #{purchase.id} has been canceled. Journal entry posted and stock levels adjusted.', 'success')

    except Exception as e:
        db.session.rollback()
        flash(f'Error canceling purchase: {str(e)}', 'danger')

    return redirect(url_for('core.purchases'))


@core_bp.route('/purchase/<int:purchase_id>')
def view_purchase(purchase_id):
    purchase = Purchase.query.get_or_404(purchase_id)
    items = PurchaseItem.query.filter_by(purchase_id=purchase.id).all()
    return render_template('purchase_view.html', purchase=purchase, items=items)


@core_bp.route('/pos')
@login_required
@role_required('Admin', 'Cashier')
def pos():
    from models import ConsignmentItem

    search = request.args.get('search', '').strip()

    product_query = Product.query.filter_by(is_active=True)
    consignment_query = ConsignmentItem.query.filter_by(is_active=True)

    if search:
        product_query = product_query.filter(
            (Product.name.ilike(f"%{search}%")) |
            (Product.sku.ilike(f"%{search}%"))
        )
        consignment_query = consignment_query.filter(
            (ConsignmentItem.product_name.ilike(f"%{search}%")) |
            (ConsignmentItem.sku.ilike(f"%{search}%")) |
            (ConsignmentItem.barcode.ilike(f"%{search}%"))
        )

    products = product_query.order_by(Product.name.asc()).all()
    consignment_items_raw = consignment_query.all()
    consignment_items = [item for item in consignment_items_raw if item.quantity_available > 0]

    combined_items = []

    for p in products:
        combined_items.append({
            'id': p.id,
            'sku': p.sku,
            'name': p.name,
            'price': float(to_decimal(p.sale_price)),
            'quantity': p.quantity,
            'is_consignment': False,
            'type': 'regular'
        })

    for c in consignment_items:
        combined_items.append({
            'id': c.id,
            'sku': c.sku,
            'name': c.product_name,
            'price': float(to_decimal(c.retail_price)),
            'quantity': c.quantity_available,
            'is_consignment': True,
            'consignment_id': c.consignment_id,
            'type': 'consignment'
        })

    per_page = 12
    page = request.args.get('page', 1, type=int)
    total_items = len(combined_items)
    total_pages = (total_items + per_page - 1) // per_page if total_items > 0 else 1
    start_idx = (page - 1) * per_page
    end_idx = start_idx + per_page
    paginated_items = combined_items[start_idx:end_idx]

    class Pagination:
        def __init__(self, page, per_page, total_count, total_pages):
            self.page = page
            self.per_page = per_page
            self.total = total_count
            self.pages = total_pages
            self.has_prev = page > 1
            self.has_next = page < total_pages
            self.prev_num = page - 1 if self.has_prev else None
            self.next_num = page + 1 if self.has_next else None

        def iter_pages(self, left_edge=2, left_current=2, right_current=5, right_edge=2):
            last = 0
            for num in range(1, self.pages + 1):
                if (num <= left_edge or
                        (num > self.page - left_current - 1 and num < self.page + right_current) or
                        num > self.pages - right_edge):
                    if last + 1 != num:
                        yield None
                    yield num
                    last = num

    pagination = Pagination(page, per_page, total_items, total_pages)
    safe_args = {k: v for k, v in request.args.items() if k != 'page'}

    return render_template(
        'pos.html',
        products=paginated_items,
        pagination=pagination,
        search=search,
        safe_args=safe_args,
        current_user_name=current_user.username,
        config=Config
    )


@core_bp.route('/api/sale', methods=['POST'])
@login_required
def api_sale():
    from routes.fifo_utils import consume_inventory_fifo

    data = request.json or {}
    items = data.get('items', [])
    sale_is_vatable = bool(data.get('is_vatable', False))
    doc_type = data.get('doc_type', 'Invoice')
    discount = data.get('discount') or {}

    discount_type = discount.get('type') or None
    discount_input = to_decimal(discount.get('input_value') or '0')

    customer_name = (data.get('customer_name') or '').strip() or 'Walk-in'

    if not items:
        return jsonify({'error': 'No items in sale'}), 400

    try:
        profile = CompanyProfile.query.first()
        if not profile:
            return jsonify({'error': 'Company profile not set up in settings'}), 500

        if not hasattr(profile, 'next_invoice_number') or profile.next_invoice_number is None:
            profile.next_invoice_number = max(getattr(profile, 'next_or_number', 1) or 1,
                                              getattr(profile, 'next_si_number', 1) or 1)

        doc_num = profile.next_invoice_number
        profile.next_invoice_number += 1
        full_doc_number = f"INV-{doc_num:06d}"

        sale = Sale(total=Decimal('0.00'), vat=Decimal('0.00'), document_number=full_doc_number, document_type=doc_type,
                    is_vatable=sale_is_vatable, customer_name=customer_name,
                    discount_type=discount_type, discount_input=to_decimal(discount_input))
        db.session.add(sale)
        db.session.flush()

        subtotal_gross = Decimal('0.00')
        total_cogs = Decimal('0.00')
        processed = []

        for it in items:
            sku = it.get('sku')
            try:
                qty = int(it.get('qty') or 0)
            except (TypeError, ValueError):
                qty = 0
            if qty <= 0:
                db.session.rollback()
                return jsonify({'error': f'Invalid quantity for SKU {sku}'}), 400

            from models import ConsignmentItem, ConsignmentSale, ConsignmentSaleItem
            is_consignment = it.get('is_consignment', False)

            if is_consignment:
                consignment_item_id = it.get('consignment_item_id')
                consignment_item = ConsignmentItem.query.get(consignment_item_id)

                if not consignment_item:
                    db.session.rollback()
                    return jsonify({'error': f'Consignment item {sku} not found'}), 404

                if consignment_item.quantity_available < qty:
                    db.session.rollback()
                    return jsonify({'error': f'Insufficient consignment stock for {consignment_item.product_name}'}), 400

                unit_price = to_decimal(consignment_item.retail_price)
                line_gross = (unit_price * Decimal(qty)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                line_cogs = Decimal('0.00')

                product_name = consignment_item.product_name
                product_sku = consignment_item.sku

                processed.append({
                    'product': None,
                    'consignment_item': consignment_item,
                    'qty': qty,
                    'unit_price': unit_price,
                    'line_gross': line_gross,
                    'cogs': line_cogs,
                    'is_consignment': True,
                    'product_name': product_name,
                    'product_sku': product_sku
                })

            else:
                product = Product.query.filter_by(sku=sku).first()
                if not product:
                    db.session.rollback()
                    return jsonify({'error': f'Product {sku} not found'}), 404
                if product.quantity < qty:
                    db.session.rollback()
                    return jsonify({'error': f'Insufficient stock for {product.name}'}), 400

                unit_price = to_decimal(product.sale_price)
                line_gross = (unit_price * Decimal(qty)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

                try:
                    line_cogs, _ = consume_inventory_fifo(
                        product_id=product.id,
                        quantity_needed=qty,
                        sale_id=sale.id,
                        sale_item_id=None
                    )
                    line_cogs = to_decimal(line_cogs)
                except ValueError as e:
                    db.session.rollback()
                    return jsonify({'error': str(e)}), 400

                product_name = product.name
                product_sku = product.sku

                processed.append({
                    'product': product,
                    'consignment_item': None,
                    'qty': qty,
                    'unit_price': unit_price,
                    'line_gross': line_gross,
                    'cogs': line_cogs,
                    'is_consignment': False,
                    'product_name': product_name,
                    'product_sku': product_sku
                })

            subtotal_gross += line_gross
            total_cogs += to_decimal(line_cogs)

        resolved_discount = Decimal('0.00')

        regular_sales_gross = sum(p['line_gross'] for p in processed if not p['is_consignment'])
        consignment_sales_gross = sum(p['line_gross'] for p in processed if p['is_consignment'])

        # SC/PWD special handling
        if discount_type == 'sc_pwd' and sale_is_vatable:
            regular_sales_net_base = (regular_sales_gross / (Decimal('1.00') + VAT_DEC)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            pct = max(Decimal('0.00'), min(Decimal('100.00'), discount_input))
            resolved_discount = (regular_sales_net_base * (pct / Decimal('100.00'))).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

            regular_sales_final_price = (regular_sales_net_base - resolved_discount).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            regular_sales_vat_final = Decimal('0.00')  # SC/PWD exempt
            regular_sales_net_final = regular_sales_final_price

            consignment_sales_final_price = consignment_sales_gross
            if sale_is_vatable:
                consignment_vat_final = (consignment_sales_final_price * (VAT_DEC / (Decimal('1.00') + VAT_DEC))).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            else:
                consignment_vat_final = Decimal('0.00')
            consignment_net_final = (consignment_sales_final_price - consignment_vat_final).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

            total_amount = (regular_sales_final_price + consignment_sales_final_price).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            vat_after = (regular_sales_vat_final + consignment_vat_final).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

        else:
            if discount_type and discount_input and subtotal_gross > Decimal('0.00'):
                if discount_type == 'percent':
                    pct = max(Decimal('0.00'), min(Decimal('100.00'), discount_input))
                    resolved_discount = (subtotal_gross * (pct / Decimal('100.00'))).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                else:
                    # fixed
                    resolved_discount = min(subtotal_gross, discount_input).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

            if subtotal_gross > Decimal('0.00'):
                regular_discount_share = (resolved_discount * (regular_sales_gross / subtotal_gross)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                consignment_discount_share = (resolved_discount * (consignment_sales_gross / subtotal_gross)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            else:
                regular_discount_share = Decimal('0.00')
                consignment_discount_share = Decimal('0.00')

            regular_sales_post_discount = (regular_sales_gross - regular_discount_share).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            if sale_is_vatable:
                regular_sales_vat_final = (regular_sales_post_discount * (VAT_DEC / (Decimal('1.00') + VAT_DEC))).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            else:
                regular_sales_vat_final = Decimal('0.00')
            regular_sales_net_final = (regular_sales_post_discount - regular_sales_vat_final).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

            consignment_sales_post_discount = (consignment_sales_gross - consignment_discount_share).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            if sale_is_vatable:
                consignment_vat_final = (consignment_sales_post_discount * (VAT_DEC / (Decimal('1.00') + VAT_DEC))).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            else:
                consignment_vat_final = Decimal('0.00')
            consignment_net_final = (consignment_sales_post_discount - consignment_vat_final).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

            vat_after = (regular_sales_vat_final + consignment_vat_final).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            total_amount = (regular_sales_post_discount + consignment_sales_post_discount).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

        sale.discount_value = to_decimal(resolved_discount)
        sale.total = to_decimal(total_amount)
        sale.vat = to_decimal(vat_after)
        db.session.flush()

        for p in processed:
            if p['is_consignment']:
                consignment_item = p['consignment_item']
                sale_item = SaleItem(
                    sale_id=sale.id,
                    product_id=None,
                    product_name=p['product_name'],
                    sku=p['product_sku'],
                    qty=p['qty'],
                    unit_price=to_decimal(p['unit_price']),
                    line_total=to_decimal(p['line_gross']),
                    cogs=Decimal('0.00')
                )
                db.session.add(sale_item)

                consignment_item.quantity_sold += p['qty']

                consignment = consignment_item.consignment
                total_received = sum(item.quantity_received for item in consignment.items)
                total_sold = sum(item.quantity_sold for item in consignment.items)
                total_returned = sum(item.quantity_returned for item in consignment.items)

                if total_sold + total_returned >= total_received:
                    consignment.status = 'Closed'
                elif total_sold > 0 or total_returned > 0:
                    consignment.status = 'Partial'

            else:
                product = p['product']
                sale_item = SaleItem(
                    sale_id=sale.id,
                    product_id=product.id,
                    product_name=product.name,
                    sku=product.sku,
                    qty=p['qty'],
                    unit_price=to_decimal(p['unit_price']),
                    line_total=to_decimal(p['line_gross']),
                    cogs=to_decimal(p['cogs'])
                )
                db.session.add(sale_item)
                product.quantity -= p['qty']

        consignment_sales_total = sum(p['line_gross'] for p in processed if p['is_consignment'])
        consignment_commission_total = Decimal('0.00')

        if consignment_sales_total > Decimal('0.00'):
            consignment_groups = {}
            for p in processed:
                if p['is_consignment']:
                    cons_id = p['consignment_item'].consignment_id
                    if cons_id not in consignment_groups:
                        consignment = p['consignment_item'].consignment
                        consignment_groups[cons_id] = {
                            'consignment': consignment,
                            'total': Decimal('0.00')
                        }
                    consignment_groups[cons_id]['total'] += to_decimal(p['line_gross'])

            for cons_id, group in consignment_groups.items():
                commission_rate = (group['consignment'].commission_rate or 0) / 100 if group['consignment'].commission_rate else 0
                commission = (group['total'] * Decimal(str(commission_rate))).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                consignment_commission_total += commission

        je_lines = []

        je_lines.append({'account_code': get_system_account_code('Cash'), 'debit': format(to_decimal(total_amount), '0.2f'), 'credit': "0.00"})

        if consignment_commission_total > Decimal('0.00'):
            je_lines.append({
                'account_code': get_system_account_code('Consignment Commission Revenue'),
                'debit': "0.00",
                'credit': format(consignment_commission_total, '0.2f')
            })

        if consignment_sales_total > Decimal('0.00'):
            consignment_payable = (consignment_sales_total - consignment_commission_total).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            je_lines.append({
                'account_code': get_system_account_code('Consignment Payable'),
                'debit': "0.00",
                'credit': format(consignment_payable, '0.2f')
            })

        discount_acc_code = get_system_account_code('Discounts Allowed')
        if resolved_discount and resolved_discount > Decimal('0.00'):
            je_lines.append({'account_code': discount_acc_code, 'debit': format(resolved_discount, '0.2f'), 'credit': "0.00"})

        if total_cogs and total_cogs > Decimal('0.00'):
            je_lines.append({'account_code': get_system_account_code('COGS'), 'debit': format(total_cogs, '0.2f'), 'credit': "0.00"})

        # Sales Revenue uses regular_sales_net_final (ensure defined)
        if 'regular_sales_net_final' in locals() and to_decimal(regular_sales_net_final) > Decimal('0.00'):
            je_lines.append({'account_code': get_system_account_code('Sales Revenue'), 'debit': "0.00", 'credit': format(to_decimal(regular_sales_net_final), '0.2f')})

        final_total_vat = (to_decimal(regular_sales_vat_final) if 'regular_sales_vat_final' in locals() else Decimal('0.00')) + \
                          (to_decimal(consignment_vat_final) if 'consignment_vat_final' in locals() else Decimal('0.00'))
        if final_total_vat > Decimal('0.00'):
            je_lines.append({'account_code': get_system_account_code('VAT Payable'), 'debit': "0.00", 'credit': format(final_total_vat, '0.2f')})

        if total_cogs and total_cogs > Decimal('0.00'):
            je_lines.append({'account_code': get_system_account_code('Inventory'), 'debit': "0.00", 'credit': format(total_cogs, '0.2f')})

        total_debits = sum(Decimal(l.get('debit')) if isinstance(l.get('debit'), str) else to_decimal(l.get('debit', '0')) for l in je_lines)
        total_credits = sum(Decimal(l.get('credit')) if isinstance(l.get('credit'), str) else to_decimal(l.get('credit', '0')) for l in je_lines)

        rounding_diff = (total_debits - total_credits).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        if abs(rounding_diff) >= Decimal('0.01'):
            adjusted = False
            sales_revenue_code = get_system_account_code('Sales Revenue')
            for l in je_lines:
                if l.get('account_code') == sales_revenue_code and l.get('credit') != "0.00":
                    current = to_decimal(l['credit'])
                    l['credit'] = format((current + rounding_diff).quantize(Decimal('0.01')), '0.2f')
                    adjusted = True
                    break

            if not adjusted:
                for l in je_lines:
                    if l.get('account_code') == discount_acc_code and l.get('debit') != "0.00":
                        current = to_decimal(l['debit'])
                        l['debit'] = format((current - rounding_diff).quantize(Decimal('0.01')), '0.2f')
                        adjusted = True
                        break

            if not adjusted:
                cash_code = get_system_account_code('Cash')
                for l in je_lines:
                    if l.get('account_code') == cash_code and l.get('debit') != "0.00":
                        current = to_decimal(l['debit'])
                        l['debit'] = format((current - rounding_diff).quantize(Decimal('0.01')), '0.2f')
                        adjusted = True
                        break

        total_debits = sum(Decimal(l.get('debit')) if isinstance(l.get('debit'), str) else to_decimal(l.get('debit', '0')) for l in je_lines)
        total_credits = sum(Decimal(l.get('credit')) if isinstance(l.get('credit'), str) else to_decimal(l.get('credit', '0')) for l in je_lines)

        if total_debits.quantize(Decimal('0.01')) != total_credits.quantize(Decimal('0.01')):
            db.session.rollback()
            return jsonify({'error': f'Journal entry balancing failed. D={total_debits}, C={total_credits}'}), 500

        db.session.add(JournalEntry(description=f'Sale #{sale.id} ({full_doc_number})', entries_json=json.dumps(je_lines)))

        log_action(f'Recorded Sale #{sale.id} ({full_doc_number}) for ₱{total_amount:,.2f}. Customer: {customer_name}. Discount: ₱{resolved_discount:.2f}')
        db.session.commit()

        server_total = to_decimal(sale.total)
        server_vat = to_decimal(sale.vat or 0)
        vatable_sales = (server_total - server_vat).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

        return jsonify({
            'status': 'ok',
            'sale_id': sale.id,
            'receipt_number': full_doc_number,
            'total': float(server_total),                    # authoritative server total
            'vat': float(server_vat),
            'discount_value': float(to_decimal(resolved_discount)),
            'vatable_sales': float(vatable_sales)
        })
    except exc.IntegrityError as e:
        db.session.rollback()
        return jsonify({'error': 'Failed to generate unique document number. Please try again.'}), 500
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': f'An unexpected error occurred: {str(e)}'}), 500


@core_bp.route('/sales')
def sales():
    from models import Sale, ARInvoice, Customer
    from app import db
    from datetime import datetime, timedelta

    search = request.args.get('search', '').strip()
    start_date_str = request.args.get('start_date', '').strip()
    end_date_str = request.args.get('end_date', '').strip()
    page = request.args.get('page', 1, type=int)
    per_page = 20

    start_date = None
    end_date = None

    if start_date_str:
        try:
            start_date = datetime.strptime(start_date_str, '%Y-%m-%d')
        except ValueError:
            flash('Invalid start date format', 'warning')

    if end_date_str:
        try:
            end_date = datetime.strptime(end_date_str, '%Y-%m-%d') + timedelta(days=1)
        except ValueError:
            flash('Invalid end date format', 'warning')

    cash_sales_query = Sale.query.filter(Sale.voided_at == None)
    ar_invoices_query = ARInvoice.query.filter(ARInvoice.voided_at == None)

    if start_date:
        cash_sales_query = cash_sales_query.filter(Sale.created_at >= start_date)
        ar_invoices_query = ar_invoices_query.filter(ARInvoice.date >= start_date)

    if end_date:
        cash_sales_query = cash_sales_query.filter(Sale.created_at <= end_date)
        ar_invoices_query = ar_invoices_query.filter(ARInvoice.date <= end_date)

    cash_sales = cash_sales_query.all()
    billing_invoices = ar_invoices_query.all()

    all_sales = []

    for s in cash_sales:
        all_sales.append({
            'id': s.id,
            'type': 'Cash Sale',
            'date': s.created_at,
            'document_number': s.document_number or f"Sale-{s.id}",
            'customer_name': s.customer_name or 'Walk-in',
            'total': to_decimal(s.total),
            'vat': to_decimal(s.vat or 0),
            'discount_value': to_decimal(s.discount_value or 0),
            'status': s.status or 'paid',
            'paid': to_decimal(s.total),
            'balance': Decimal('0.00'),
            'created_at': s.created_at
        })

    for inv in billing_invoices:
        all_sales.append({
            'id': inv.id,
            'type': 'Billing Invoice',
            'date': inv.date,
            'document_number': inv.invoice_number or f"AR-{inv.id}",
            'customer_name': inv.customer.name if inv.customer else 'N/A',
            'total': to_decimal(inv.total),
            'vat': to_decimal(inv.vat or 0),
            'discount_value': Decimal('0.00'),
            'status': inv.status,
            'paid': to_decimal(inv.paid),
            'balance': (to_decimal(inv.total) - to_decimal(inv.paid)),
            'created_at': inv.date
        })

    if search:
        search_lower = search.lower()
        all_sales = [
            s for s in all_sales
            if (
                    search_lower in str(s['id']).lower() or
                    search_lower in s['type'].lower() or
                    search_lower in s['document_number'].lower() or
                    search_lower in s['customer_name'].lower() or
                    search_lower in s['status'].lower()
            )
        ]

    all_sales.sort(key=lambda x: x['date'], reverse=True)

    total_count = len(all_sales)
    total_pages = (total_count + per_page - 1) // per_page if total_count > 0 else 1
    start_idx = (page - 1) * per_page
    end_idx = start_idx + per_page
    paginated_sales = all_sales[start_idx:end_idx]

    class Pagination:
        def __init__(self, page, per_page, total_count, total_pages):
            self.page = page
            self.per_page = per_page
            self.total_count = total_count
            self.pages = total_pages
            self.has_prev = page > 1
            self.has_next = page < total_pages
            self.prev_num = page - 1 if self.has_prev else None
            self.next_num = page + 1 if self.has_next else None

    pagination = Pagination(page, per_page, total_count, total_pages)

    total_sales_sum = sum(s['total'] for s in all_sales)
    total_vat = sum(s['vat'] for s in all_sales)
    total_discount = sum(s['discount_value'] for s in all_sales)

    summary = {
        "total_sales": total_sales_sum,
        "total_vat": total_vat,
        "total_discount": total_discount,
        "count": len(all_sales),
        "cash_sales_count": len([s for s in all_sales if s['type'] == 'Cash Sale']),
        "billing_invoices_count": len([s for s in all_sales if s['type'] == 'Billing Invoice']),
    } if all_sales else None

    return render_template(
        'sales.html',
        sales=paginated_sales,
        summary=summary,
        pagination=pagination,
        search=search,
        start_date=start_date_str,
        end_date=end_date_str
    )


@core_bp.route('/sales/<int:sale_id>/print')
def print_receipt(sale_id):
    from models import Sale, SaleItem
    sale = Sale.query.get_or_404(sale_id)
    items = SaleItem.query.filter_by(sale_id=sale.id).all()
    from models import CompanyProfile
    company = CompanyProfile.query.first()
    return render_template('receipt.html', sale=sale, items=items, company=company, config=Config, current_user=current_user)


@core_bp.route('/export_sales')
def export_sales():
    from models import Sale, ARInvoice, Customer

    format_type = request.args.get('format', 'csv')

    search = request.args.get('search', '').strip()
    start_date = request.args.get('start_date', '')
    end_date = request.args.get('end_date', '')

    cash_query = Sale.query
    ar_query = ARInvoice.query

    if search:
        cash_query = cash_query.filter(
            (Sale.customer_name.ilike(f"%{search}%")) |
            (Sale.id.cast(db.String).ilike(f"%{search}%"))
        )
    if start_date:
        cash_query = cash_query.filter(Sale.created_at >= start_date)
        ar_query = ar_query.filter(ARInvoice.date >= start_date)
    if end_date:
        cash_query = cash_query.filter(Sale.created_at <= end_date)
        ar_query = ar_query.filter(ARInvoice.date <= end_date)

    cash_sales = cash_query.order_by(Sale.created_at.desc()).all()
    ar_invoices = ar_query.order_by(ARInvoice.date.desc()).all()

    if format_type == 'csv':
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["Type", "Doc #", "Date", "Customer", "Total", "Paid", "Balance", "VAT", "Discount", "Status"])

        for s in cash_sales:
            writer.writerow([
                "Cash Sale",
                s.document_number or f"Sale-{s.id}",
                s.created_at.strftime('%Y-%m-%d %H:%M') if s.created_at else "",
                s.customer_name or "Walk-in",
                f"{to_decimal(s.total):.2f}",
                f"{to_decimal(s.total):.2f}",
                "0.00",
                f"{to_decimal(s.vat or 0):.2f}",
                f"{to_decimal(s.discount_value or 0):.2f}",
                s.status or "paid"
            ])

        for inv in ar_invoices:
            writer.writerow([
                "Billing Invoice",
                inv.invoice_number or f"AR-{inv.id}",
                inv.date.strftime('%Y-%m-%d %H:%M') if inv.date else "",
                inv.customer.name if inv.customer else "N/A",
                f"{to_decimal(inv.total):.2f}",
                f"{to_decimal(inv.paid):.2f}",
                f"{(to_decimal(inv.total) - to_decimal(inv.paid)):.2f}",
                f"{to_decimal(inv.vat or 0):.2f}",
                "0.00",
                inv.status or "Open"
            ])

        output.seek(0)
        filename = f"sales_export_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"

        return Response(
            output.getvalue(),
            mimetype="text/csv",
            headers={"Content-Disposition": f"attachment;filename={filename}"}
        )

    return redirect(url_for('core.sales'))


@core_bp.route('/sales/<int:sale_id>')
def view_sale(sale_id):
    from models import SaleItem, Sale

    sale = Sale.query.get_or_404(sale_id)
    items = SaleItem.query.filter_by(sale_id=sale.id).all()

    return render_template('view_sale.html', sale=sale, items=items)


@core_bp.route('/journal-entries')
@role_required('Admin', 'Accountant')
def journal_entries():
    search = request.args.get('search', '')
    start_date_str = request.args.get('start_date', '')
    end_date_str = request.args.get('end_date', '')

    query = JournalEntry.query.order_by(JournalEntry.created_at.desc())

    accounts_map = {a.code: a.name for a in Account.query.all()}

    safe_args = {}
    if search:
        safe_args['search'] = search
        query = query.filter(
            (JournalEntry.description.ilike(f'%{search}%')) |
            (JournalEntry.entries_json.ilike(f'%\"account_code\": \"{search}\"%'))
        )

    start_date, end_date = None, None
    if start_date_str:
        try:
            start_date = datetime.strptime(start_date_str, '%Y-%m-%d')
            query = query.filter(JournalEntry.created_at >= start_date)
            safe_args['start_date'] = start_date_str
        except ValueError:
            pass
    if end_date_str:
        try:
            end_date = datetime.strptime(end_date_str, '%Y-%m-%d') + timedelta(days=1)
            query = query.filter(JournalEntry.created_at <= end_date)
            safe_args['end_date'] = end_date_str
        except ValueError:
            pass

    # paginate the query (same as before)
    pagination = paginate_query(query)

    # small helper to coerce values to Decimal for templates
    def _to_decimal_for_template(value):
        try:
            if value is None:
                return Decimal('0.00')
            if isinstance(value, Decimal):
                return value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            return Decimal(str(value)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        except (InvalidOperation, TypeError, ValueError):
            return Decimal('0.00')

    # Build a flat list of entry rows from the paginated JournalEntry objects
    entries = []
    total_debit_dec = Decimal('0.00') # <-- Decimal is accessible here (global scope)
    total_credit_dec = Decimal('0.00') # <-- Decimal is accessible here (global scope)

    for je in pagination.items:
        try:
            lines = je.entries()  # entries() should return list/dict lines
        except Exception:
            # If entries() isn't available or fails, try reading entries_json
            try:
                lines = json.loads(getattr(je, 'entries_json', '[]') or '[]')
            except Exception:
                lines = []

        for line in lines:
            # line can be dict-like; be defensive
            account_code = line.get('account_code') if isinstance(line, dict) else None
            debit_raw = line.get('debit') if isinstance(line, dict) else getattr(line, 'debit', 0)
            credit_raw = line.get('credit') if isinstance(line, dict) else getattr(line, 'credit', 0)
            description = getattr(je, 'description', '') or (line.get('description') if isinstance(line, dict) else '')

            # Coerce to Decimal for correct arithmetic and rounding
            # Use the global to_decimal() function
            debit_dec = to_decimal(debit_raw)
            credit_dec = to_decimal(credit_raw)

            total_debit_dec += debit_dec
            total_credit_dec += credit_dec

            # Use float values for template iteration/sum to avoid Jinja int+Decimal/start=0 issues.
            # The money filter will re-coerce floats to Decimal for formatting.
            entries.append({
                'journal_id': getattr(je, 'id', None),
                'date': getattr(je, 'created_at', None),
                'desc': description,
                'account_code': account_code,
                'account_name': accounts_map.get(account_code, account_code),
                'debit': float(debit_dec),
                'credit': float(credit_dec),
                'raw_line': line
            })

    # Provide both Decimal totals (precise) and float totals (template-friendly)
    total_debit = float(total_debit_dec)
    total_credit = float(total_credit_dec)

    return render_template(
        'reports.html',
        journals=pagination.items,
        entries=entries,
        pagination=pagination,
        accounts_map=accounts_map,
        safe_args=safe_args,
        start_date=start_date_str,
        end_date=end_date_str,
        total_debit=total_debit,          # float for template sum compat
        total_credit=total_credit,        # float for template sum compat
        total_debit_dec=total_debit_dec,  # Decimal if you need precise server-side usage
        total_credit_dec=total_credit_dec
    )


@core_bp.route('/export/journal-entries')
@login_required
@role_required('Admin', 'Accountant')
def export_journal_entries():
    search = request.args.get('search', '')
    start_date_str = request.args.get('start_date', '')
    end_date_str = request.args.get('end_date', '')

    query = JournalEntry.query.order_by(JournalEntry.created_at.asc())

    accounts_map = {a.code: a.name for a in Account.query.all()}

    if search:
        query = query.filter(
            (JournalEntry.description.ilike(f'%{search}%')) |
            (JournalEntry.entries_json.ilike(f'%\"account_code\": \"{search}\"%'))
        )
    if start_date_str:
        try:
            start_date = datetime.strptime(start_date_str, '%Y-%m-%d')
            query = query.filter(JournalEntry.created_at >= start_date)
        except ValueError:
            pass
    if end_date_str:
        try:
            end_date = datetime.strptime(end_date_str, '%Y-%m-%d') + timedelta(days=1)
            query = query.filter(JournalEntry.created_at <= end_date)
        except ValueError:
            pass

    journals = query.all()

    si = io.StringIO()
    writer = csv.writer(si)

    writer.writerow(['Journal_ID', 'Date', 'Description', 'Account_Code', 'Account_Name', 'Debit', 'Credit'])

    for je in journals:
        je_date = je.created_at.strftime('%Y-%m-%d %H:%M')
        for line in je.entries():
            code = line.get('account_code')
            name = accounts_map.get(code, code)
            debit = line.get('debit', 0)
            credit = line.get('credit', 0)
            writer.writerow([je.id, je_date, je.description, code, name, debit, credit])

    output = si.getvalue()
    return Response(
        output,
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment;filename=journal_entries.csv"}
    )


@core_bp.route('/export_journals')
def export_journals():
    journals = JournalEntry.query.order_by(JournalEntry.created_at.desc()).all()

    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["ID", "Description", "Account", "Debit", "Credit", "Date"])

    for j in journals:
        for e in j.entries():
            writer.writerow([
                j.id,
                j.description or '',
                e.get("account", ""),
                e.get("debit", 0),
                e.get("credit", 0),
                j.created_at.strftime("%Y-%m-%d %H:%M") if j.created_at else "",
            ])

    output.seek(0)
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=journal_entries.csv"}
    )


@core_bp.route('/vat_report')
def vat_report():
    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')
    return redirect(url_for('reports.vat_report', start_date=start_date, end_date=end_date))


def parse_date(date_str):
    try:
        return datetime.strptime(date_str, "%Y-%m-%d")
    except (ValueError, TypeError):
        return None


@core_bp.route('/export_vat', methods=['GET'])
def export_vat_report():
    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')
    return redirect(url_for('reports.export_vat_report', start_date=start_date, end_date=end_date))


@core_bp.route('/api/product/<sku>')
def api_product(sku):
    product = Product.query.filter_by(sku=sku).first()
    if not product:
        return jsonify({'error': 'Product not found'}), 404

    return jsonify({
        'sku': product.sku,
        'name': product.name,
        'sale_price': float(to_decimal(product.sale_price or 0)),
        'cost_price': float(to_decimal(product.cost_price or 0)),
        'quantity': product.quantity
    })


@core_bp.route('/login', methods=['GET', 'POST'])
@limiter.limit("10 per minute")
def login():
    if current_user.is_authenticated:
        return redirect(url_for('core.index'))

    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')

        if not username or not password:
            flash('Username and password are required.', 'danger')
            return render_template('login.html'), 400

        if len(username) > 100 or len(password) > 100:
            flash('Username or password is too long.', 'danger')
            return render_template('login.html'), 400

        user = User.query.filter_by(username=username).first()

        if user and pbkdf2_sha256.verify(password, user.password_hash):
            login_user(user)
            log_action(f'User logged in successfully.', user=user)
            db.session.commit()
            flash('Logged in successfully!', 'success')
            return redirect(url_for('core.index'))
        else:
            log_action(f'Failed login attempt for username: {username}.')
            db.session.commit()
            flash('Invalid username or password.', 'danger')

    return render_template('login.html')


@core_bp.route('/reset-password', methods=['GET', 'POST'])
@limiter.limit("5 per minute")
def reset_password_form():
    if request.method == 'POST':
        username = request.form.get('username')
        new_password = request.form.get('password')

        if not username or not new_password:
            flash('Username and new password are required.', 'danger')
            return redirect(url_for('core.reset_password_form'))

        user = User.query.filter_by(username=username).first()
        if user:
            user.password_hash = pbkdf2_sha256.hash(new_password)
            db.session.commit()

            log_action(f'Password for user {username} was reset via TIN verification.')

            flash('Password has been reset successfully. You can now log in.', 'success')
            return redirect(url_for('core.login'))
        else:
            flash('User not found.', 'danger')

    all_users = User.query.order_by(User.username).all()
    return render_template('reset_password.html', users=all_users)


@core_bp.route('/forgot-password', methods=['GET', 'POST'])
@limiter.limit("10 per hour")
def forgot_password():
    if request.method == 'POST':
        tin = request.form.get('tin')
        company = CompanyProfile.query.first()

        if company and company.tin == tin:
            return redirect(url_for('core.reset_password_form'))
        else:
            flash('The provided TIN does not match our records.', 'danger')

    return render_template('forgot_password.html')


@core_bp.route('/logout')
@login_required
def logout():
    user_id_to_log = current_user.id
    username_to_log = current_user.username

    logout_user()

    try:
        log = AuditLog(
            user_id=user_id_to_log,
            action=f'User {username_to_log} logged out successfully.',
            ip_address=request.remote_addr
        )
        db.session.add(log)
        db.session.commit()
    except Exception as e:
        print(f"Error creating audit log for logout: {e}")
        db.session.rollback()

    flash('You have been logged out.', 'info')
    return redirect(url_for('core.login'))


@core_bp.route('/settings', methods=['GET', 'POST'])
@login_required
@role_required('Admin')
def settings():
    profile = CompanyProfile.query.first_or_404()
    if request.method == 'POST':
        profile.name = request.form.get('name')
        profile.tin = request.form.get('tin')
        profile.address = request.form.get('address')
        profile.business_style = request.form.get('business_style')
        profile.branch = request.form.get('branch')

        if profile.branch and not Branch.query.filter_by(name=profile.branch).first():
            new_branch = Branch(name=profile.branch, address='', is_active=True)
            db.session.add(new_branch)
            log_action(f'Auto-created branch: {profile.branch} from company settings.')

        log_action(f'Updated Company Profile settings.')
        db.session.commit()
        flash('Company profile updated successfully!', 'success')
        return redirect(url_for('core.settings'))

    all_users = User.query.order_by(User.username).all()
    return render_template('settings.html', profile=profile, users=all_users)


@core_bp.route('/inventory/adjust', methods=['POST'])
@login_required
@role_required('Admin', 'Accountant')
def adjust_stock():
    product_id = int(request.form.get('product_id'))
    quantity = int(request.form.get('quantity'))
    reason = request.form.get('reason')

    product = Product.query.get_or_404(product_id)

    if not reason:
        flash('A reason for the adjustment is required.', 'danger')
        return redirect(url_for('core.inventory'))

    if quantity == 0:
        flash('Quantity cannot be zero.', 'warning')
        return redirect(url_for('core.inventory'))

    try:
        original_qty = product.quantity
        product.quantity += quantity

        adjustment = StockAdjustment(
            product_id=product.id,
            quantity_changed=quantity,
            reason=reason,
            user_id=current_user.id
        )
        db.session.add(adjustment)
        db.session.flush()

        adjustment_value = to_decimal(abs(quantity) * to_decimal(product.cost_price))

        if quantity < 0:
            debit_account_code = get_system_account_code('Inventory Loss')
            credit_account_code = get_system_account_code('Inventory')
            desc = f"Stock loss for {product.name}: {reason}"

            try:
                cogs_from_loss, _ = consume_inventory_fifo(
                    product_id=product.id,
                    quantity_needed=abs(quantity),
                    adjustment_id=adjustment.id
                )
                adjustment_value = to_decimal(cogs_from_loss)
            except ValueError as e:
                db.session.rollback()
                flash(f'Error consuming inventory: {str(e)}', 'danger')
                return redirect(url_for('core.inventory'))

        else:
            debit_account_code = get_system_account_code('Inventory')
            credit_account_code = get_system_account_code('Inventory Gain')
            desc = f"Stock gain for {product.name}: {reason}"

            create_inventory_lot(
                product_id=product.id,
                quantity=quantity,
                unit_cost=to_decimal(product.cost_price),
                adjustment_id=adjustment.id,
                is_opening_balance=False
            )
            adjustment_value = (Decimal(quantity) * to_decimal(product.cost_price)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

        je_lines = [
            {"account_code": debit_account_code, "debit": format(adjustment_value, '0.2f'), "credit": "0.00"},
            {"account_code": credit_account_code, "debit": "0.00", "credit": format(adjustment_value, '0.2f')}
        ]
        journal = JournalEntry(description=desc, entries_json=json.dumps(je_lines))
        db.session.add(journal)

        log_action(f'Adjusted stock for {product.name} by {quantity}. Reason: {reason}.')
        db.session.commit()
        flash(f'Stock for {product.name} adjusted successfully.', 'success')

    except Exception as e:
        db.session.rollback()
        flash(f'Error adjusting stock: {str(e)}', 'danger')

    return redirect(url_for('core.inventory'))


@core_bp.route('/stock-adjustments')
@login_required
@role_required('Admin', 'Accountant')
def stock_adjustments():
    search = request.args.get('search', '').strip()
    status = request.args.get('status', '').strip()
    date_from = request.args.get('date_from', '').strip()

    query = StockAdjustment.query

    if search:
        query = query.join(Product).filter(
            (Product.name.ilike(f'%{search}%')) |
            (StockAdjustment.reason.ilike(f'%{search}%'))
        )

    if status == 'active':
        query = query.filter(StockAdjustment.voided_at.is_(None))
    elif status == 'voided':
        query = query.filter(StockAdjustment.voided_at.isnot(None))

    if date_from:
        try:
            date_obj = datetime.strptime(date_from, '%Y-%m-%d')
            query = query.filter(StockAdjustment.created_at >= date_obj)
        except ValueError:
            pass

    adjustments = query.order_by(StockAdjustment.created_at.desc()).all()

    all_active_products = Product.query.filter_by(is_active=True).order_by(Product.name).all()

    return render_template('stock_adjustments.html',
                           adjustments=adjustments,
                           all_active_products=all_active_products)


@core_bp.route('/audit-log')
@login_required
@role_required('Admin')
def audit_log():
    page = request.args.get('page', 1, type=int)
    logs = AuditLog.query.order_by(AuditLog.timestamp.desc()).paginate(page=page, per_page=25)
    return render_template('audit_log.html', logs=logs)


@core_bp.route('/inventory/lots/<int:product_id>')
@login_required
@role_required('Admin', 'Accountant')
def inventory_lots(product_id):
    from routes.fifo_utils import get_inventory_lots_summary, reconcile_inventory_lots

    product = Product.query.get_or_404(product_id)
    lots = get_inventory_lots_summary(product_id)
    reconciliation = reconcile_inventory_lots(product_id)

    total_qty = sum(lot['quantity'] for lot in lots)
    total_value = sum(to_decimal(lot['total_value']) for lot in lots)
    avg_cost = (total_value / Decimal(total_qty)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP) if total_qty > 0 else Decimal('0.00')

    return render_template('inventory_lots.html',
                           product=product,
                           lots=lots,
                           total_qty=total_qty,
                           total_value=total_value,
                           avg_cost=avg_cost,
                           reconciliation=reconciliation)


@core_bp.route('/inventory-movement/create', methods=['POST'])
@login_required
@role_required('Admin', 'Accountant')
def create_inventory_movement():
    movement_type = request.form.get('movement_type') or (request.json.get('movement_type') if request.json else None)
    from_branch_id = request.form.get('from_branch_id') or (request.json.get('from_branch_id') if request.json else None)
    to_branch_id = request.form.get('to_branch_id') or (request.json.get('to_branch_id') if request.json else None)
    notes = request.form.get('notes') or (request.json.get('notes') if request.json else None)

    if movement_type not in ['receive', 'transfer']:
        return jsonify({'error': 'Invalid movement type'}), 400

    items = []
    if movement_type == 'receive' and 'csv_file' in request.files:
        file = request.files['csv_file']
        if file.filename.endswith('.csv'):
            stream = io.StringIO(file.stream.read().decode("UTF-8"), newline=None)
            csv_reader = csv.reader(stream)
            for i, row in enumerate(csv_reader):
                if i == 0 and len(row) >= 1 and row[0].lower() == 'sku':
                    continue
                if len(row) >= 5:
                    sku, productname, sale_price, cost_price, qty = row[0], row[1], to_decimal(row[2]), to_decimal(row[3]), int(row[4])
                    product = Product.query.filter_by(sku=sku).first()
                    if product:
                        items.append({'sku': sku, 'quantity': qty, 'unit_cost': cost_price})
        else:
            return jsonify({'error': 'Invalid CSV file for receive'}), 400
    else:
        items_raw = request.form.getlist('items[][sku]') or (request.json.get('items', []) if request.json else [])
        if items_raw and isinstance(items_raw, list):
            for item in items_raw:
                if isinstance(item, dict):
                    sku = item.get('sku')
                    quantity = int(item.get('quantity'))
                    unit_cost = to_decimal(item.get('unit_cost'))
                    items.append({'sku': sku, 'quantity': quantity, 'unit_cost': unit_cost})
        else:
            quantities = request.form.getlist('items[][quantity]')
            unit_costs = request.form.getlist('items[][unit_cost]')
            for i in range(len(items_raw)):
                sku = items_raw[i]
                quantity = int(quantities[i])
                unit_cost = to_decimal(unit_costs[i])
                items.append({'sku': sku, 'quantity': quantity, 'unit_cost': unit_cost})

    if not items:
        return jsonify({'error': 'No items provided'}), 400

    try:
        movement = InventoryMovement(
            movement_type=movement_type,
            from_branch_id=from_branch_id,
            to_branch_id=to_branch_id,
            notes=notes,
            created_by=current_user.id
        )
        db.session.add(movement)
        db.session.flush()

        for item in items:
            sku = item['sku']
            quantity = int(item['quantity'])
            unit_cost = to_decimal(item['unit_cost'])

            product = Product.query.filter_by(sku=sku).first()
            if not product:
                db.session.rollback()
                return jsonify({'error': f'Product with SKU {sku} not found'}), 400

            product_id = product.id

            if movement_type == 'transfer' and product.quantity < quantity:
                db.session.rollback()
                return jsonify({
                    'error': f'Insufficient stock for {product.name}. Available: {product.quantity}, Requested: {quantity}'
                }), 400

            movement_item = InventoryMovementItem(
                movement_id=movement.id,
                product_id=product_id,
                quantity=quantity,
                unit_cost=unit_cost
            )
            db.session.add(movement_item)

            if movement_type == 'transfer' and from_branch_id:
                try:
                    cogs_value, _ = consume_inventory_fifo(
                        product_id=product.id,
                        quantity_needed=quantity,
                        adjustment_id=movement.id
                    )
                    cogs_value = to_decimal(cogs_value)
                    movement_item.unit_cost = (cogs_value / Decimal(quantity)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP) if quantity > 0 else unit_cost
                except ValueError as e:
                    db.session.rollback()
                    return jsonify({'error': f'FIFO error for {product.name}: {str(e)}'}), 400

                product.quantity -= quantity

            elif movement_type == 'receive' and to_branch_id:
                create_inventory_lot(
                    product_id=product.id,
                    quantity=quantity,
                    unit_cost=unit_cost,
                    movement_id=movement.id,
                    is_opening_balance=False
                )

                product.quantity += quantity

        db.session.commit()

        from_branch_name = movement.from_branch.name if movement.from_branch else 'N/A'
        to_branch_name = movement.to_branch.name if movement.to_branch else 'N/A'
        items_count = len(movement.items)

        return jsonify({
            'success': True,
            'message': 'Movement recorded successfully',
            'movement': {
                'id': movement.id,
                'date': movement.created_at.strftime('%Y-%m-%d'),
                'type': movement.movement_type.title(),
                'from': from_branch_name,
                'to': to_branch_name,
                'items': items_count,
                'notes': movement.notes or '-'
            },
            'download_url': url_for('core.export_movement_csv', movement_id=movement.id) if movement.movement_type == 'transfer' else None
        })

    except Exception as e:
        db.session.rollback()
        return jsonify({'error': f'Error creating movement: {str(e)}'}), 500


@core_bp.route('/branches', methods=['GET', 'POST'])
@login_required
@role_required('Admin')
def manage_branches():
    if request.method == 'POST':
        name = request.form.get('name')
        address = request.form.get('address')
        if not name:
            flash('Branch name is required', 'danger')
            return redirect(url_for('core.manage_branches'))

        branch = Branch(name=name, address=address)
        db.session.add(branch)
        db.session.commit()
        flash('Branch added successfully', 'success')
        return redirect(url_for('core.manage_branches'))

    branches = Branch.query.all()
    return render_template('manage_branches.html', branches=branches)


@core_bp.route('/inventory-movement')
@login_required
@role_required('Admin', 'Accountant')
def inventory_movement():
    branches = Branch.query.filter_by(is_active=True).all()
    movements = InventoryMovement.query.order_by(InventoryMovement.created_at.desc()).all()
    all_active_products = Product.query.filter_by(is_active=True).order_by(Product.name.asc()).all()

    company = CompanyProfile.query.first()
    default_branch_id = None
    if company and company.branch:
        default_branch = Branch.query.filter_by(name=company.branch, is_active=True).first()
        if default_branch:
            default_branch_id = default_branch.id

    return render_template('inventory_movement.html', branches=branches, movements=movements, all_active_products=all_active_products, default_branch_id=default_branch_id)


@core_bp.route('/inventory-movement/export/<int:movement_id>')
@login_required
@role_required('Admin', 'Accountant')
def export_movement_csv(movement_id):
    movement = InventoryMovement.query.get_or_404(movement_id)
    items = InventoryMovementItem.query.filter_by(movement_id=movement_id).all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['sku', 'productname', 'sale_price', 'cost_price', 'qty'])

    for item in items:
        product = Product.query.get(item.product_id)
        if product:
            writer.writerow([
                product.sku,
                product.name,
                format(to_decimal(product.sale_price), '0.2f'),
                format(to_decimal(product.cost_price), '0.2f'),
                item.quantity
            ])

    output.seek(0)
    filename = f"movement_{movement_id}_{movement.movement_type}.csv"

    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )