from flask import Blueprint, render_template, request, redirect, url_for, flash, send_file
from flask_login import login_required, current_user
from models import db, Customer, Supplier, ARInvoice, APInvoice, Payment, JournalEntry, CreditMemo, Account, Product, ARInvoiceItem, RecurringBill
import io, csv
import json
from .decorators import role_required
from .utils import log_action, get_system_account_code
from models import Product, ARInvoiceItem, Payment
from datetime import datetime, timedelta

ar_ap_bp = Blueprint('ar_ap', __name__, url_prefix='')


@ar_ap_bp.route('/customers', methods=['GET', 'POST'])
@login_required
@role_required('Admin', 'Accountant')
def customers():
    if request.method == 'POST':
        name = request.form.get('name')
        tin = request.form.get('tin')
        addr = request.form.get('address')
        if not name:
            flash('Customer name is required')
            return redirect(url_for('ar_ap.customers'))
        c = Customer(name=name, tin=tin, address=addr)
        db.session.add(c)
        log_action(f'Created new customer: {name} (TIN: {tin}).')
        db.session.commit()
        flash('Customer added')
        return redirect(url_for('ar_ap.customers'))
    custs = Customer.query.order_by(Customer.name).all()
    return render_template('customers.html', customers=custs)


@ar_ap_bp.route('/suppliers', methods=['GET', 'POST'])
@login_required
@role_required('Admin', 'Accountant')
def suppliers():
    if request.method == 'POST':
        name = request.form.get('name')
        tin = request.form.get('tin')
        addr = request.form.get('address')
        if not name:
            flash('Supplier name is required')
            return redirect(url_for('ar_ap.suppliers'))
        s = Supplier(name=name, tin=tin, address=addr)
        db.session.add(s)
        log_action(f'Created new supplier: {name} (TIN: {tin}).')
        db.session.commit()
        flash('Supplier added')
        return redirect(url_for('ar_ap.suppliers'))
    sups = Supplier.query.order_by(Supplier.name).all()
    return render_template('suppliers.html', suppliers=sups)


@ar_ap_bp.route('/ar-invoices', methods=['GET', 'POST'])
@login_required
@role_required('Admin', 'Accountant')
def ar_invoices():
    """
    Create AR invoice (credit sale). Creates a JournalEntry:
      Debit Accounts Receivable (net + vat)
      Credit Sales Revenue (net)
      Credit VAT Payable (vat)
    """
    if request.method == 'POST':
        try:
            cust_id = int(request.form.get('customer_id') or 0) or None
        except ValueError:
            cust_id = None
        total = float(request.form.get('total') or 0)
        vat = float(request.form.get('vat') or 0)
        if total <= 0:
            flash('Invoice total must be > 0')
            return redirect(url_for('ar_ap.ar_invoices'))

        try:
            inv = ARInvoice(customer_id=cust_id, total=round(total, 2), vat=round(vat, 2))
            db.session.add(inv)
            db.session.flush()

            # Journal entry
            je_lines = [
                    {'account_code': get_system_account_code('Accounts Receivable'), 'debit': round(inv.total, 2), 'credit': 0},
                    {'account_code': get_system_account_code('Sales Revenue'), 'debit': 0, 'credit': round(inv.total - inv.vat, 2)},
                    {'account_code': get_system_account_code('VAT Payable'), 'debit': 0, 'credit': round(inv.vat, 2)},
                ]
            
            je = JournalEntry(description=f'AR Invoice #{inv.id}', entries_json=json.dumps(je_lines))
            db.session.add(je)
            log_action(f'Created AR Invoice #{inv.id} for ₱{inv.total:,.2f}.')
            db.session.commit()
            flash('AR Invoice created and journal entry recorded.')
        
        except Exception as e:
            db.session.rollback()
            flash(f'An error occurred: {str(e)}', 'danger')
        # --- END ADD ---

        return redirect(url_for('ar_ap.ar_invoices'))

    invoices = ARInvoice.query.order_by(ARInvoice.date.desc()).all()
    customers = Customer.query.order_by(Customer.name).all()
    return render_template('ar_invoices.html', invoices=invoices, customers=customers)


