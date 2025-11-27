from flask import Blueprint, render_template, request, redirect, url_for, flash, send_file
from flask_login import login_required, current_user
from models import db, Customer, Supplier, ARInvoice, APInvoice, Payment, JournalEntry, CreditMemo, Account, Product, ARInvoiceItem, RecurringBill, ConsignmentRemittance, Purchase
import io, csv
import json
from .decorators import role_required
from .utils import log_action, get_system_account_code
from models import Product, ARInvoiceItem, Payment
from datetime import datetime, timedelta
from sqlalchemy import func
from decimal import Decimal, ROUND_HALF_UP, getcontext

getcontext().prec = 28

ar_ap_bp = Blueprint('ar_ap', __name__, url_prefix='')


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
    try:
        d = Decimal(value)
    except Exception:
        try:
            d = Decimal(str(value))
        except Exception:
            return Decimal('0.00')
    return d.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


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
            try:
                cust_id = int(request.form.get('customer_id') or 0) or None
            except ValueError:
                cust_id = None
            total = to_decimal(request.form.get('total') or '0')
            vat = to_decimal(request.form.get('vat') or '0')
            if total <= Decimal('0.00'):
                flash('Invoice total must be > 0')
                return redirect(url_for('ar_ap.ar_invoices'))

            inv = ARInvoice(customer_id=cust_id, total=total, vat=vat)
            db.session.add(inv)
            db.session.flush()

            # Journal entry (store amounts as strings for exact audit)
            je_lines = [
                {'account_code': get_system_account_code('Accounts Receivable'), 'debit': format(total, '0.2f'), 'credit': "0.00"},
                {'account_code': get_system_account_code('Sales Revenue'), 'debit': "0.00", 'credit': format((total - vat).quantize(Decimal('0.01')), '0.2f')},
            ]
            if vat > Decimal('0.00'):
                je_lines.append({'account_code': get_system_account_code('VAT Payable'), 'debit': "0.00", 'credit': format(vat, '0.2f')})

            je = JournalEntry(description=f'AR Invoice #{inv.id}', entries_json=json.dumps(je_lines))
            db.session.add(je)
            log_action(f'Created AR Invoice #{inv.id} for ₱{total:,.2f}.')
            db.session.commit()
            flash('AR Invoice created and journal entry recorded.')

        except Exception as e:
            db.session.rollback()
            flash(f'An error occurred: {str(e)}', 'danger')

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
            try:
                sup_id = int(request.form.get('supplier_id') or 0) or None
            except ValueError:
                sup_id = None

            total = to_decimal(request.form.get('total') or '0')
            vat = to_decimal(request.form.get('vat') or '0')

            # --- NEW FIELDS ---
            invoice_number = request.form.get('invoice_number')
            description = request.form.get('description')
            is_vatable = request.form.get('is_vatable') == 'true'

            due_date_str = request.form.get('due_date')
            due_date = None
            if due_date_str:
                try:
                    due_date = datetime.strptime(due_date_str, '%Y-%m-%d')
                except ValueError:
                    flash('Invalid due date format. Please use YYYY-MM-DD.', 'danger')
                    return redirect(url_for('ar_ap.ap_invoices'))

            default_inv_code = get_system_account_code('Inventory')
            expense_account_code = request.form.get('expense_account_code') or default_inv_code

            if not is_vatable:
                vat = Decimal('0.00')

            if total <= Decimal('0.00'):
                flash('Invoice total must be > 0')
                return redirect(url_for('ar_ap.ap_invoices'))

            if not sup_id:
                flash('Please select a supplier.')
                return redirect(url_for('ar_ap.ap_invoices'))

            if not expense_account_code:
                flash('Please select a debit account.')
                return redirect(url_for('ar_ap.ap_invoices'))

            inv = APInvoice(
                supplier_id=sup_id,
                total=total,
                vat=vat,
                invoice_number=invoice_number,
                description=description,
                due_date=due_date,
                is_vatable=is_vatable,
                expense_account_code=expense_account_code
            )
            db.session.add(inv)
            db.session.flush()

            # Journal entry using formatted strings
            je_lines = [
                {'account_code': expense_account_code, 'debit': format((inv.total - inv.vat).quantize(Decimal('0.01')), '0.2f'), 'credit': "0.00"},
                {'account_code': get_system_account_code('VAT Input'), 'debit': format(inv.vat.quantize(Decimal('0.01')), '0.2f'), 'credit': "0.00"},
                {'account_code': get_system_account_code('Accounts Payable'), 'debit': "0.00", 'credit': format(inv.total.quantize(Decimal('0.01')), '0.2f')},
            ]

            # Remove VAT Input line if VAT is zero
            if inv.vat == Decimal('0.00'):
                # remove the second line
                je_lines.pop(1)

            je = JournalEntry(description=f'AP Invoice #{inv.id} ({inv.invoice_number}) - {inv.description or ""}', entries_json=json.dumps(je_lines))
            db.session.add(je)
            log_action(f'Created AP Invoice #{inv.id} for ₱{inv.total:,.2f}.')
            db.session.commit()
            flash('AP Invoice created and journal entry recorded.')

        except Exception as e:
            db.session.rollback()
            flash(f'An error occurred: {str(e)}', 'danger')

        return redirect(url_for('ar_ap.ap_invoices'))

    invoices = APInvoice.query.order_by(APInvoice.date.desc()).all()
    suppliers = Supplier.query.order_by(Supplier.name).all()

    accounts = Account.query.filter(
        (Account.type == 'Expense') | (Account.code == get_system_account_code('Inventory'))
    ).order_by(Account.name).all()

    return render_template(
        'ap_invoices.html',
        invoices=invoices,
        suppliers=suppliers,
        accounts=accounts
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
        amount = to_decimal(request.form.get('amount') or '0')
        wht_amount = to_decimal(request.form.get('wht_amount') or '0')
    except Exception:
        flash('Invalid amount or WHT value.', 'danger')
        return redirect(request.referrer or url_for('ar_ap.ar_invoices'))

    method = request.form.get('method') or 'Cash'
    total_credited = (amount + wht_amount).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

    if total_credited <= Decimal('0.00'):
        flash('Total credited amount (Amount + WHT) must be > 0.', 'warning')
        return redirect(request.referrer or url_for('ar_ap.ar_invoices'))

    if ref_type == 'AR':
        inv = ARInvoice.query.get(ref_id)
        if not inv:
            flash(f'AR Invoice {ref_id} not found.', 'danger')
            return redirect(url_for('ar_ap.ar_invoices'))

        inv.paid = to_decimal(inv.paid) + total_credited

        if to_decimal(inv.paid) >= (to_decimal(inv.total) - Decimal('0.001')):
            inv.status = 'Paid'
        else:
            inv.status = 'Partially Paid'

        je_lines = [
            {'account_code': get_system_account_code('Cash'), 'debit': format(amount, '0.2f'), 'credit': "0.00"},
            {'account_code': get_system_account_code('Creditable Withholding Tax'), 'debit': format(wht_amount, '0.2f'), 'credit': "0.00"},
            {'account_code': get_system_account_code('Accounts Receivable'), 'debit': "0.00", 'credit': format(total_credited, '0.2f')}
        ]

        redirect_url = url_for('ar_ap.ar_invoices')

    elif ref_type == 'AP':
        inv = APInvoice.query.get(ref_id)
        if not inv:
            flash(f'AP Invoice {ref_id} not found.', 'danger')
            return redirect(url_for('ar_ap.ap_invoices'))

        inv.paid = to_decimal(inv.paid) + amount
        inv.status = 'Paid' if to_decimal(inv.paid) >= to_decimal(inv.total) else 'Partially Paid'

        je_lines = [
            {'account_code': get_system_account_code('Accounts Payable'), 'debit': format(amount, '0.2f'), 'credit': "0.00"},
            {'account_code': get_system_account_code('Cash'), 'debit': "0.00", 'credit': format(amount, '0.2f')}
        ]

        redirect_url = url_for('ar_ap.ap_invoices')

    else:
        flash('Unknown reference type.', 'danger')
        return redirect(url_for('core.index'))

    try:
        p = Payment(
            amount=amount,
            ref_type=ref_type,
            ref_id=ref_id,
            method=method,
            wht_amount=wht_amount,
            date=datetime.utcnow()
        )
        db.session.add(p)
        db.session.flush()

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


@ar_ap_bp.route('/credit-memos', methods=['GET', 'POST'])
@login_required
@role_required('Admin', 'Accountant')
def credit_memos():
    # --- FIX: Import new models and utils ---
    from routes.fifo_utils import create_inventory_lot

    if request.method == 'POST':
        customer_id = int(request.form.get('customer_id') or 0) or None
        ar_invoice_id = int(request.form.get('ar_invoice_id') or 0) or None
        reason = request.form.get('reason')
        total_amount = to_decimal(request.form.get('total_amount') or '0')

        return_product_id = int(request.form.get('return_product_id') or 0) or None
        return_quantity = int(request.form.get('return_quantity') or 0) or None

        if not customer_id or total_amount <= Decimal('0.00'):
            flash('Customer and a valid amount are required.', 'danger')
            return redirect(url_for('ar_ap.credit_memos'))

        # Calculate net and VAT (assuming 12% VAT)
        amount_net = (total_amount / Decimal('1.12')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        vat = (total_amount - amount_net).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

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
                inv.paid = to_decimal(inv.paid) + total_amount
                remaining_balance = to_decimal(inv.total) - to_decimal(inv.paid)
                if remaining_balance <= Decimal('0.01'):
                    inv.status = 'Paid'
                elif remaining_balance < to_decimal(inv.total):
                    inv.status = 'Partially Paid'

        # Journal Entry
        je_lines = [
            {'account_code': get_system_account_code('Sales Returns'), 'debit': format(amount_net, '0.2f'), 'credit': "0.00"},
            {'account_code': get_system_account_code('VAT Payable'), 'debit': format(vat, '0.2f'), 'credit': "0.00"},
            {'account_code': get_system_account_code('Accounts Receivable'), 'debit': "0.00", 'credit': format(total_amount, '0.2f')}
        ]

        # Handle inventory return
        if return_product_id and return_quantity and return_quantity > 0:
            product = Product.query.get(return_product_id)
            if product:
                product.quantity += int(return_quantity)

                # determine original cost
                return_cost = to_decimal(product.cost_price)
                if ar_invoice_id:
                    original_item = ARInvoiceItem.query.filter_by(
                        ar_invoice_id=ar_invoice_id,
                        product_id=return_product_id
                    ).first()
                    if original_item and to_decimal(original_item.cogs) > Decimal('0.00') and original_item.qty > 0:
                        return_cost = (to_decimal(original_item.cogs) / Decimal(original_item.qty)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

                create_inventory_lot(
                    product_id=product.id,
                    quantity=return_quantity,
                    unit_cost=return_cost,
                    is_opening_balance=False
                )

                total_cogs_reversal = (return_cost * Decimal(return_quantity)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                if total_cogs_reversal > Decimal('0.00'):
                    je_lines.append({'account_code': get_system_account_code('Inventory'), 'debit': format(total_cogs_reversal, '0.2f'), 'credit': "0.00"})
                    je_lines.append({'account_code': get_system_account_code('COGS'), 'debit': "0.00", 'credit': format(total_cogs_reversal, '0.2f')})

                log_action(f'Returned {return_quantity} of {product.name} to inventory via CM #{cm.id}.')

        je = JournalEntry(description=f'Credit Memo #{cm.id} for {reason}', entries_json=json.dumps(je_lines))
        db.session.add(je)
        log_action(f'Created Credit Memo #{cm.id} for ₱{cm.total_amount:,.2f} (Reason: {reason}).')
        db.session.commit()
        flash('Credit Memo created successfully.', 'success')
        return redirect(url_for('ar_ap.credit_memos'))

    memos = CreditMemo.query.order_by(CreditMemo.date.desc()).all()
    customers = Customer.query.order_by(Customer.name).all()
    invoices = ARInvoice.query.filter(ARInvoice.status != 'Paid').order_by(ARInvoice.id.desc()).all()

    products = Product.query.filter_by(is_active=True).order_by(Product.name).all()

    return render_template('credit_memos.html',
                           memos=memos,
                           customers=customers,
                           invoices=invoices,
                           products=products)


@ar_ap_bp.route('/billing-invoices', methods=['GET', 'POST'])
@login_required
@role_required('Admin', 'Accountant', 'Cashier')
def billing_invoices():
    from routes.fifo_utils import consume_inventory_fifo

    if request.method == 'POST':
        try:
            # ✅ CHANGED: Allow customer name OR customer_id
            customer_name = request.form.get('customer_name', '').strip()
            customer_id_str = request.form.get('customer_id', '').strip()
            
            # Determine if using existing customer or creating new
            customer_id = None
            customer = None
            
            if customer_id_str and customer_id_str.isdigit():
                # Existing customer selected from dropdown
                customer_id = int(customer_id_str)
                customer = Customer.query.get(customer_id)
                if not customer:
                    flash('Selected customer not found', 'danger')
                    return redirect(url_for('ar_ap.billing_invoices'))
            elif customer_name:
                # New customer name typed in
                customer = Customer.query. filter_by(name=customer_name).first()
                if not customer:
                    # Create new customer
                    customer = Customer(name=customer_name)
                    db.session.add(customer)
                    db. session.flush()
                    flash(f'ℹ️ Created new customer: {customer_name}', 'info')
                customer_id = customer.id
            else:
                flash('Please select or enter a customer name', 'danger')
                return redirect(url_for('ar_ap.billing_invoices'))
            
            description = request.form.get('description', '')
            is_vatable = request.form.get('is_vatable') == 'true'

            due_date_str = request.form.get('due_date')
            if due_date_str:
                due_date = datetime.strptime(due_date_str, '%Y-%m-%d')
            else:
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
            subtotal = Decimal('0.00')
            total_vat = Decimal('0.00')

            for i in range(len(product_ids)):
                product_id = int(product_ids[i])
                qty = int(quantities[i])
                unit_price = to_decimal(unit_prices[i])
                line_is_vatable = line_vatables[i] == 'true'

                product = Product.query.get(product_id)
                if not product:
                    flash(f'Product ID {product_id} not found', 'danger')
                    return redirect(url_for('ar_ap.billing_invoices'))

                if product.quantity < qty:
                    flash(f'Insufficient stock for {product.name}. Available: {product.quantity}, Requested: {qty}', 'danger')
                    return redirect(url_for('ar_ap.billing_invoices'))

                line_total = (Decimal(qty) * unit_price).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                line_vat = Decimal('0.00')

                if line_is_vatable:
                    net_amount = (line_total / Decimal('1.12')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                    line_vat = (line_total - net_amount).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                    total_vat += line_vat

                if line_total <= Decimal('0.00') or line_vat < Decimal('0.00'):
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
                    'cogs': Decimal('0.00')  # Will be set after consumption
                })

                subtotal += line_total

            invoice_total = subtotal

            company = None
            try:
                from models import CompanyProfile
                company = CompanyProfile.query.first()
            except Exception:
                company = None

            if company and hasattr(company, 'next_invoice_number'):
                invoice_number = f"INV-{int(company.next_invoice_number):05d}"
                company.next_invoice_number = int(company.next_invoice_number) + 1
            else:
                invoice_number = f"INV-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"

            ar_invoice = ARInvoice(
                customer_id=customer_id,
                total=invoice_total,
                vat=total_vat,
                paid=Decimal('0.00'),
                is_vatable=(is_vatable or (total_vat > Decimal('0.00'))),
                status='Open',
                invoice_number=invoice_number,
                description=description,
                due_date=due_date
            )
            db.session.add(ar_invoice)
            db.session.flush()

            # create and keep created items
            created_ar_items = []
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
                created_ar_items.append(ar_item)
            db.session.flush()

            # consume FIFO using the created objects directly
            total_cogs = Decimal('0.00')
            for ar_item, item in zip(created_ar_items, line_items):
                try:
                    line_cogs, _ = consume_inventory_fifo(
                        product_id=item['product_id'],
                        quantity_needed=item['qty'],
                        ar_invoice_id=ar_invoice.id,
                        ar_invoice_item_id=ar_item.id
                    )
                    line_cogs = to_decimal(line_cogs)
                    item['cogs'] = line_cogs
                    ar_item.cogs = line_cogs
                    total_cogs += line_cogs
                    product = Product.query.get(item['product_id'])
                    if product:
                        product.quantity = int(product.quantity) - int(item['qty'])
                except ValueError as e:
                    db.session.rollback()
                    flash(f'FIFO error for {item["product_name"]}: {str(e)}', 'danger')
                    return redirect(url_for('ar_ap.billing_invoices'))

            je_lines = [
                {'account_code': get_system_account_code('Accounts Receivable'), 'debit': format(invoice_total, '0.2f'), 'credit': "0.00"},
                {'account_code': get_system_account_code('Sales Revenue'), 'debit': "0.00", 'credit': format((invoice_total - total_vat).quantize(Decimal('0.01')), '0.2f')},
            ]

            if total_vat > Decimal('0.00'):
                je_lines.append({'account_code': get_system_account_code('VAT Payable'), 'debit': "0.00", 'credit': format(total_vat, '0.2f')})

            je_lines.extend([
                {'account_code': get_system_account_code('COGS'), 'debit': format(total_cogs, '0.2f'), 'credit': "0.00"},
                {'account_code': get_system_account_code('Inventory'), 'debit': "0.00", 'credit': format(total_cogs, '0.2f')}
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
            'sale_price': float(to_decimal(p.sale_price)),
            'cost_price': float(to_decimal(p.cost_price)),
            'quantity': p.quantity
        })

    return render_template('billing_invoices.html',
                         invoices=invoices,
                         customers=customers,
                         products=products_list,
                         Payment=Payment)


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
            'total': f"{to_decimal(inv.total):.2f}",
            'vat': f"{to_decimal(inv.vat):.2f}",
            'paid': f"{to_decimal(inv.paid):.2f}",
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
            'total': f"{to_decimal(inv.total):.2f}",
            'vat': f"{to_decimal(inv.vat):.2f}",
            'paid': f"{to_decimal(inv.paid):.2f}",
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
            total = to_decimal(request.form.get('total') or '0')
            vat = to_decimal(request.form.get('vat') or '0')
            is_vatable = request.form.get('is_vatable') == 'true'
            frequency = request.form.get('frequency')  # e.g., 'monthly'
            next_due_date_str = request.form.get('next_due_date')

            if not supplier_id or not expense_account_code or total <= Decimal('0.00') or not frequency or not next_due_date_str:
                flash('Please fill out all required fields.', 'danger')
                return redirect(url_for('ar_ap.recurring_bills'))

            next_due_date = datetime.strptime(next_due_date_str, '%Y-%m-%d')

            if not is_vatable:
                vat = Decimal('0.00')

            bill = RecurringBill(
                supplier_id=supplier_id,
                expense_account_code=expense_account_code,
                description=description,
                total=total,
                vat=vat,
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
        inv = APInvoice(
            supplier_id=bill.supplier_id,
            total=bill.total,
            vat=bill.vat,
            description=f"(Recurring) {bill.description}",
            due_date=bill.next_due_date,
            is_vatable=bill.is_vatable,
            expense_account_code=bill.expense_account_code,
            status='Open'
        )
        db.session.add(inv)
        db.session.flush()

        je_lines = [
            {'account_code': bill.expense_account_code, 'debit': format((inv.total - inv.vat).quantize(Decimal('0.01')), '0.2f'), 'credit': "0.00"},
            {'account_code': get_system_account_code('VAT Input'), 'debit': format(inv.vat.quantize(Decimal('0.01')), '0.2f'), 'credit': "0.00"},
            {'account_code': get_system_account_code('Accounts Payable'), 'debit': "0.00", 'credit': format(inv.total.quantize(Decimal('0.01')), '0.2f')},
        ]
        if inv.vat == Decimal('0.00'):
            je_lines.pop(1)

        je = JournalEntry(description=f'Recurring AP Invoice #{inv.id} - {inv.description}', entries_json=json.dumps(je_lines))
        db.session.add(je)

        today = datetime.utcnow()
        if bill.frequency == 'monthly':
            next_due = bill.next_due_date + timedelta(days=30)
            while next_due <= today:
                next_due += timedelta(days=30)
            bill.next_due_date = next_due

        elif bill.frequency == 'quarterly':
            next_due = bill.next_due_date + timedelta(days=90)
            while next_due <= today:
                next_due += timedelta(days=90)
            bill.next_due_date = next_due

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
        bill_description = bill.description
        db.session.delete(bill)
        db.session.commit()
        log_action(f'Deleted recurring bill: {bill_description} (ID: {bill_id}).')
        flash(f'Recurring bill "{bill_description}" has been deleted.', 'success')

    except Exception as e:
        db.session.rollback()
        flash(f'Error deleting bill: {str(e)}', 'danger')

    return redirect(url_for('ar_ap.recurring_bills'))

# In routes/ar_ap.py, before the recurring bill routes:

@ar_ap_bp.route('/purchase/pay/<int:purchase_id>', methods=['POST'])
@login_required
@role_required('Admin', 'Accountant', 'Cashier')
def record_purchase_payment(purchase_id):
    """
    Records a payment made against a Purchase (clears the Accounts Payable liability).
    """
    purchase = Purchase.query.get_or_404(purchase_id)
    
    # Ensure purchase is not already paid or cancelled
    if purchase.status in ['Paid', 'Canceled', 'Voided']:
        flash(f'Purchase #{purchase_id} is already marked as {purchase.status}. No payment recorded.', 'warning')
        return redirect(request.referrer or url_for('core.view_purchase', purchase_id=purchase_id))

    try:
        payment_amount = to_decimal(request.form.get('payment_amount'))
        payment_method = request.form.get('payment_method') or 'Cash'
        reference = request.form.get('reference') or ''
        
        balance_due = purchase.total - purchase.paid

        if payment_amount <= Decimal('0.00'):
            flash('Payment amount must be greater than zero.', 'danger')
            return redirect(request.referrer)

        if payment_amount > balance_due:
            flash(f'Payment amount (₱{payment_amount:.2f}) exceeds balance due (₱{balance_due:.2f}). Please pay ₱{balance_due:.2f} or less.', 'danger')
            return redirect(request.referrer)

        # 1. Update Purchase Record
        purchase.paid += payment_amount
        
        if purchase.paid.quantize(Decimal('0.01')) >= purchase.total.quantize(Decimal('0.01')):
            purchase.status = 'Paid'
        elif purchase.paid > Decimal('0.00'):
            purchase.status = 'Partial'

        # 2. Create Payment Record (for detailed tracking, optional but good practice)
        payment = Payment(
            date=datetime.utcnow(),
            amount=payment_amount,
            ref_type='Purchase',
            ref_id=purchase.id,
            method=payment_method,
            wht_amount=Decimal('0.00') # Simplified: No WHT handling here
        )
        db.session.add(payment)
        
        # 3. Create Journal Entry (DR Accounts Payable / CR Cash)
        ap_code = get_system_account_code('Accounts Payable')
        cash_code = get_system_account_code('Cash') # Assuming cash/bank payments use this GL account

        je_lines = [
            # DR: Liability decreases
            {"account_code": ap_code, "debit": format(payment_amount, '0.2f'), "credit": "0.00"}, 
            # CR: Cash/Bank Asset decreases
            {"account_code": cash_code, "debit": "0.00", "credit": format(payment_amount, '0.2f')} 
        ]

        journal = JournalEntry(
            description=f"Payment for Purchase #{purchase.id} - {purchase.supplier} ({payment_method})",
            entries_json=json.dumps(je_lines)
        )
        db.session.add(journal)

        log_action(f'Recorded payment of ₱{payment_amount:,.2f} for Purchase #{purchase.id}. Status: {purchase.status}.')
        db.session.commit()
        
        flash(f"✅ Payment of ₱{payment_amount:,.2f} recorded successfully. Purchase status: {purchase.status}.", "success")
        return redirect(url_for('core.view_purchase', purchase_id=purchase.id))

    except Exception as e:
        db.session.rollback()
        flash(f"❌ Error recording payment: {str(e)}", "danger")
        return redirect(request.referrer)