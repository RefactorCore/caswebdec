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


core_bp = Blueprint('core', __name__)
VAT_RATE = Config.VAT_RATE


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
    total_inventory_value = db.session.query(
        func.sum(InventoryLot.quantity_remaining * InventoryLot.unit_cost)
    ).join(Product).filter(
        Product.is_active == True,
        InventoryLot.quantity_remaining > 0
    ).scalar() or 0.0
    
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
    
    cash_sales_query = db.session.query(func.sum(Sale.total)).filter(Sale.voided_at == None)
    ar_sales_query = db.session.query(func.sum(ARInvoice.total)).filter(ARInvoice.voided_at == None)
    
    cash_purchases_query = db.session.query(func.sum(Purchase.total)).filter(Purchase.voided_at == None)
    ap_purchases_query = db.session.query(func.sum(APInvoice.total)).filter(APInvoice.voided_at == None)

    if start_date:
        cash_sales_query = cash_sales_query.filter(Sale.created_at >= start_date)
        ar_sales_query = ar_sales_query.filter(ARInvoice.date >= start_date)
        cash_purchases_query = cash_purchases_query.filter(Purchase.created_at >= start_date)
        ap_purchases_query = ap_purchases_query.filter(APInvoice.date >= start_date)

    # --- Execute FILTERED queries ---
    total_cash_sales = cash_sales_query.scalar() or 0
    total_ar_sales = ar_sales_query.scalar() or 0
    total_cash_purchases = cash_purchases_query.scalar() or 0
    total_ap_purchases = ap_purchases_query.scalar() or 0
    
    # 📊 COMBINED TOTALS (Cash + Credit)
    total_sales = total_cash_sales + total_ar_sales
    total_purchases = total_cash_purchases + total_ap_purchases

    # --- 📊 CALCULATE TRUE NET INCOME FROM ACCOUNTING RECORDS ---
    from routes.reports import aggregate_account_balances
    
    end_date = None
    if period != 'all':
        end_date = today
    
    agg = aggregate_account_balances(start_date, end_date)
    
    total_revenue = 0.0
    total_expenses = 0.0
    total_cogs = 0.0
    
    try:
        cogs_code = get_system_account_code('COGS')
    except:
        cogs_code = None
    
    for acc_code, bal in agg.items():
        acct_rec = Account.query.filter_by(code=acc_code).first()
        if not acct_rec:
            continue
        
        if acct_rec.type == 'Revenue':
            # Revenue accounts have credit balances (negative in agg)
            total_revenue += abs(bal)
        elif acct_rec.type == 'Expense':
            if cogs_code and acc_code == cogs_code:
                total_cogs += abs(bal)
            else:
                total_expenses += abs(bal)
    
    # 📊 TRUE NET INCOME (Revenue - COGS - Expenses)
    gross_profit = total_revenue - total_cogs
    net_income = gross_profit - total_expenses

    # --- Charting Logic (unchanged) ---
    sales_by_period = []
    labels = []
    
    if period == '12':
        intervals = [today - timedelta(hours=i) for i in range(11, -1, -1)]
        for hour_start in intervals:
            hour_end = hour_start + timedelta(hours=1)
            hour_total = (db.session.query(func.sum(Sale.total))
                          .filter(Sale.created_at >= hour_start)
                          .filter(Sale.created_at < hour_end)
                          .filter(Sale.voided_at == None)
                          .scalar() or 0)
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
            day_total = (db.session.query(func.sum(Sale.total))
                         .filter(func.date(Sale.created_at) == day)
                         .filter(Sale.voided_at == None)
                         .scalar() or 0)
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
        
        balance = inv.total - inv.paid
        
        if balance <= 0:
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
        
        balance = inv.total - inv.paid
        
        if balance <= 0:
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

    return render_template(
        'index.html',
        products=products,
        low_stock=low_stock,
        total_sales=total_sales,  # 📊 Now includes Cash + Credit Sales
        total_purchases=total_purchases,  # 📊 Now includes Cash + Credit Purchases
        net_income=net_income,  # 📊 True accounting net income
        gross_profit=gross_profit,  # 📊 NEW: Shows gross profit
        total_inventory_value=total_inventory_value,  # 📊 From FIFO lots
        products_in_stock=products_in_stock,
        labels=labels,
        sales_by_day=sales_by_period,
        top_sellers=top_sellers,
        current_period_filter=period,
        current_filter_label=current_filter_label,
        overdue_items=overdue_items,
        due_soon_items=due_soon_items,
        upcoming_items=upcoming_items
    )