@ar_ap_bp.route('/ap-invoices', methods=['GET', 'POST'])
@login_required
@role_required('Admin', 'Accountant')
def ap_invoices():
    """
    Create AP invoice (credit purchase). Creates a JournalEntry:
      Debit Inventory / Expense (net) - User Selected
      Debit VAT Input (vat)
      Credit Accounts Payable (total)
    """
    if request.method == 'POST':
        try:
            sup_id = int(request.form.get('supplier_id') or 0) or None
        except ValueError:
            sup_id = None
            
        total = float(request.form.get('total') or 0)
        vat = float(request.form.get('vat') or 0)
        
        # --- NEW FIELDS ---
        invoice_number = request.form.get('invoice_number')
        description = request.form.get('description')
        is_vatable = request.form.get('is_vatable') == 'true'
        
        # Handle due date
        due_date_str = request.form.get('due_date')
        due_date = None
        if due_date_str:
            try:
                due_date = datetime.strptime(due_date_str, '%Y-%m-%d')
            except ValueError:
                flash('Invalid due date format. Please use YYYY-MM-DD.', 'danger')
                return redirect(url_for('ar_ap.ap_invoices'))
        
        # Handle expense account
        default_inv_code = get_system_account_code('Inventory')
        expense_account_code = request.form.get('expense_account_code') or default_inv_code
        
        if not is_vatable:
            vat = 0.0 # Force VAT to zero if not vatable

        if total <= 0:
            flash('Invoice total must be > 0')
            return redirect(url_for('ar_ap.ap_invoices'))
        
        if not sup_id:
            flash('Please select a supplier.')
            return redirect(url_for('ar_ap.ap_invoices'))
        
        if not expense_account_code:
            flash('Please select a debit account.')
            return redirect(url_for('ar_ap.ap_invoices'))

        try:
            inv = APInvoice(
                supplier_id=sup_id, 
                total=round(total, 2), 
                vat=round(vat, 2),
                invoice_number=invoice_number,
                description=description,
                due_date=due_date,
                is_vatable=is_vatable,
                expense_account_code=expense_account_code
            )
            db.session.add(inv)
            db.session.flush()

            # --- UPDATED JOURNAL ENTRY ---
            # Debits the user-selected account
            je_lines = [
                    {'account_code': expense_account_code, 'debit': round(inv.total - inv.vat, 2), 'credit': 0},
                    {'account_code': get_system_account_code('VAT Input'), 'debit': round(inv.vat, 2), 'credit': 0},
                    {'account_code': get_system_account_code('Accounts Payable'), 'debit': 0, 'credit': round(inv.total, 2)},
                ]
            
            # Remove VAT Input line if VAT is zero
            if inv.vat == 0:
                je_lines.pop(1) # Removes the VAT input line

            je = JournalEntry(description=f'AP Invoice #{inv.id} ({inv.invoice_number}) - {inv.description}', entries_json=json.dumps(je_lines))
            db.session.add(je)
            log_action(f'Created AP Invoice #{inv.id} for ₱{inv.total:,.2f}.')
            db.session.commit()
            flash('AP Invoice created and journal entry recorded.')
            
        except Exception as e:
            db.session.rollback()
            flash(f'An error occurred: {str(e)}', 'danger')
        # --- END ADD ---

        return redirect(url_for('ar_ap.ap_invoices'))

    # --- UPDATED GET REQUEST ---
    invoices = APInvoice.query.order_by(APInvoice.date.desc()).all()
    suppliers = Supplier.query.order_by(Supplier.name).all()
    
    # Get accounts that can be debited (Expenses and "Inventory" Asset)
    accounts = Account.query.filter(
        (Account.type == 'Expense') | (Account.code == get_system_account_code('Inventory'))
    ).order_by(Account.name).all()
    
    return render_template(
        'ap_invoices.html', 
        invoices=invoices, 
        suppliers=suppliers,
        accounts=accounts # Pass accounts to the template
    )


@ar_ap_bp.route('/payment', methods=['POST'])
@login_required
@role_required('Admin', 'Accountant')
def record_payment():
    """
    Record payment for AR or AP and create corresponding journal entry.
      AR payment: Debit Cash, Debit CWT, Credit Accounts Receivable
      AP payment: Debit Accounts Payable, Credit Cash
    """
    ref_type = request.form.get('ref_type')
    try:
        ref_id = int(request.form.get('ref_id') or 0)
    except ValueError:
        flash('Invalid reference ID.', 'danger')
        return redirect(url_for('ar_ap.ar_invoices'))

    try:
        amount = float(request.form.get('amount') or 0.0)
        wht_amount = float(request.form.get('wht_amount') or 0.0)
    except ValueError:
        flash('Invalid amount or WHT value.', 'danger')
        return redirect(request.referrer or url_for('ar_ap.ar_invoices'))

    method = request.form.get('method') or 'Cash'
    total_credited = round(amount + wht_amount, 2)

    if total_credited <= 0:
        flash('Total credited amount (Amount + WHT) must be > 0.', 'warning')
        return redirect(request.referrer or url_for('ar_ap.ar_invoices'))

    if ref_type == 'AR':
        inv = ARInvoice.query.get(ref_id)
        if not inv:
            flash(f'AR Invoice {ref_id} not found.', 'danger')
            return redirect(url_for('ar_ap.ar_invoices'))

        inv.paid += total_credited

        if inv.paid >= (inv.total - 0.001):
            inv.status = 'Paid'
        else:
            inv.status = 'Partially Paid'
            
        je_lines = [
            {'account_code': get_system_account_code('Cash'), 'debit': round(amount, 2), 'credit': 0},
            {'account_code': get_system_account_code('Creditable Withholding Tax'), 'debit': round(wht_amount, 2), 'credit': 0},
            {'account_code': get_system_account_code('Accounts Receivable'), 'debit': 0, 'credit': total_credited}
        ]
        
        redirect_url = url_for('ar_ap.ar_invoices')

    elif ref_type == 'AP':
        inv = APInvoice.query.get(ref_id)
        if not inv:
            flash(f'AP Invoice {ref_id} not found.', 'danger')
            return redirect(url_for('ar_ap.ap_invoices'))
        
        inv.paid += amount
        inv.status = 'Paid' if inv.paid >= inv.total else 'Partially Paid'
        
        je_lines = [
            {'account_code': get_system_account_code('Accounts Payable'), 'debit': round(amount, 2), 'credit': 0},
            {'account_code': get_system_account_code('Cash'), 'debit': 0, 'credit': round(amount, 2)}
        ]
        
        redirect_url = url_for('ar_ap.ap_invoices')
        
    else:
        flash('Unknown reference type.', 'danger')
        return redirect(url_for('core.index'))

    try:
        p = Payment(
            amount=round(amount, 2), 
            ref_type=ref_type, 
            ref_id=ref_id, 
            method=method, 
            wht_amount=round(wht_amount, 2),
            date=datetime.utcnow()
        )
        db.session.add(p)
        db.session.flush()

        # ✅ FIX: Create JE with only valid parameters
        je = JournalEntry(
            description=f'Payment for {ref_type} #{ref_id}', 
            entries_json=json.dumps(je_lines)
        )
        db.session.add(je)
        
        log_action(f'Recorded Payment #{p.id} of ₱{p.amount:,.2f} (WHT: ₱{p.wht_amount:,.2f}) for {ref_type} #{ref_id}.')
        db.session.commit()
        
        flash('Payment recorded and journal entry created.', 'success')
        return redirect(request.referrer or redirect_url)

    except Exception as e:
        db.session.rollback()
        flash(f'An error occurred: {str(e)}', 'danger')
        return redirect(request.referrer or redirect_url)

# --- ADD THIS NEW ROUTE ---
@ar_ap_bp.route('/credit-memos', methods=['GET', 'POST'])
@login_required
@role_required('Admin', 'Accountant')
def credit_memos():
    # --- FIX: Import new models and utils ---
    from routes.fifo_utils import create_inventory_lot
    # Note: Product, ARInvoiceItem are already imported in your file

    if request.method == 'POST':
        customer_id = int(request.form.get('customer_id'))
        ar_invoice_id = int(request.form.get('ar_invoice_id') or 0) or None
        reason = request.form.get('reason')
        total_amount = float(request.form.get('total_amount') or 0)

        # --- FIX: Read new (optional) fields for inventory return ---
        # You must update your HTML form to send these fields
        return_product_id = int(request.form.get('return_product_id') or 0) or None
        return_quantity = int(request.form.get('return_quantity') or 0) or None
        # --- END FIX ---

        if not customer_id or total_amount <= 0:
            flash('Customer and a valid amount are required.', 'danger')
            return redirect(url_for('ar_ap.credit_memos'))

        # Calculate net and VAT (assuming 12% VAT)
        amount_net = round(total_amount / 1.12, 2)
        vat = round(total_amount - amount_net, 2)

        cm = CreditMemo(
            customer_id=customer_id,
            ar_invoice_id=ar_invoice_id,
            reason=reason,
            amount_net=amount_net,
            vat=vat,
            total_amount=total_amount
        )
        db.session.add(cm)
        db.session.flush()

        if ar_invoice_id:
            inv = ARInvoice.query.get(ar_invoice_id)
            if inv:
                inv.paid += total_amount 
                remaining_balance = inv.total - inv.paid
                if remaining_balance <= 0.01: # Add tolerance
                    inv.status = 'Paid'
                elif remaining_balance < inv.total:
                    inv.status = 'Partially Paid'

        # Journal Entry
        je_lines = [
                {'account_code': get_system_account_code('Sales Returns'), 'debit': amount_net, 'credit': 0},
                {'account_code': get_system_account_code('VAT Payable'), 'debit': vat, 'credit': 0},
                {'account_code': get_system_account_code('Accounts Receivable'), 'debit': 0, 'credit': total_amount}
            ]
        
        # --- FIX: Add logic to handle inventory return ---
        if return_product_id and return_quantity:
            product = Product.query.get(return_product_id)
            if product:
                # 1. Add item back to stock
                product.quantity += return_quantity
                
                # 2. Get the item's original cost
                # We will find the *original* cost from the AR Invoice item
                # If not found, we'll fall back to the product's current cost_price
                return_cost = product.cost_price 
                if ar_invoice_id:
                    original_item = ARInvoiceItem.query.filter_by(
                        ar_invoice_id=ar_invoice_id, 
                        product_id=return_product_id
                    ).first()
                    if original_item and original_item.cogs > 0 and original_item.qty > 0:
                        return_cost = original_item.cogs / original_item.qty
                
                # 3. Create a new inventory lot for the returned item
                create_inventory_lot(
                    product_id=product.id,
                    quantity=return_quantity,
                    unit_cost=return_cost,
                    is_opening_balance=False
                )
                
                # 4. Add COGS reversal to Journal Entry
                total_cogs_reversal = round(return_cost * return_quantity, 2)
                if total_cogs_reversal > 0:
                    je_lines.append({
                        'account_code': get_system_account_code('Inventory'), 
                        'debit': total_cogs_reversal, 
                        'credit': 0
                    })
                    je_lines.append({
                        'account_code': get_system_account_code('COGS'), 
                        'debit': 0, 
                        'credit': total_cogs_reversal
                    })

                log_action(f'Returned {return_quantity} of {product.name} to inventory via CM #{cm.id}.')
        # --- END FIX ---

        je = JournalEntry(description=f'Credit Memo #{cm.id} for {reason}', entries_json=json.dumps(je_lines))
        db.session.add(je)
        log_action(f'Created Credit Memo #{cm.id} for ₱{cm.total_amount:,.2f} (Reason: {reason}).')
        db.session.commit()
        flash('Credit Memo created successfully.', 'success')
        return redirect(url_for('ar_ap.credit_memos'))

    # GET request logic
    memos = CreditMemo.query.order_by(CreditMemo.date.desc()).all()
    customers = Customer.query.order_by(Customer.name).all()
    invoices = ARInvoice.query.filter(ARInvoice.status != 'Paid').order_by(ARInvoice.id.desc()).all()
    
    # --- FIX: Pass products to template for the new dropdown ---
    products = Product.query.filter_by(is_active=True).order_by(Product.name).all()
    
    return render_template('credit_memos.html', 
                           memos=memos, 
                           customers=customers, 
                           invoices=invoices,
                           products=products) # <-- Pass products


# Update the billing_invoices function (around line 354)

@ar_ap_bp.route('/billing-invoices', methods=['GET', 'POST'])
@login_required
@role_required('Admin', 'Accountant','Cashier')
def billing_invoices():
    from models import Product, ARInvoiceItem
    from datetime import datetime, timedelta
    from routes.fifo_utils import consume_inventory_fifo
    
    if request.method == 'POST':
        try:
            customer_id = int(request.form.get('customer_id') or 0)
            description = request.form.get('description', '')
            is_vatable = request.form.get('is_vatable') == 'true'
            
            due_date_str = request.form.get('due_date')
            if due_date_str:
                due_date = datetime.strptime(due_date_str, '%Y-%m-%d')
            else:
                customer = Customer.query.get(customer_id)
                payment_terms = customer.payment_terms_days if customer and hasattr(customer, 'payment_terms_days') else 30
                due_date = datetime.utcnow() + timedelta(days=payment_terms)
            
            product_ids = request.form.getlist('product_id[]')
            quantities = request.form.getlist('quantity[]')
            unit_prices = request.form.getlist('unit_price[]')
            line_vatables = request.form.getlist('line_vatable[]')
            
            if not customer_id:
                flash('Please select a customer', 'danger')
                return redirect(url_for('ar_ap.billing_invoices'))
            
            if not product_ids:
                flash('Please add at least one product', 'danger')
                return redirect(url_for('ar_ap.billing_invoices'))
            
            line_items = []
            subtotal = 0.0
            total_vat = 0.0
            
            for i in range(len(product_ids)):
                product_id = int(product_ids[i])
                qty = int(quantities[i])
                unit_price = float(unit_prices[i])
                line_is_vatable = line_vatables[i] == 'true'
                
                product = Product.query.get(product_id)
                if not product:
                    flash(f'Product ID {product_id} not found', 'danger')
                    return redirect(url_for('ar_ap.billing_invoices'))
                
                if product.quantity < qty:
                    flash(f'Insufficient stock for {product.name}. Available: {product.quantity}, Requested: {qty}', 'danger')
                    return redirect(url_for('ar_ap.billing_invoices'))
                
                line_total = qty * unit_price
                line_vat = 0.0
                
                if line_is_vatable:
                    net_amount = line_total / 1.12
                    line_vat = line_total - net_amount
                    total_vat += line_vat
                
                if line_total <= 0 or line_vat < 0:
                    flash(f'Invalid line total or VAT for product ID {product_id}', 'danger')
                    return redirect(url_for('ar_ap.billing_invoices'))
                
                line_items.append({
                    'product_id': product_id,
                    'product_name': product.name,
                    'sku': product.sku,
                    'qty': qty,
                    'unit_price': unit_price,
                    'line_total': line_total,
                    'is_vatable': line_is_vatable,
                    'cogs': 0.0  # Will be set after consumption
                })
                
                subtotal += line_total
            
            invoice_total = subtotal
            
            from models import CompanyProfile
            company = CompanyProfile.query.first()
            if company:
                invoice_number = f"INV-{company.next_invoice_number:05d}"
                company.next_invoice_number += 1
            else:
                invoice_number = f"INV-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
            
            ar_invoice = ARInvoice(
                customer_id=customer_id,
                total=round(invoice_total, 2),
                vat=round(total_vat, 2),
                paid=0.0,
                is_vatable=(is_vatable or (total_vat > 0.0)),
                status='Open',
                invoice_number=invoice_number,
                description=description,
                due_date=due_date
            )
            db.session.add(ar_invoice)
            db.session.flush()
            
            for item in line_items:
                ar_item = ARInvoiceItem(
                    ar_invoice_id=ar_invoice.id,
                    product_id=item['product_id'],
                    product_name=item['product_name'],
                    sku=item['sku'],
                    qty=item['qty'],
                    unit_price=item['unit_price'],
                    line_total=item['line_total'],
                    cogs=item['cogs'],
                    is_vatable=item['is_vatable']
                )
                db.session.add(ar_item)
            db.session.flush()
            
            # ✅ NOW consume FIFO after invoice and items are created
            total_cogs = 0.0
            for idx, item in enumerate(line_items):
                ar_item = ARInvoiceItem.query.filter_by(
                    ar_invoice_id=ar_invoice.id,
                    product_id=item['product_id'],
                    qty=item['qty'],
                    unit_price=item['unit_price']
                ).first()
                
                if ar_item:
                    try:
                        line_cogs, _ = consume_inventory_fifo(
                            product_id=item['product_id'],
                            quantity_needed=item['qty'],
                            ar_invoice_id=ar_invoice.id,
                            ar_invoice_item_id=ar_item.id
                        )
                        item['cogs'] = line_cogs
                        ar_item.cogs = line_cogs
                        total_cogs += line_cogs
                        product = Product.query.get(item['product_id'])
                        if product:
                            product.quantity -= item['qty']
                    except ValueError as e:
                        db.session.rollback()
                        flash(f'FIFO error for {item["product_name"]}: {str(e)}', 'danger')
                        return redirect(url_for('ar_ap.billing_invoices'))
            
            je_lines = [
                {'account_code': get_system_account_code('Accounts Receivable'), 'debit': round(invoice_total, 2), 'credit': 0},
                {'account_code': get_system_account_code('Sales Revenue'), 'debit': 0, 'credit': round(invoice_total - total_vat, 2)},
            ]
            
            if total_vat > 0:
                je_lines.append({'account_code': get_system_account_code('VAT Payable'), 'debit': 0, 'credit': round(total_vat, 2)})
            
            je_lines.extend([
                {'account_code': get_system_account_code('COGS'), 'debit': round(total_cogs, 2), 'credit': 0},
                {'account_code': get_system_account_code('Inventory'), 'debit': 0, 'credit': round(total_cogs, 2)}
            ])
            
            je = JournalEntry(description=f'Billing Invoice {invoice_number} - {description}', entries_json=json.dumps(je_lines))
            db.session.add(je)
            
            log_action(f'Created Billing Invoice {invoice_number} for ₱{invoice_total:,.2f} (Due: {due_date.strftime("%Y-%m-%d")})')
            db.session.commit()
            
            flash(f'Billing Invoice {invoice_number} created successfully! Due date: {due_date.strftime("%Y-%m-%d")}', 'success')
            return redirect(url_for('ar_ap.billing_invoices'))
            
        except Exception as e:
            db.session.rollback()
            flash(f'Error creating billing invoice: {str(e)}', 'danger')
            return redirect(url_for('ar_ap.billing_invoices'))
    
    invoices = ARInvoice.query.filter(ARInvoice.items.any()).order_by(ARInvoice.date.desc()).all()
    customers = Customer.query.order_by(Customer.name).all()
    products_query = Product.query.filter_by(is_active=True).order_by(Product.name).all()
    
    products_list = []
    for p in products_query:
        products_list.append({
            'id': p.id,
            'name': p.name,
            'sku': p.sku,
            'sale_price': float(p.sale_price),
            'cost_price': float(p.cost_price),
            'quantity': p.quantity
        })
    
    return render_template('billing_invoices.html', 
                         invoices=invoices, 
                         customers=customers,
                         products=products_list,
                         Payment=Payment)  # ✅ Pass Payment model


@ar_ap_bp.route('/export/ar.csv')
@login_required
def export_ar_csv():
    invoices = ARInvoice.query.order_by(ARInvoice.date.desc()).all()
    si = io.StringIO()
    writer = csv.DictWriter(si, fieldnames=['id', 'date', 'customer_id', 'total', 'vat', 'paid', 'status'])
    writer.writeheader()
    for inv in invoices:
        writer.writerow({
            'id': inv.id,
            'date': inv.date.strftime('%Y-%m-%d'),
            'customer_id': inv.customer_id or '',
            'total': f"{inv.total:.2f}",
            'vat': f"{inv.vat:.2f}",
            'paid': f"{inv.paid:.2f}",
            'status': inv.status
        })
    return send_file(io.BytesIO(si.getvalue().encode('utf-8')), mimetype='text/csv', download_name='ar_invoices.csv', as_attachment=True)


@ar_ap_bp.route('/export/ap.csv')
@login_required
def export_ap_csv():
    invoices = APInvoice.query.order_by(APInvoice.date.desc()).all()
    si = io.StringIO()
    writer = csv.DictWriter(si, fieldnames=['id', 'date', 'supplier_id', 'total', 'vat', 'paid', 'status'])
    writer.writeheader()
    for inv in invoices:
        writer.writerow({
            'id': inv.id,
            'date': inv.date.strftime('%Y-%m-%d'),
            'supplier_id': inv.supplier_id or '',
            'total': f"{inv.total:.2f}",
            'vat': f"{inv.vat:.2f}",
            'paid': f"{inv.paid:.2f}",
            'status': inv.status
        })
    return send_file(io.BytesIO(si.getvalue().encode('utf-8')), mimetype='text/csv', download_name='ap_invoices.csv', as_attachment=True)

@ar_ap_bp.route('/recurring-bills', methods=['GET', 'POST'])
@login_required
@role_required('Admin', 'Accountant')
def recurring_bills():
    """
    Manage (create, list) recurring bill templates.
    """
    if request.method == 'POST':
        try:
            supplier_id = int(request.form.get('supplier_id'))
            expense_account_code = request.form.get('expense_account_code')
            description = request.form.get('description')
            total = float(request.form.get('total'))
            vat = float(request.form.get('vat') or 0.0)
            is_vatable = request.form.get('is_vatable') == 'true'
            frequency = request.form.get('frequency') # e.g., 'monthly'
            next_due_date_str = request.form.get('next_due_date')
            
            if not supplier_id or not expense_account_code or total <= 0 or not frequency or not next_due_date_str:
                flash('Please fill out all required fields.', 'danger')
                return redirect(url_for('ar_ap.recurring_bills'))

            next_due_date = datetime.strptime(next_due_date_str, '%Y-%m-%d')
            
            if not is_vatable:
                vat = 0.0

            bill = RecurringBill(
                supplier_id=supplier_id,
                expense_account_code=expense_account_code,
                description=description,
                total=round(total, 2),
                vat=round(vat, 2),
                is_vatable=is_vatable,
                frequency=frequency,
                next_due_date=next_due_date,
                is_active=True
            )
            db.session.add(bill)
            log_action(f'Created new recurring bill for {description}.')
            db.session.commit()
            flash('Recurring bill created successfully.', 'success')

        except Exception as e:
            db.session.rollback()
            flash(f'Error creating recurring bill: {str(e)}', 'danger')
        
        return redirect(url_for('ar_ap.recurring_bills'))

    # GET request
    bills = RecurringBill.query.filter_by(is_active=True).order_by(RecurringBill.next_due_date).all()
    suppliers = Supplier.query.order_by(Supplier.name).all()
    accounts = Account.query.filter(
        (Account.type == 'Expense') | (Account.code == get_system_account_code('Inventory'))
    ).order_by(Account.name).all()

    return render_template(
        'recurring_bills.html', 
        bills=bills, 
        suppliers=suppliers, 
        accounts=accounts
    )


@ar_ap_bp.route('/recurring-bills/generate/<int:bill_id>', methods=['POST'])
@login_required
@role_required('Admin', 'Accountant')
def generate_recurring_bill(bill_id):
    """
    Generates a new APInvoice from a RecurringBill template.
    """
    bill = RecurringBill.query.get_or_404(bill_id)
    
    try:
        # 1. Create the new APInvoice
        inv = APInvoice(
            supplier_id=bill.supplier_id,
            total=bill.total,
            vat=bill.vat,
            description=f"(Recurring) {bill.description}",
            due_date=bill.next_due_date,
            is_vatable=bill.is_vatable,
            expense_account_code=bill.expense_account_code,
            status='Open' # Explicitly set status
        )
        db.session.add(inv)
        db.session.flush() # Need the inv.id for the journal entry

        # 2. Create the Journal Entry
        je_lines = [
            {'account_code': bill.expense_account_code, 'debit': round(inv.total - inv.vat, 2), 'credit': 0},
            {'account_code': get_system_account_code('VAT Input'), 'debit': round(inv.vat, 2), 'credit': 0},
            {'account_code': get_system_account_code('Accounts Payable'), 'debit': 0, 'credit': round(inv.total, 2)},
        ]
        if inv.vat == 0:
            je_lines.pop(1) # Remove VAT input line

        je = JournalEntry(description=f'Recurring AP Invoice #{inv.id} - {inv.description}', entries_json=json.dumps(je_lines))
        db.session.add(je)

        # 3. Update the RecurringBill's next_due_date
        today = datetime.utcnow()
        if bill.frequency == 'monthly':
            # This is a simple way; a more robust way would use dateutil.relativedelta
            next_due = bill.next_due_date + timedelta(days=30)
            # Ensure next_due is in the future
            while next_due <= today:
                next_due += timedelta(days=30)
            bill.next_due_date = next_due
            
        elif bill.frequency == 'quarterly':
            next_due = bill.next_due_date + timedelta(days=90)
            while next_due <= today:
                next_due += timedelta(days=90)
            bill.next_due_date = next_due

        # (add more frequencies like 'annually' as needed)

        log_action(f'Generated AP Invoice #{inv.id} from recurring bill #{bill.id}.')
        db.session.commit()
        flash(f'Successfully generated AP Invoice #{inv.id}.', 'success')

    except Exception as e:
        db.session.rollback()
        flash(f'Error generating invoice: {str(e)}', 'danger')

    return redirect(url_for('ar_ap.recurring_bills'))

@ar_ap_bp.route('/recurring-bills/delete/<int:bill_id>', methods=['POST'])
@login_required
@role_required('Admin', 'Accountant')
def delete_recurring_bill(bill_id):
    """
    Deletes a recurring bill template.
    """
    bill = RecurringBill.query.get_or_404(bill_id)
    
    try:
        bill_description = bill.description # Get description before deleting
        db.session.delete(bill)
        db.session.commit()
        log_action(f'Deleted recurring bill: {bill_description} (ID: {bill_id}).')
        flash(f'Recurring bill "{bill_description}" has been deleted.', 'success')

    except Exception as e:
        db.session.rollback()
        flash(f'Error deleting bill: {str(e)}', 'danger')

    return redirect(url_for('ar_ap.recurring_bills'))