@login_required
@core_bp.route('/inventory', methods=['GET', 'POST'])
def inventory():
    from models import Product

    # Handle product form submission (keeping this code block as is)
    if request.method == 'POST':
        data = request.form
        new_prod = Product(
            sku=data.get('sku'),
            name=data.get('name'),
            sale_price=float(data.get('sale_price') or 0),
            cost_price=float(data.get('cost_price') or 0),
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
    pagination = paginate_query(query, per_page=12) # Using 2 for testing

    # 👇 NEW: Create a dictionary of current arguments, excluding 'page'
    # This is the fix for the pagination link error.
    safe_args = {k: v for k, v in request.args.items() if k != 'page'}

    all_active_products = Product.query.filter_by(is_active=True).order_by(Product.name.asc()).all()


    has_opening_balance = db.session.query(JournalEntry.id)\
        .filter(JournalEntry.entries_json.ilike('%"account_code": "302"%'))\
        .first() is not None
    
    return render_template(
        'inventory.html',
        products=pagination.items,
        pagination=pagination,
        search=search,
        # 👇 NEW: Pass the filtered arguments dictionary
        safe_args=safe_args,
        all_active_products=all_active_products,
        has_opening_balance=has_opening_balance
    )

# ✅ Update product
@core_bp.route('/update_product', methods=['POST'])
def update_product():
    sku = request.form.get('sku')
    product = Product.query.filter_by(sku=sku).first()
    if not product:
        flash('Product not found.', 'danger')
        return redirect(request.referrer or url_for('core.inventory'))

    try:
        # Update fields
        product.name = request.form.get('name')
        product.sale_price = float(request.form.get('sale_price') or 0)
        product.cost_price = float(request.form.get('cost_price') or 0)

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


# --- ADD THIS ENTIRE FUNCTION ---

# Update inventory_bulk_add function (around line 285)

@core_bp.route('/inventory/bulk-add', methods=['GET', 'POST'])
@login_required
@role_required('Admin', 'Accountant')
def inventory_bulk_add():
    if request.method == 'POST':
        from routes.fifo_utils import create_inventory_lot
        from routes.sku_utils import generate_sku  # ✅ ADD THIS
        
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
            total_value = 0.0
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
                
                # ✅ UPDATED: CSV now only requires name, sale_price, cost_price, quantity
                # SKU column is IGNORED (always auto-generated)
                if len(row) < 4:
                    errors.append(f"Row {row_num}: Not enough columns (expected at least 4: name, sale_price, cost_price, quantity)")
                    skipped_count += 1
                    continue

                try:
                    # ✅ UPDATED: Parse without SKU (SKU will be auto-generated)
                    # Expected format: name, sale_price, cost_price, quantity, [optional: category]
                    name = row[0].strip() if len(row) > 0 else ''
                    sale_price = float(row[1] or 0.0) if len(row) > 1 else 0.0
                    cost_price = float(row[2] or 0.0) if len(row) > 2 else 0.0
                    quantity = int(row[3] or 0) if len(row) > 3 else 0
                    category = row[4].strip() if len(row) > 4 and row[4].strip() else None
                    
                    if not name:
                        errors.append(f"Row {row_num}: Missing product name")
                        skipped_count += 1
                        continue

                    # ✅ NEW: Auto-generate SKU
                    try:
                        sku = generate_sku(name, category=category)
                    except ValueError as e:
                        errors.append(f"Row {row_num}: {str(e)}")
                        skipped_count += 1
                        continue

                    # Create product
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
                        # Could not create a product even after retries
                        db.session.rollback()
                        errors.append(f"Row {row_num}: Failed to create product: {str(e)}")
                        skipped_count += 1
                        continue
                    db.session.add(new_prod)
                    db.session.flush()

                    # Create opening balance if qty and cost > 0
                    if quantity > 0 and cost_price > 0:
                        initial_value = round(quantity * cost_price, 2)
                        
                        debug_items.append({
                            'sku': sku,
                            'name': name,
                            'qty': quantity,
                            'cost': cost_price,
                            'value': initial_value
                        })
                        
                        total_value += initial_value
                        
                        # Create inventory lot
                        create_inventory_lot(
                            product_id=new_prod.id,
                            quantity=quantity,
                            unit_cost=cost_price,
                            is_opening_balance=True
                        )
                        
                        # Create journal entry
                        je_lines = [
                            {'account_code': inventory_code, 'debit': initial_value, 'credit': 0},
                            {'account_code': equity_code, 'debit': 0, 'credit': initial_value}
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
            if total_value > 0:
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

    # ✅ NEW: Pass category suggestions to template
    from routes.sku_utils import get_category_suggestions
    categories = get_category_suggestions()
    
    return render_template('inventory_bulk_add.html', categories=categories)


def create_product_with_retry(name, category, sale_price, cost_price, quantity, custom_sku=None, max_retries=3):
    """
    Try to create a Product record with an auto-generated SKU or a provided custom_sku.
    Retries on generated SKU collisions. If custom_sku is provided, it is validated and used;
    if it's invalid or already exists, generate_sku will raise ValueError and we will return that error.
    Returns (new_prod, sku) on success. Raises the last exception on fatal failure.
    """
    from datetime import datetime
    from routes.sku_utils import generate_sku
    from models import Product
    attempt = 0
    last_exc = None

    # If a custom_sku is provided, attempt once to use it (generate_sku will validate uniqueness/format).
    # We don't loop retries for a user-supplied SKU because it should be deterministic.
    if custom_sku:
        sku = None
        try:
            sku = generate_sku(name, category=category, custom_sku=custom_sku)
        except Exception as e:
            # Propagate validation error (e.g., duplicate or invalid format)
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
            # If duplicate despite validation, raise so caller can decide (no automatic retry for custom_sku)
            raise ie
        except Exception:
            db.session.rollback()
            raise

    # No custom_sku: generate and retry on collisions
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

    # Exhausted retries: fallback to timestamp-suffixed SKU
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
@role_required('Admin', 'Accountant') # Good to protect this
def api_add_multiple_products():
    data = request.json
    products_data = data.get('products', [])
    
    if not products_data:
        return jsonify({'error': 'No product data provided'}), 400

    new_product_count = 0
    total_value = 0.0
    errors = []
    
    try:
        # --- REFACTORED: Get codes once ---
        inventory_code = get_system_account_code('Inventory')
        equity_code = get_system_account_code('Opening Balance Equity')
        # --- END REFACTOR ---
        
        for p_data in products_data:
            # Basic validation
            sku = p_data.get('sku')
            name = p_data.get('name')
            
            if not sku or not name:
                errors.append(f"Skipped row (missing SKU or Name): {sku}")
                continue
            
            # Check if SKU already exists
            if Product.query.filter_by(sku=sku).first():
                 errors.append(f"SKU '{sku}' already exists. Skipped.")
                 continue 
            
            try:
                # Get data from payload
                initial_cost = float(p_data.get('cost_price') or 0)
                initial_qty = int(p_data.get('quantity') or 0)

                new_prod = Product(
                    sku=sku,
                    name=name,
                    sale_price=float(p_data.get('sale_price') or 0),
                    cost_price=initial_cost,
                    quantity=initial_qty
                )
                db.session.add(new_prod)
                
                # --- NEW: Create Beginning Balance Journal Entry ---
                if initial_qty > 0 and initial_cost > 0:
                    initial_value = round(initial_qty * initial_cost, 2)
                    total_value += initial_value
                    
                    # 120: Inventory, 302: Opening Balance Equity
                    je_lines = [
                        {'account_code': inventory_code, 'debit': initial_value, 'credit': 0},
                        {'account_code': equity_code, 'debit': 0, 'credit': initial_value}
                    ]
                    je = JournalEntry(
                        description=f'Beginning Balance for {new_prod.sku} ({new_prod.name})',
                        entries_json=json.dumps(je_lines)
                    )
                    db.session.add(je)
                # --- END OF NEW BLOCK ---
                
                new_product_count += 1
            
            except ValueError:
                errors.append(f"Invalid number for SKU '{sku}'. Skipped.")
            
        db.session.commit()
        
        log_action(f'Bulk-added {new_product_count} products with total beginning value of {total_value}.')
        
        # Check if there were errors to report
        if errors:
             return jsonify({
                'status': 'partial', 
                'count': new_product_count,
                'error': f'Added {new_product_count} products, but some failed. See errors.',
                'errors': errors
            }), 207 # 207 Multi-Status
            
        return jsonify({'status': 'ok', 'count': new_product_count})

    except exc.IntegrityError as e:
        db.session.rollback()
        return jsonify({'error': 'A database error occurred (e.g., duplicate SKU).', 'details': str(e)}), 500
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500

# Add this route (around line 1100, near other API routes)

@core_bp.route('/api/products/search')
def api_products_search():
    """
    Search products by SKU or name for purchase lookup.
    Returns JSON array of matching products.
    """
    query = request.args.get('q', '').strip()
    
    if query == '':
        # Return all active products if no search query (limited to 100)
        products = Product.query.filter_by(is_active=True)\
            .order_by(Product.name.asc())\
            .limit(100)\
            .all()
    else:
        # Search by SKU or name
        products = Product.query.filter(
            (Product.sku.ilike(f'%{query}%')) |
            (Product.name.ilike(f'%{query}%'))
        ).filter_by(is_active=True)\
        .order_by(Product.name.asc())\
        .limit(50)\
        .all()
    
    results = [{
        'id': p.id,
        'sku': p.sku,
        'name': p.name,
        'quantity': p.quantity,
        'cost_price': float(p.cost_price),
        'sale_price': float(p.sale_price)
    } for p in products]
    
    return jsonify(results)

# Update the purchase function (around line 430)

@core_bp.route('/purchase', methods=['GET', 'POST'])
@login_required
@role_required('Admin', 'Accountant', 'Cashier')
def purchase():
    if request.method == 'POST':
        try:
            from routes.fifo_utils import create_inventory_lot
            
            supplier_name = request.form.get('supplier', '').strip() or 'Unknown'
            items_raw = request.form.get('items_json')
            items = json.loads(items_raw) if items_raw else []

            if not items:
                flash("No items added to the purchase. Please add products first.", "warning")
                return redirect(url_for('core.purchase'))

            purchase_is_vatable = 'is_vatable' in request.form

            # Handle Supplier
            supplier = Supplier.query.filter_by(name=supplier_name).first()
            if not supplier and supplier_name != 'Unknown':
                supplier = Supplier(name=supplier_name)
                db.session.add(supplier)
                db.session.flush()

            # Create Purchase Record
            purchase = Purchase(total=0, vat=0, supplier=supplier_name, is_vatable=purchase_is_vatable)
            db.session.add(purchase)
            db.session.flush()

            total, vat_total = 0.0, 0.0

            for item in items:
                # 1. Handle SKU input: If "AUTO" or empty, we treat it as a request for Auto-Generation
                raw_sku = item.get('sku', '').strip()
                if raw_sku == 'AUTO' or raw_sku == '':
                    sku_arg = None # This tells create_product_with_retry to generate one
                else:
                    sku_arg = raw_sku # This tells it to use the specific SKU provided

                try:
                    qty = int(item.get('qty', 0))
                    unit_cost = float(item.get('unit_cost', 0))
                except (TypeError, ValueError):
                    continue

                name = item.get('name', 'Unnamed')

                # 2. Validation: We no longer check "if not sku". We only require Name and Qty.
                if qty <= 0 or unit_cost < 0:
                    continue

                line_net = round(qty * unit_cost, 2)
                vat = round(line_net * VAT_RATE, 2) if purchase_is_vatable else 0.0
                line_total = round(line_net + vat, 2)

                # 3. Find or Create Product
                # We only try to find it if a specific SKU was actually provided
                product = Product.query.filter_by(sku=sku_arg).first() if sku_arg else None

                final_sku = sku_arg # Default to input

                if not product:
                    # Product doesn't exist, create it (Auto-SKU or Custom SKU)
                    try:
                        product, generated_sku = create_product_with_retry(
                            name=name,
                            category=None, # You could add a category dropdown to the UI later
                            sale_price=round(unit_cost * 1.5, 2), # Default markup
                            cost_price=unit_cost,
                            quantity=qty,
                            custom_sku=sku_arg, # If None, it generates. If 'XYZ', it validates 'XYZ'.
                            max_retries=3
                        )
                        db.session.add(product)
                        db.session.flush()
                        
                        # IMPORTANT: Update final_sku to the actual generated string
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
                    # Product exists: Update quantity (FIFO logic handles cost separately)
                    product.quantity += qty
                    final_sku = product.sku 

                # 4. Create Purchase Item using the FINAL resolved SKU
                purchase_item = PurchaseItem(
                    purchase_id=purchase.id,
                    product_id=product.id,
                    product_name=product.name,
                    sku=final_sku, 
                    qty=qty,
                    unit_cost=unit_cost,
                    line_total=line_total
                )
                db.session.add(purchase_item)
                db.session.flush()

                # 5. Create Inventory Lot (FIFO)
                create_inventory_lot(
                    product_id=product.id,
                    quantity=qty,
                    unit_cost=unit_cost,
                    purchase_id=purchase.id,
                    purchase_item_id=purchase_item.id
                )

                total += line_total
                vat_total += vat

            # Update Purchase Totals
            purchase.total = round(total, 2)
            purchase.vat = round(vat_total, 2)

            # Create Journal Entry
            if purchase_is_vatable:
                journal_lines = [
                    {"account_code": get_system_account_code('Inventory'), "debit": round(total - vat_total, 2), "credit": 0},
                    {"account_code": get_system_account_code('VAT Input'), "debit": round(vat_total, 2), "credit": 0},
                    {"account_code": get_system_account_code('Accounts Payable'), "debit": 0, "credit": round(total, 2)}
                ]
            else:
                journal_lines = [
                    {"account_code": get_system_account_code('Inventory'), "debit": round(total, 2), "credit": 0},
                    {"account_code": get_system_account_code('Accounts Payable'), "debit": 0, "credit": round(total, 2)}
                ]

            journal = JournalEntry(
                description=f"Purchase #{purchase.id} - {supplier_name}",
                entries_json=json.dumps(journal_lines)
            )
            db.session.add(journal)
            
            log_action(f'Recorded Purchase #{purchase.id} from {supplier_name} for ₱{total:,.2f}.')
            db.session.commit()

            flash(f"✅ Purchase #{purchase.id} recorded successfully.", "success")
            return redirect(url_for('core.purchases'))

        except Exception as e:
            db.session.rollback()
            flash(f"❌ Error saving purchase: {str(e)}", "danger")
            return redirect(url_for('core.purchase'))

    # GET Request
    products = Product.query.filter_by(is_active=True).order_by(Product.name.asc()).all()
    suppliers = Supplier.query.order_by(Supplier.name).all()
    today = datetime.utcnow().strftime('%Y-%m-%d')

    return render_template('purchase.html', products=products, suppliers=suppliers, today=today)



# ✅ New: List all purchases
@core_bp.route('/purchases')
def purchases():
    purchases = Purchase.query.order_by(Purchase.id.desc()).all()
    return render_template('purchases.html', purchases=purchases)


# @core_bp.route('/delete_purchase/<int:purchase_id>', methods=['POST'])
# def delete_purchase(purchase_id):
#     purchase = Purchase.query.get_or_404(purchase_id)
#     log_action(f'Deleted Purchase #{purchase.id} (Supplier: {purchase.supplier}, Total: ₱{purchase.total:,.2f}).')
#     db.session.delete(purchase)
#     db.session.commit()
#     return jsonify({'status': 'deleted'})
@core_bp.route('/purchase/cancel/<int:purchase_id>', methods=['POST'])
@login_required
@role_required('Admin', 'Accountant') # Protect this action
def cancel_purchase(purchase_id):
    """
    Cancels a purchase by:
    1. Creating a reversing journal entry.
    2. Reversing the stock quantity adjustments.
    3. Marking the purchase as 'Canceled'.
    """
    purchase = Purchase.query.get_or_404(purchase_id)

    # 1. Check if already canceled
    if purchase.status == 'Canceled':
        flash(f'Purchase #{purchase.id} is already canceled.', 'warning')
        return redirect(url_for('core.purchases'))

    try:
        # 2. Calculate reversal amounts
        total_net = purchase.total - purchase.vat
        total_vat = purchase.vat
        total = purchase.total

        # 3. Create the reversing journal entry
        # Include VAT Input reversal only when the original purchase had VAT
        journal_lines = [
            {"account_code": get_system_account_code('Accounts Payable'), "debit": total, "credit": 0},
            {"account_code": get_system_account_code('Inventory'), "debit": 0, "credit": total_net},
        ]
        if total_vat and total_vat > 0:
            journal_lines.append({"account_code": get_system_account_code('VAT Input'), "debit": 0, "credit": total_vat})

        journal = JournalEntry(
            description=f"Reversal/Cancel of Purchase #{purchase.id} - {purchase.supplier}",
            entries_json=json.dumps(journal_lines)
        )
        db.session.add(journal)

        # --- 4. Reverse Product Quantities ---
        # We do not touch the average cost_price.
        for item in purchase.items:
            product = Product.query.get(item.product_id)
            if product:
                # Subtract the quantity from the product's stock
                product.quantity = max(0, product.quantity - item.qty)
        # --- End of New Block ---

        # 5. Update the purchase status
        purchase.status = 'Canceled'
        
        # 6. Log this compliant action
        log_action(f'Canceled Purchase #{purchase.id} (Supplier: {purchase.supplier}, Total: ₱{purchase.total:,.2f}). Reversing JE and stock adjustment created.')
        
        db.session.commit()
        flash(f'Purchase #{purchase.id} has been canceled. Journal entry posted and stock levels adjusted.', 'success')

    except Exception as e:
        db.session.rollback()
        flash(f'Error canceling purchase: {str(e)}', 'danger')

    return redirect(url_for('core.purchases'))


# ✅ New: View a specific purchase
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
    
    # --- Handle GET (view with pagination and search) ---
    search = request.args.get('search', '').strip()
    
    # Query regular products
    product_query = Product.query.filter_by(is_active=True)
    
    # Query consignment items
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
    
    # Get regular products (order by name)
    products = product_query.order_by(Product.name.asc()).all()
    
    # Get consignment items with available quantity
    consignment_items_raw = consignment_query.all()
    consignment_items = [item for item in consignment_items_raw if item.quantity_available > 0]
    
    # Combine into unified list for display
    combined_items = []
    
    # Add regular products
    for p in products:
        combined_items.append({
            'id': p.id,
            'sku': p.sku,
            'name': p.name,
            'price': p.sale_price,
            'quantity': p.quantity,
            'is_consignment': False,
            'type': 'regular'
        })
    
    # Add consignment items
    for c in consignment_items:
        combined_items.append({
            'id': c.id,
            'sku': c.sku,
            'name': c.product_name,
            'price': c.retail_price,
            'quantity': c.quantity_available,
            'is_consignment': True,
            'consignment_id': c.consignment_id,
            'type': 'consignment'
        })
    
    # Manual pagination for combined list
    per_page = 12
    page = request.args.get('page', 1, type=int)
    total_items = len(combined_items)
    total_pages = (total_items + per_page - 1) // per_page if total_items > 0 else 1
    start_idx = (page - 1) * per_page
    end_idx = start_idx + per_page
    paginated_items = combined_items[start_idx:end_idx]
    
    # ✅ FIXED: Complete Pagination class with iter_pages method
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
            """
            Generate page numbers for pagination display.
            Mimics Flask-SQLAlchemy's pagination.iter_pages() method.
            """
            last = 0
            for num in range(1, self.pages + 1):
                if (num <= left_edge or 
                    (num > self.page - left_current - 1 and num < self.page + right_current) or
                    num > self.pages - right_edge):
                    if last + 1 != num:
                        yield None  # Ellipsis
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
        current_user_name=current_user.username
    )


# (only the api_sale function is shown — replace the existing api_sale in routes/core.py with this)
# Update the api_sale function (around line 700)

@core_bp.route('/api/sale', methods=['POST'])
@login_required
def api_sale():
    # Import FIFO utilities at the top of the function
    from routes.fifo_utils import consume_inventory_fifo
    
    data = request.json or {}
    items = data.get('items', [])
    sale_is_vatable = bool(data.get('is_vatable', False))
    doc_type = data.get('doc_type', 'Invoice')
    discount = data.get('discount') or {}
    
    # ✅ FIX: Added 'sc_pwd' as a potential discount type
    discount_type = discount.get('type') or None # 'percent', 'fixed', or 'sc_pwd'
    discount_input = float(discount.get('input_value') or 0) if discount.get('input_value') is not None else 0.0

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

        sale = Sale(total=0, vat=0, document_number=full_doc_number, document_type=doc_type,
                    is_vatable=sale_is_vatable, customer_name=customer_name,
                    discount_type=discount_type, discount_input=discount_input)
        db.session.add(sale)
        db.session.flush()

        subtotal_gross = 0.0
        total_cogs = 0.0
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
                # Handle consignment item
                consignment_item_id = it.get('consignment_item_id')
                consignment_item = ConsignmentItem.query.get(consignment_item_id)
                
                if not consignment_item:
                    db.session.rollback()
                    return jsonify({'error': f'Consignment item {sku} not found'}), 404
                
                if consignment_item.quantity_available < qty:
                    db.session.rollback()
                    return jsonify({'error': f'Insufficient consignment stock for {consignment_item.product_name}'}), 400
                
                unit_price = float(consignment_item.retail_price)
                line_gross = round(unit_price * qty, 2)
                line_cogs = 0.0  # No COGS for consignment (we don't own it)
                
                product_name = consignment_item.product_name
                product_sku = consignment_item.sku
                
            else:
                # Handle regular product
                product = Product.query.filter_by(sku=sku).first()
                if not product:
                    db.session.rollback()
                    return jsonify({'error': f'Product {sku} not found'}), 404
                if product.quantity < qty:
                    db.session.rollback()
                    return jsonify({'error': f'Insufficient stock for {product.name}'}), 400

                unit_price = float(product.sale_price)
                line_gross = round(unit_price * qty, 2)
                
                try:
                    line_cogs, _ = consume_inventory_fifo(
                        product_id=product.id,
                        quantity_needed=qty,
                        sale_id=sale.id,
                        sale_item_id=None 
                    )
                except ValueError as e:
                    db.session.rollback()
                    return jsonify({'error': str(e)}), 400
                
                product_name = product.name
                product_sku = product.sku

            processed.append({
                'product': product if not is_consignment else None,
                'consignment_item': consignment_item if is_consignment else None,
                'qty': qty,
                'unit_price': unit_price,
                'line_gross': line_gross,
                'cogs': line_cogs,
                'is_consignment': is_consignment,
                'product_name': product_name,
                'product_sku': product_sku
            })

            subtotal_gross += line_gross
            total_cogs += line_cogs

        # ✅ FIX: Complete refactor of discount and VAT logic to be BIR-compliant
        # This new block correctly handles SC/PWD discounts while keeping
        # promo/fixed discounts functional. It also fixes the JE balancing.

        resolved_discount = 0.0
        
        # 1. Calculate totals for regular items
        regular_sales_gross = sum(p['line_gross'] for p in processed if not p['is_consignment'])
        
        # 2. Calculate totals for consignment items
        consignment_sales_gross = sum(p['line_gross'] for p in processed if p['is_consignment'])

        # 3. Handle different discount types
        if discount_type == 'sc_pwd' and sale_is_vatable:
            # --- SC/PWD LOGIC ---
            # Assumes SC/PWD discount applies to VATable regular items only.
            # Consignment items are not discounted in this case.
            
            regular_sales_net_base = round(regular_sales_gross / (1 + VAT_RATE), 2)
            pct = max(0.0, min(100.0, float(discount_input))) # Should be 20
            
            resolved_discount = round(regular_sales_net_base * (pct / 100.0), 2)
            
            regular_sales_final_price = round(regular_sales_net_base - resolved_discount, 2)
            regular_sales_vat_final = 0.0 # SC/PWD sales are VAT-Exempt
            regular_sales_net_final = regular_sales_final_price
            
            # Consignment items are unaffected
            consignment_sales_final_price = consignment_sales_gross
            # (We assume consignment VAT logic follows main flag)
            if sale_is_vatable:
                consignment_vat_final = round(consignment_sales_final_price * (VAT_RATE / (1 + VAT_RATE)), 2)
            else:
                consignment_vat_final = 0.0
            consignment_net_final = round(consignment_sales_final_price - consignment_vat_final, 2)
            
            # Final totals
            total_amount = round(regular_sales_final_price + consignment_sales_final_price, 2)
            vat_after = round(regular_sales_vat_final + consignment_vat_final, 2)

        else:
            # --- STANDARD DISCOUNT LOGIC (Percent or Fixed) ---
            if discount_type and discount_input:
                if discount_type == 'percent':
                    pct = max(0.0, min(100.0, float(discount_input)))
                    resolved_discount = round(subtotal_gross * (pct / 100.0), 2)
                else: # 'fixed'
                    resolved_discount = round(min(subtotal_gross, float(discount_input)), 2)
            
            # Apportion discount proportionally
            if subtotal_gross > 0:
                regular_discount_share = round(resolved_discount * (regular_sales_gross / subtotal_gross), 2)
                consignment_discount_share = round(resolved_discount * (consignment_sales_gross / subtotal_gross), 2)
            else:
                regular_discount_share = 0.0
                consignment_discount_share = 0.0

            # Calculate finals for regular items
            regular_sales_post_discount = regular_sales_gross - regular_discount_share
            if sale_is_vatable:
                 regular_sales_vat_final = round(regular_sales_post_discount * (VAT_RATE / (1 + VAT_RATE)), 2)
            else:
                 regular_sales_vat_final = 0.0
            regular_sales_net_final = round(regular_sales_post_discount - regular_sales_vat_final, 2)
            
            # Calculate finals for consignment items
            consignment_sales_post_discount = consignment_sales_gross - consignment_discount_share
            if sale_is_vatable:
                consignment_vat_final = round(consignment_sales_post_discount * (VAT_RATE / (1 + VAT_RATE)), 2)
            else:
                consignment_vat_final = 0.0
            consignment_net_final = round(consignment_sales_post_discount - consignment_vat_final, 2)

            # Final totals
            vat_after = round(regular_sales_vat_final + consignment_vat_final, 2)
            total_amount = round(regular_sales_post_discount + consignment_sales_post_discount, 2)

        # --- Update Sale object with final calculated totals ---
        sale.discount_value = resolved_discount
        sale.total = total_amount
        sale.vat = vat_after
        db.session.flush()

        # Insert sale items and deduct stock
        for p in processed:
            if p['is_consignment']:
                # Handle consignment item sale
                consignment_item = p['consignment_item']
                
                # Create sale item (with product_id = None for consignment)
                sale_item = SaleItem(
                    sale_id=sale.id,
                    product_id=None,  # No product_id for consignment items
                    product_name=p['product_name'],
                    sku=p['product_sku'],
                    qty=p['qty'],
                    unit_price=p['unit_price'],
                    line_total=p['line_gross'],
                    cogs=0.0  # No COGS for consignment
                )
                db.session.add(sale_item)
                
                # Update consignment item quantity
                consignment_item.quantity_sold += p['qty']
                
                # Update consignment status if needed
                consignment = consignment_item.consignment
                total_received = sum(item.quantity_received for item in consignment.items)
                total_sold = sum(item.quantity_sold for item in consignment.items)
                total_returned = sum(item.quantity_returned for item in consignment.items)
                
                if total_sold + total_returned >= total_received:
                    consignment.status = 'Closed'
                elif total_sold > 0 or total_returned > 0:
                    consignment.status = 'Partial'
                
            else:
                # Handle regular product
                product = p['product']
                sale_item = SaleItem(
                    sale_id=sale.id,
                    product_id=product.id,
                    product_name=product.name,
                    sku=product.sku,
                    qty=p['qty'],
                    unit_price=p['unit_price'],
                    line_total=p['line_gross'],
                    cogs=p['cogs']
                )
                db.session.add(sale_item)
                product.quantity -= p['qty']

        # Calculate consignment commission (based on pre-discount gross)
        consignment_sales_total = sum(p['line_gross'] for p in processed if p['is_consignment'])
        consignment_commission_total = 0.0

        if consignment_sales_total > 0:
            # Group consignment sales by consignment_id
            consignment_groups = {}
            for p in processed:
                if p['is_consignment']:
                    cons_id = p['consignment_item'].consignment_id
                    if cons_id not in consignment_groups:
                        consignment = p['consignment_item'].consignment
                        consignment_groups[cons_id] = {
                            'consignment': consignment,
                            'total': 0.0
                        }
                    consignment_groups[cons_id]['total'] += p['line_gross']
            
            # Calculate commission for each consignment
            for cons_id, group in consignment_groups.items():
                commission_rate = group['consignment'].commission_rate / 100 if group['consignment'].commission_rate else 0
                commission = round(group['total'] * commission_rate, 2)
                consignment_commission_total += commission

        # ✅ FIX: Journal Entry logic refactored to be balanced and correct
        je_lines = []

        # 1. Cash received
        je_lines.append({'account_code': get_system_account_code('Cash'), 'debit': float(total_amount), 'credit': 0})

        # 2. Consignment Commission Revenue (based on pre-discount)
        if consignment_commission_total > 0:
            je_lines.append({
                'account_code': get_system_account_code('Consignment Commission Revenue'), 
                'debit': 0, 
                'credit': float(consignment_commission_total)
            })

        # 3. Consignment Payable (pre-discount sales - commission)
        if consignment_sales_total > 0:
            consignment_payable = consignment_sales_total - consignment_commission_total
            je_lines.append({
                'account_code': get_system_account_code('Consignment Payable'), 
                'debit': 0, 
                'credit': float(consignment_payable)
            })
        
        # 4. Discount
        discount_acc_code = get_system_account_code('Discounts Allowed')
        if resolved_discount and resolved_discount > 0:
            je_lines.append({'account_code': discount_acc_code, 'debit': float(resolved_discount), 'credit': 0})
        
        # 5. COGS (regular products only)
        if total_cogs and total_cogs > 0:
            je_lines.append({'account_code': get_system_account_code('COGS'), 'debit': float(total_cogs), 'credit': 0})

        # 6. Sales Revenue (FIXED: Uses final post-discount/pre-tax value)
        if regular_sales_net_final > 0:
            je_lines.append({'account_code': get_system_account_code('Sales Revenue'), 'debit': 0, 'credit': float(regular_sales_net_final)})

        # 7. VAT Payable (FIXED: Uses final calculated VAT for both)
        final_total_vat = regular_sales_vat_final + (consignment_vat_final if 'consignment_vat_final' in locals() else 0.0)
        if final_total_vat > 0:
            je_lines.append({'account_code': get_system_account_code('VAT Payable'), 'debit': 0, 'credit': float(final_total_vat)})

        # 8. Inventory (regular products only)
        if total_cogs and total_cogs > 0:
            je_lines.append({'account_code': get_system_account_code('Inventory'), 'debit': 0, 'credit': float(total_cogs)})

        # Rounding adjustment
        total_debits = sum(float(l.get('debit', 0) or 0) for l in je_lines)
        total_credits = sum(float(l.get('credit', 0) or 0) for l in je_lines)

        rounding_diff = round(total_debits - total_credits, 2)
        if abs(rounding_diff) >= 0.01:
            adjusted = False
            # Try to adjust Sales Revenue first
            sales_revenue_code = get_system_account_code('Sales Revenue')
            for l in je_lines:
                if l.get('account_code') == sales_revenue_code:
                    l['credit'] = round(l['credit'] + rounding_diff, 2)
                    adjusted = True
                    break
            
            # If not, adjust discount
            if not adjusted:
                for l in je_lines:
                    if l.get('account_code') == discount_acc_code and l.get('debit', 0) >= abs(rounding_diff):
                        l['debit'] = round(l['debit'] - rounding_diff, 2)
                        adjusted = True
                        break
            
            # Fallback to cash
            if not adjusted:
                cash_code = get_system_account_code('Cash')
                for l in je_lines:
                    if l.get('account_code') == cash_code:
                        l['debit'] = round(l['debit'] - rounding_diff, 2)
                        adjusted = True
                        break
        
        # Final balance check
        total_debits = sum(float(l.get('debit', 0) or 0) for l in je_lines)
        total_credits = sum(float(l.get('credit', 0) or 0) for l in je_lines)

        if round(total_debits, 2) != round(total_credits, 2):
            db.session.rollback()
            return jsonify({'error': f'Journal entry balancing failed. D={total_debits}, C={total_credits}'}), 500

        db.session.add(JournalEntry(description=f'Sale #{sale.id} ({full_doc_number})', entries_json=json.dumps(je_lines)))

        log_action(f'Recorded Sale #{sale.id} ({full_doc_number}) for ₱{total_amount:,.2f}. Customer: {customer_name}. Discount: ₱{resolved_discount:.2f}')
        db.session.commit()

        return jsonify({
            'status': 'ok',
            'sale_id': sale.id,
            'receipt_number': full_doc_number,
            'vat': vat_after,
            'discount_value': resolved_discount
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

    # ✅ Parse dates properly
    start_date = None
    end_date = None
    
    if start_date_str:
        try:
            start_date = datetime.strptime(start_date_str, '%Y-%m-%d')
        except ValueError:
            flash('Invalid start date format', 'warning')
    
    if end_date_str:
        try:
            # Add 1 day to make end date inclusive (end of day)
            end_date = datetime.strptime(end_date_str, '%Y-%m-%d') + timedelta(days=1)
        except ValueError:
            flash('Invalid end date format', 'warning')

    # ✅ Query both cash sales (POS) and billing invoices (AR)
    cash_sales_query = Sale.query
    ar_invoices_query = ARInvoice.query

    cash_sales_query = cash_sales_query.filter(Sale.voided_at == None)
    ar_invoices_query = ar_invoices_query.filter(ARInvoice.voided_at == None)

    # ✅ FIXED: Apply date filters BEFORE search (more efficient)
    if start_date:
        cash_sales_query = cash_sales_query.filter(Sale.created_at >= start_date)
        ar_invoices_query = ar_invoices_query.filter(ARInvoice.date >= start_date)

    if end_date:
        # ✅ FIX: Use <= instead of < for inclusive end date
        cash_sales_query = cash_sales_query.filter(Sale.created_at <= end_date)
        ar_invoices_query = ar_invoices_query.filter(ARInvoice.date <= end_date)

    # ✅ FIXED: Simplified search - search happens AFTER combining results
    # Get all results first
    cash_sales = cash_sales_query.all()
    billing_invoices = ar_invoices_query.all()

    # ✅ Combine into unified sales list
    all_sales = []
    
    # Add cash sales (POS)
    for s in cash_sales:
        all_sales.append({
            'id': s.id,
            'type': 'Cash Sale',
            'date': s.created_at,
            'document_number': s.document_number or f"Sale-{s.id}",
            'customer_name': s.customer_name or 'Walk-in',
            'total': s.total,
            'vat': s.vat or 0.0,
            'discount_value': s.discount_value or 0.0,
            'status': s.status or 'paid',
            'paid': s.total,
            'balance': 0.0,
            'created_at': s.created_at
        })
    
    # Add billing invoices (AR)
    for inv in billing_invoices:
        all_sales.append({
            'id': inv.id,
            'type': 'Billing Invoice',
            'date': inv.date,
            'document_number': inv.invoice_number or f"AR-{inv.id}",
            'customer_name': inv.customer.name if inv.customer else 'N/A',
            'total': inv.total,
            'vat': inv.vat or 0.0,
            'discount_value': 0.0,
            'status': inv.status,
            'paid': inv.paid,
            'balance': inv.total - inv.paid,
            'created_at': inv.date
        })
    
    # ✅ NEW: Apply search filter AFTER combining (searches across all fields)
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
    
    # Sort by date descending
    all_sales.sort(key=lambda x: x['date'], reverse=True)
    
    # Manual pagination
    total_count = len(all_sales)
    total_pages = (total_count + per_page - 1) // per_page if total_count > 0 else 1
    start_idx = (page - 1) * per_page
    end_idx = start_idx + per_page
    paginated_sales = all_sales[start_idx:end_idx]
    
    # Create pagination object
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
    
    # Calculate summary
    total_sales = sum(s['total'] for s in all_sales)
    total_vat = sum(s['vat'] for s in all_sales)
    total_discount = sum(s['discount_value'] for s in all_sales)
    
    summary = {
        "total_sales": total_sales,
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
    return render_template('receipt.html', sale=sale, items=items)

@core_bp.route('/export_sales')
def export_sales():
    from models import Sale, ARInvoice, Customer
    
    format_type = request.args.get('format', 'csv')
    
    # Optional filters (same as in your sales page)
    search = request.args.get('search', '').strip()
    start_date = request.args.get('start_date', '')
    end_date = request.args.get('end_date', '')

    # Query both cash sales and AR invoices
    cash_query = Sale.query
    ar_query = ARInvoice.query

    # Apply filters
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
        # Generate CSV in memory
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["Type", "Doc #", "Date", "Customer", "Total", "Paid", "Balance", "VAT", "Discount", "Status"])

        # Add cash sales
        for s in cash_sales:
            writer.writerow([
                "Cash Sale",
                s.document_number or f"Sale-{s.id}",
                s.created_at.strftime('%Y-%m-%d %H:%M') if s.created_at else "",
                s.customer_name or "Walk-in",
                f"{s.total:.2f}",
                f"{s.total:.2f}",  # Fully paid
                "0.00",
                f"{(s.vat or 0):.2f}",
                f"{(s.discount_value or 0):.2f}",
                s.status or "paid"
            ])
        
        # Add billing invoices
        for inv in ar_invoices:
            writer.writerow([
                "Billing Invoice",
                inv.invoice_number or f"AR-{inv.id}",
                inv.date.strftime('%Y-%m-%d %H:%M') if inv.date else "",
                inv.customer.name if inv.customer else "N/A",
                f"{inv.total:.2f}",
                f"{inv.paid:.2f}",
                f"{(inv.total - inv.paid):.2f}",
                f"{(inv.vat or 0):.2f}",
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

    # Default fallback
    return redirect(url_for('core.sales'))


@core_bp.route('/sales/<int:sale_id>')
def view_sale(sale_id):
    from models import SaleItem, Sale  # adjust imports per your structure

    sale = Sale.query.get_or_404(sale_id)
    items = SaleItem.query.filter_by(sale_id=sale.id).all()

    return render_template('view_sale.html', sale=sale, items=items)

@core_bp.route('/journal-entries')
@role_required('Admin', 'Accountant')
def journal_entries():
    """Display all journal entries with search and date filters."""
    search = request.args.get('search', '')
    start_date_str = request.args.get('start_date', '')
    end_date_str = request.args.get('end_date', '')

    query = JournalEntry.query.order_by(JournalEntry.created_at.desc())

    # --- THIS IS NEW: Create a map of Account Codes -> Account Names ---
    # We will pass this to the template to display names instead of codes
    accounts_map = {a.code: a.name for a in Account.query.all()}
    
    # --- UPDATED: Search/Filter Logic ---
    safe_args = {}
    if search:
        safe_args['search'] = search
        # Updated to search for account_code instead of account name
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
            pass # ignore invalid date
    if end_date_str:
        try:
            end_date = datetime.strptime(end_date_str, '%Y-%m-%d')
            # Add 1 day to end_date to make it inclusive
            end_date = end_date + timedelta(days=1)
            query = query.filter(JournalEntry.created_at <= end_date)
            safe_args['end_date'] = end_date_str
        except ValueError:
            pass # ignore invalid date
    # --- END OF UPDATED LOGIC ---

    pagination = paginate_query(query)
    
    return render_template(
        'reports.html',
        journals=pagination.items,
        pagination=pagination,
        accounts_map=accounts_map,  # <-- Pass the map to the template
        safe_args=safe_args,
        start_date=start_date_str,
        end_date=end_date_str
    )

@core_bp.route('/export/journal-entries')
@login_required
@role_required('Admin', 'Accountant')
def export_journal_entries():
    """Export journal entries to CSV, respecting filters."""
    search = request.args.get('search', '')
    start_date_str = request.args.get('start_date', '')
    end_date_str = request.args.get('end_date', '')

    query = JournalEntry.query.order_by(JournalEntry.created_at.asc())
    
    # --- Create the account map just like in the main function ---
    accounts_map = {a.code: a.name for a in Account.query.all()}

    # --- Apply filters just like in the main function ---
    if search:
        query = query.filter(
            (JournalEntry.description.ilike(f'%{search}%')) |
            (JournalEntry.entries_json.ilike(f'%\"account_code\": \"{search}\"%'))
        )
    if start_date_str:
        try:
            start_date = datetime.strptime(start_date_str, '%Y-%m-%d')
            query = query.filter(JournalEntry.created_at >= start_date)
        except ValueError: pass
    if end_date_str:
        try:
            end_date = datetime.strptime(end_date_str, '%Y-%m-%d') + timedelta(days=1)
            query = query.filter(JournalEntry.created_at <= end_date)
        except ValueError: pass

    journals = query.all()
    
    # --- Create CSV ---
    si = io.StringIO()
    writer = csv.writer(si)
    
    # Write Header
    writer.writerow(['Journal_ID', 'Date', 'Description', 'Account_Code', 'Account_Name', 'Debit', 'Credit'])
    
    # Write Rows
    for je in journals:
        je_date = je.created_at.strftime('%Y-%m-%d %H:%M')
        for line in je.entries():
            code = line.get('account_code')
            # Use the map to find the name
            name = accounts_map.get(code, code) # Fallback to code if not found
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
    """Export journal entries as CSV."""
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

@core_bp.route('/new_journal')
def new_journal():
    return "📝 Journal entry creation page (coming soon)"


@core_bp.route('/vat_report')
def vat_report():
    """
    Redirect old core VAT report route to the canonical reports.vat_report endpoint.
    Keeps backward compatibility for any links using /vat_report.
    """
    # Preserve possible filters if present
    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')
    return redirect(url_for('reports.vat_report', start_date=start_date, end_date=end_date))


def parse_date(date_str):
    """Helper to safely parse YYYY-MM-DD format strings."""
    try:
        return datetime.strptime(date_str, "%Y-%m-%d")
    except (ValueError, TypeError):
        return None

@core_bp.route('/export_vat', methods=['GET'])
def export_vat_report():
    # --- Parse optional date filters ---
    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')
    return redirect(url_for('reports.export_vat_report', start_date=start_date, end_date=end_date))

    # --- Build queries ---
    sale_query = Sale.query
    purchase_query = Purchase.query

    if start_date:
        sale_query = sale_query.filter(Sale.created_at >= start_date)
        purchase_query = purchase_query.filter(Purchase.created_at >= start_date)
    if end_date:
        sale_query = sale_query.filter(Sale.created_at <= end_date)
        purchase_query = purchase_query.filter(Purchase.created_at <= end_date)

    # --- Compute VAT totals ---
    sale_vat = sum(s.vat or 0 for s in sale_query.all())
    purchase_vat = sum(p.vat or 0 for p in purchase_query.all())
    vat_payable = sale_vat - purchase_vat

    # --- Prepare CSV output ---
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Type", "Amount (₱)"])
    writer.writerow(["Input VAT (from Purchases)", f"{purchase_vat:.2f}"])
    writer.writerow(["Output VAT (from Sales)", f"{sale_vat:.2f}"])

    if vat_payable >= 0:
        writer.writerow(["VAT Payable", f"{vat_payable:.2f}"])
    else:
        writer.writerow(["VAT Refund", f"{abs(vat_payable):.2f}"])

    output.seek(0)

    # --- Return downloadable CSV ---
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={
            "Content-Disposition": "attachment; filename=vat_report.csv"
        },
    )





@core_bp.route('/api/product/<sku>')
def api_product(sku):
    product = Product.query.filter_by(sku=sku).first()
    if not product:
        return jsonify({'error': 'Product not found'}), 404

    return jsonify({
        'sku': product.sku,
        'name': product.name,
        'sale_price': float(product.sale_price or 0),
        'cost_price': float(product.cost_price or 0),
        'quantity': product.quantity
    })



# --- ADD THIS LOGIN ROUTE ---
@core_bp.route('/login', methods=['GET', 'POST'])
@limiter.limit("10 per minute") 
def login():
    """Handles user login."""
    if current_user.is_authenticated:
        return redirect(url_for('core.index'))

    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')

        # --- ADD THIS: Basic Input Validation ---
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
            # Hash the new password and update the user
            user.password_hash = pbkdf2_sha256.hash(new_password)
            db.session.commit()
            
            # Log this action (without a specific user in session)
            log_action(f'Password for user {username} was reset via TIN verification.')

            flash('Password has been reset successfully. You can now log in.', 'success')
            return redirect(url_for('core.login'))
        else:
            flash('User not found.', 'danger')

    # For the GET request, fetch all users to populate the dropdown
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
    """Handles user logout."""
    # 1. Capture the user's ID and username before logging out
    user_id_to_log = current_user.id
    username_to_log = current_user.username

    # 2. Log the user out, which clears the session
    logout_user()

    # 3. Manually create the AuditLog entry with the saved details
    try:
        log = AuditLog(
            user_id=user_id_to_log,
            action=f'User {username_to_log} logged out successfully.',
            ip_address=request.remote_addr
        )
        db.session.add(log)
        db.session.commit()
    except Exception as e:
        # In case of a database error, we don't want to crash the logout process
        # You could add proper logging here if needed
        print(f"Error creating audit log for logout: {e}")
        db.session.rollback()

    flash('You have been logged out.', 'info')
    return redirect(url_for('core.login'))


@core_bp.route('/settings', methods=['GET', 'POST'])
@login_required
@role_required('Admin')
def settings():
    # We assume only one company profile exists
    profile = CompanyProfile.query.first_or_404()
    if request.method == 'POST':
        profile.name = request.form.get('name')
        profile.tin = request.form.get('tin')
        profile.address = request.form.get('address')
        profile.business_style = request.form.get('business_style')
        profile.branch = request.form.get('branch')
        
        # Auto-create Branch record if company branch doesn't exist
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
    # --- FIX: Import FIFO utils ---

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
        # 1. Update Product Quantity
        original_qty = product.quantity
        product.quantity += quantity
        
        # 2. Create Stock Adjustment Log
        adjustment = StockAdjustment(
            product_id=product.id,
            quantity_changed=quantity,
            reason=reason,
            user_id=current_user.id
        )
        db.session.add(adjustment)
        db.session.flush() # Flush to get adjustment.id

        # 3. Create Journal Entry & Handle FIFO Lots
        adjustment_value = abs(quantity) * product.cost_price
        
        if quantity < 0: # Stock reduction (loss)
            debit_account_code = get_system_account_code('Inventory Loss')
            credit_account_code = get_system_account_code('Inventory')
            desc = f"Stock loss for {product.name}: {reason}"
            
            # --- FIX: Consume from FIFO lots ---
            try:
                # Use abs(quantity) because consume_inventory_fifo expects a positive number
                cogs_from_loss, _ = consume_inventory_fifo(
                    product_id=product.id,
                    quantity_needed=abs(quantity),
                    adjustment_id=adjustment.id
                )
                # Use the *actual* cost from FIFO for the journal entry
                adjustment_value = cogs_from_loss
            except ValueError as e:
                db.session.rollback()
                flash(f'Error consuming inventory: {str(e)}', 'danger')
                return redirect(url_for('core.inventory'))
            # --- END FIX ---

        else: # Stock increase (gain)
            debit_account_code = get_system_account_code('Inventory')
            credit_account_code = get_system_account_code('Inventory Gain')
            desc = f"Stock gain for {product.name}: {reason}"

            # --- FIX: Create new inventory lot ---
            create_inventory_lot(
                product_id=product.id,
                quantity=quantity,
                unit_cost=product.cost_price, # Use product's current cost_price for gains
                adjustment_id=adjustment.id,
                is_opening_balance=False 
            )
            # Use the product's cost_price for the journal entry
            adjustment_value = quantity * product.cost_price
            # --- END FIX ---

        je_lines = [
            {"account_code": debit_account_code, "debit": adjustment_value, "credit": 0},
            {"account_code": credit_account_code, "debit": 0, "credit": adjustment_value}
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
    """List all stock adjustments with search and filters"""
    search = request.args.get('search', '').strip()
    status = request.args.get('status', '').strip()
    date_from = request.args.get('date_from', '').strip()
    
    query = StockAdjustment.query
    
    # Apply search filter
    if search:
        query = query.join(Product).filter(
            (Product.name.ilike(f'%{search}%')) |
            (StockAdjustment.reason.ilike(f'%{search}%'))
        )
    
    # Apply status filter
    if status == 'active':
        query = query.filter(StockAdjustment.voided_at.is_(None))
    elif status == 'voided':
        query = query.filter(StockAdjustment.voided_at.isnot(None))
    
    # Apply date filter
    if date_from:
        try:
            date_obj = datetime.strptime(date_from, '%Y-%m-%d')
            query = query.filter(StockAdjustment.created_at >= date_obj)
        except ValueError:
            pass
    
    adjustments = query.order_by(StockAdjustment.created_at.desc()).all()
    
    # Get all active products for the adjustment modal
    all_active_products = Product.query.filter_by(is_active=True).order_by(Product.name).all()
    
    return render_template('stock_adjustments.html', 
                         adjustments=adjustments,
                         all_active_products=all_active_products)


# Add this new route for viewing the logs
@core_bp.route('/audit-log')
@login_required
@role_required('Admin')
def audit_log():
    """Display the audit log."""
    page = request.args.get('page', 1, type=int)
    logs = AuditLog.query.order_by(AuditLog.timestamp.desc()).paginate(page=page, per_page=25)
    return render_template('audit_log.html', logs=logs)


# Add this new route

@core_bp.route('/inventory/lots/<int:product_id>')
@login_required
@role_required('Admin', 'Accountant')
def inventory_lots(product_id):
    """View FIFO inventory lots for a specific product"""
    from routes.fifo_utils import get_inventory_lots_summary, reconcile_inventory_lots
    
    product = Product.query.get_or_404(product_id)
    lots = get_inventory_lots_summary(product_id)
    reconciliation = reconcile_inventory_lots(product_id)
    
    total_qty = sum(lot['quantity'] for lot in lots)
    total_value = sum(lot['total_value'] for lot in lots)
    avg_cost = total_value / total_qty if total_qty > 0 else 0.0
    
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
    movement_type = request.form.get('movement_type') or request.json.get('movement_type')
    from_branch_id = request.form.get('from_branch_id') or request.json.get('from_branch_id')
    to_branch_id = request.form.get('to_branch_id') or request.json.get('to_branch_id')
    notes = request.form.get('notes') or request.json.get('notes')
    
    if movement_type not in ['receive', 'transfer']:
        return jsonify({'error': 'Invalid movement type'}), 400

    # Handle CSV upload for receive
    items = []
    if movement_type == 'receive' and 'csv_file' in request.files:
        file = request.files['csv_file']
        if file.filename.endswith('.csv'):
            stream = io.StringIO(file.stream.read().decode("UTF-8"), newline=None)
            csv_reader = csv.reader(stream)
            for i, row in enumerate(csv_reader):
                if i == 0 and len(row) >= 1 and row[0].lower() == 'sku':  # Skip header row
                    continue
                if len(row) >= 5:
                    sku, productname, sale_price, cost_price, qty = row[0], row[1], float(row[2]), float(row[3]), int(row[4])
                    product = Product.query.filter_by(sku=sku).first()
                    if product:
                        items.append({'sku': sku, 'quantity': qty, 'unit_cost': cost_price})
        else:
            return jsonify({'error': 'Invalid CSV file for receive'}), 400
    else:
        # Manual entry (from JSON)
        items_raw = request.form.getlist('items[][sku]') or request.json.get('items', [])
        if items_raw and isinstance(items_raw, list):
            for item in items_raw:
                if isinstance(item, dict):
                    sku = item.get('sku')
                    quantity = item.get('quantity')
                    unit_cost = item.get('unit_cost')
                    items.append({'sku': sku, 'quantity': quantity, 'unit_cost': unit_cost})
        else:
            # Fallback for form data
            quantities = request.form.getlist('items[][quantity]')
            unit_costs = request.form.getlist('items[][unit_cost]')
            for i in range(len(items_raw)):
                sku = items_raw[i]
                quantity = int(quantities[i])
                unit_cost = float(unit_costs[i])
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
        db.session.flush()  # Get movement.id

        for item in items:
            sku = item['sku']
            quantity = item['quantity']
            unit_cost = item['unit_cost']
            
            # Look up product by SKU
            product = Product.query.filter_by(sku=sku).first()
            if not product:
                db.session.rollback()
                return jsonify({'error': f'Product with SKU {sku} not found'}), 400
            
            product_id = product.id
            
            # ✅ FIX: Check stock availability for transfers
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

            # ✅ FIX: Handle FIFO lots correctly
            if movement_type == 'transfer' and from_branch_id:
                # Transfer OUT: Consume FIFO lots (like a sale)
                try:
                    cogs_value, _ = consume_inventory_fifo(
                        product_id=product.id,
                        quantity_needed=quantity,
                        adjustment_id=movement.id  # Link to movement for tracking
                    )
                    # Update unit_cost with actual FIFO cost
                    movement_item.unit_cost = cogs_value / quantity if quantity > 0 else unit_cost
                except ValueError as e:
                    db.session.rollback()
                    return jsonify({'error': f'FIFO error for {product.name}: {str(e)}'}), 400
                
                # Reduce quantity
                product.quantity -= quantity
                
            elif movement_type == 'receive' and to_branch_id:
                # Receive IN: Create new FIFO lot (like a purchase)
                create_inventory_lot(
                    product_id=product.id,
                    quantity=quantity,
                    unit_cost=unit_cost,
                    movement_id=movement.id,
                    is_opening_balance=False
                )
                
                # Increase quantity
                product.quantity += quantity

        db.session.commit()
        
        # Prepare response data for dynamic update
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
    
    # Get default branch from company profile
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
    writer.writerow(['sku', 'productname', 'sale_price', 'cost_price', 'qty'])  # Updated header
    
    for item in items:
        product = Product.query.get(item.product_id)
        if product:
            writer.writerow([
                product.sku,
                product.name,
                product.sale_price,
                product.cost_price,
                item.quantity
            ])
    
    output.seek(0)
    filename = f"movement_{movement_id}_{movement.movement_type}.csv"
    
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )