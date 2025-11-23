from flask import Blueprint, request, flash, redirect, url_for, jsonify
from flask_login import login_required, current_user
from models import (db, Sale, Purchase, ARInvoice, APInvoice, Payment, 
                   JournalEntry, StockAdjustment, Product, InventoryLot, SaleItem, 
                   InventoryTransaction, ARInvoiceItem) # Added Invoice Item Models
from datetime import datetime
import json
from .decorators import role_required
from .utils import log_action, get_system_account_code
from routes.fifo_utils import reverse_inventory_consumption
from sqlalchemy import func
from decimal import Decimal, ROUND_HALF_UP

void_bp = Blueprint('void', __name__, url_prefix='/void')

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

# def create_reversing_je(original_je, description_prefix, void_reason):
#     """
#     Create a reversing JE that swaps debit/credit lines from original_je, and mark original as voided.
#     Uses Decimal rounding and formats values as fixed 2-decimal strings for entries_json.
#     Returns the reversing JE object.
#     """
#     # Ensure we import to_decimal from surrounding module if available, else define locally
#     def _fmt(d):
#         if not isinstance(d, Decimal):
#             d = to_decimal(d) if 'to_decimal' in globals() else Decimal(str(d or '0.00'))
#         return format(d.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP), '0.2f')

#     try:
#         original_entries = []
#         if hasattr(original_je, 'entries'):
#             original_entries = original_je.entries()
#         else:
#             original_entries = json.loads(getattr(original_je, 'entries_json', '[]') or '[]')
#     except Exception:
#         original_entries = []

#     reversed_entries = []
#     for entry in original_entries:
#         # Safely coerce values to Decimal
#         debit = to_decimal(entry.get('debit', '0.00'))
#         credit = to_decimal(entry.get('credit', '0.00'))
#         reversed_entries.append({
#             'account_code': entry.get('account_code'),
#             # swap debit/credit
#             'debit': _fmt(credit),
#             'credit': _fmt(debit)
#         })

#     reversing_je = JournalEntry(
#         description=f'[VOID] {description_prefix} - {void_reason}',
#         entries_json=json.dumps(reversed_entries),
#         created_at=datetime.utcnow()
#     )
#     db.session.add(reversing_je)

#     # Mark original as voided (audit info)
#     original_je.voided_at = datetime.utcnow()
#     original_je.voided_by = current_user.id
#     original_je.void_reason = void_reason

#     # Helpful: try to capture relationship by noting reversing_je id after flush/commit (if model supports)
#     # (Don't commit here; calling function commits once everything is set)
#     return reversing_je

def create_reversing_je(original_je, description_prefix, void_reason):
    """
    Mark original JE as voided.
    
    CORRECTION: We do NOT create a new 'reversing' journal entry here.
    Since the reports filter out entries where voided_at is set, simply 
    marking the original as void is sufficient to remove it from the GL.
    Creating a reversal entry while also voiding the original would 
    double-count the reversal, causing incorrect balances.
    """
    try:
        # 1. Mark original as voided (removes it from GL reports)
        original_je.voided_at = datetime.utcnow()
        original_je.voided_by = current_user.id
        original_je.void_reason = void_reason
        
        # 2. No new entry is created. 
        # The original is now 'invisible' to the accounting engine, which results in 0.00 balance.
        return None

    except Exception as e:
        # Log error or re-raise depending on preference, but usually safe to pass
        print(f"Error marking JE as void: {e}")
        return None

# --- 1. VOID SALE (Covers POS/Cash Sales) ---
@void_bp.route('/sale/<int:sale_id>', methods=['POST'])
@login_required
@role_required('Admin', 'Accountant', 'Cashier')
def void_sale(sale_id):
    """
    Void a POS/Cash Sale transaction.
    If fully paid (which most POS sales are), the reversal must DR Inventory, CR COGS, CR VAT Payable, DR Revenue.
    The original JE already handles the cash/bank movement, so reversing the original JE is the correct action here.
    """
    sale = Sale.query.get_or_404(sale_id)
    
    if sale.voided_at:
        flash('This sale has already been voided.', 'warning')
        return redirect(url_for('core.sales'))
    
    void_reason = request.form.get('void_reason', '').strip()
    if not void_reason:
        flash('Void reason is required.', 'danger')
        return redirect(request.referrer or url_for('core.sales'))
    
    try:
        # 1. Reverse FIFO inventory consumption and restore quantities
        reverse_inventory_consumption(sale_id=sale.id)
        
        # 2. Restore product quantities (Note: Inventory restoration happens inside reverse_inventory_consumption too, 
        # but this check is kept for robustness against sale.items)
        for item in sale.items:
            if item.product_id:
                product = Product.query.get(item.product_id)
                if product:
                    product.quantity += item.qty
        
        # 3. Create reversing journal entry
        original_je = JournalEntry.query.filter(
            JournalEntry.description.like(f'%Sale #{sale.id}%')
        ).filter(JournalEntry.voided_at.is_(None)).first()
        
        if original_je:
            create_reversing_je(original_je, f'Sale #{sale.id} ({sale.document_number})', void_reason)
        
        # 4. Mark sale as voided
        sale.voided_at = datetime.utcnow()
        sale.voided_by = current_user.id
        sale.void_reason = void_reason
        sale.status = 'Voided'
        
        log_action(f'Voided Sale #{sale.id} ({sale.document_number}). Reason: {void_reason}')
        
        db.session.commit()
        flash(f'Sale #{sale.id} has been voided successfully.', 'success')
        
    except Exception as e:
        db.session.rollback()
        flash(f'Error voiding sale: {str(e)}', 'danger')
    
    return redirect(url_for('core.sales'))

# --- 2. VOID PURCHASE (THE COMPLEX CONDITIONAL FIX) ---
@void_bp.route('/purchase/<int:purchase_id>', methods=['POST'])
@login_required
@role_required('Admin', 'Accountant')
def void_purchase(purchase_id):
    """
    Void a Purchase. 
    Updated: Automatically voids any associated payments first, then voids the purchase.
    """
    purchase = Purchase.query.get_or_404(purchase_id)

    if purchase.voided_at:
        flash('This purchase has already been voided.', 'warning')
        return redirect(url_for('core.purchases'))

    void_reason = request.form.get('void_reason', '').strip()
    if not void_reason:
        flash('Void reason is required.', 'danger')
        return redirect(request.referrer or url_for('core.purchases'))

    try:
        # 1. Check inventory lots FIRST — do not allow void if any lot from this purchase has been consumed
        # We check this before touching payments to ensure we don't partially void things if the purchase is locked.
        for item in purchase.items:
            lots = InventoryLot.query.filter_by(purchase_id=purchase.id, purchase_item_id=item.id).all()
            for lot in lots:
                consumed = InventoryTransaction.query.filter(InventoryTransaction.lot_id == lot.id).first()
                if consumed:
                    flash(f'Cannot void purchase: inventory from this purchase has been used/sold (Product: {item.product_name}).', 'danger')
                    return redirect(url_for('core.purchases'))

        # 2. Auto-Void Associated Payments (Fix for "Payments Applied" error)
        active_payments = Payment.query.filter(
            Payment.ref_type == 'Purchase', 
            Payment.ref_id == purchase.id, 
            Payment.voided_at.is_(None)
        ).all()

        for payment in active_payments:
            # A. Find the JE for this payment
            # Note: Since payments often share descriptions, we pick the first non-voided matching one.
            payment_je = JournalEntry.query.filter(
                JournalEntry.description.like(f'%Payment for Purchase #{purchase.id}%')
            ).filter(JournalEntry.voided_at.is_(None)).first()

            # B. Void the JE
            if payment_je:
                create_reversing_je(payment_je, f'Payment #{payment.id}', f"Auto-void linked to Purchase #{purchase.id} void")

            # C. Void the Payment Record
            payment.voided_at = datetime.utcnow()
            payment.voided_by = current_user.id
            payment.void_reason = f"Auto-voided with Purchase #{purchase.id}: {void_reason}"
            
            log_action(f'Auto-voided Payment #{payment.id} for Purchase #{purchase.id}.')

        # 3. Delete lots (safe now) and adjust product quantities
        for item in purchase.items:
            lots = InventoryLot.query.filter_by(purchase_id=purchase.id, purchase_item_id=item.id).all()
            for lot in lots:
                db.session.delete(lot)

            product = Product.query.get(item.product_id)
            if product:
                # Reduce stock quantity
                try:
                    product.quantity = max(0, int(product.quantity) - int(item.qty))
                except Exception:
                    product.quantity = max(0, (product.quantity or 0) - (item.qty or 0))

        # 4. Reverse the original purchase JE(s)
        original_purchase_je = None
        if hasattr(purchase, 'journal_entry_id') and purchase.journal_entry_id:
            original_purchase_je = JournalEntry.query.get(purchase.journal_entry_id)
            if original_purchase_je and original_purchase_je.voided_at is not None:
                original_purchase_je = None

        if not original_purchase_je:
            original_purchase_je = JournalEntry.query.filter(
                JournalEntry.description.ilike(f'%Purchase #{purchase.id}%'),
                JournalEntry.voided_at.is_(None)
            ).order_by(JournalEntry.created_at.asc()).first()

        if original_purchase_je:
            create_reversing_je(original_purchase_je, f'Purchase #{purchase.id} ({purchase.supplier})', void_reason)
        
        # 5. Mark purchase as voided and reset paid amount
        purchase.voided_at = datetime.utcnow()
        purchase.voided_by = current_user.id
        purchase.void_reason = void_reason
        purchase.status = 'Voided'
        purchase.paid = Decimal('0.00')

        log_action(f'Voided Purchase #{purchase.id}. Reason: {void_reason}')
        db.session.commit()

        flash(f'Purchase #{purchase.id} and {len(active_payments)} associated payments have been voided successfully.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Error voiding purchase: {str(e)}', 'danger')

    return redirect(url_for('core.purchases'))

# --- 3. VOID AR INVOICE ---
@void_bp.route('/ar-invoice/<int:invoice_id>', methods=['POST'])
@login_required
@role_required('Admin', 'Accountant')
def void_ar_invoice(invoice_id):
    """
    Void a Billing Invoice (AR). Xero standard: Must have zero payments applied.
    """
    invoice = ARInvoice.query.get_or_404(invoice_id)
    
    if invoice.voided_at:
        flash('This invoice has already been voided.', 'warning')
        return redirect(url_for('ar_ap.billing_invoices'))
    
    # Xero Standard: Cannot void if payments are applied. Payments must be voided first.
    if invoice.paid.quantize(Decimal('0.01')) > Decimal('0.00'):
        flash('Cannot void invoice with payments. Void the associated payments first.', 'danger')
        return redirect(request.referrer or url_for('ar_ap.billing_invoices'))
    
    void_reason = request.form.get('void_reason', '').strip()
    if not void_reason:
        flash('Void reason is required.', 'danger')
        return redirect(request.referrer or url_for('ar_ap.billing_invoices'))
    
    try:
        # 1. Reverse FIFO inventory consumption and restore quantities
        reverse_inventory_consumption(ar_invoice_id=invoice.id)
        
        # 2. Restore product quantities
        for item in invoice.items:
            product = Product.query.get(item.product_id)
            if product:
                product.quantity += item.qty
        
        # 3. Create reversing journal entry (reverses DR A/R, CR Revenue/VAT, DR COGS, CR Inventory)
        # Note: AR Invoices should have AT LEAST two JEs (Sale and COGS). We reverse the Sale JE first.
        original_je = JournalEntry.query.filter(
            JournalEntry.description.like(f'%Billing Invoice {invoice.invoice_number}%')
        ).filter(JournalEntry.voided_at.is_(None)).first()
        
        if original_je:
            create_reversing_je(original_je, f'Billing Invoice {invoice.invoice_number}', void_reason)
            
            # Find and void the COGS entry, typically named 'COGS for AR Invoice...'
            cogs_je = JournalEntry.query.filter(
                JournalEntry.description.like(f'%COGS for AR Invoice {invoice.invoice_number}%')
            ).filter(JournalEntry.voided_at.is_(None)).first()

            if cogs_je:
                create_reversing_je(cogs_je, f'COGS for AR Invoice {invoice.invoice_number}', void_reason)
        
        # 4. Mark invoice as voided
        invoice.voided_at = datetime.utcnow()
        invoice.voided_by = current_user.id
        invoice.void_reason = void_reason
        invoice.status = 'Voided'
        
        log_action(f'Voided AR Invoice {invoice.invoice_number}. Reason: {void_reason}')
        db.session.commit()
        
        flash(f'Invoice {invoice.invoice_number} has been voided successfully.', 'success')
        
    except Exception as e:
        db.session.rollback()
        flash(f'Error voiding invoice: {str(e)}', 'danger')
    
    return redirect(url_for('ar_ap.billing_invoices'))


# --- 4. VOID AP INVOICE ---
@void_bp.route('/ap-invoice/<int:invoice_id>', methods=['POST'])
@login_required
@role_required('Admin', 'Accountant')
def void_ap_invoice(invoice_id):
    """
    Void an AP Invoice. Xero standard: Must have zero payments applied.
    """
    invoice = APInvoice.query.get_or_404(invoice_id)
    
    if invoice.voided_at:
        flash('This invoice has already been voided.', 'warning')
        return redirect(url_for('ar_ap.ap_invoices'))
    
    # Xero Standard: Cannot void if payments are applied. Payments must be voided first.
    if invoice.paid.quantize(Decimal('0.01')) > Decimal('0.00'):
        flash('Cannot void invoice with payments. Void the payments first.', 'danger')
        return redirect(request.referrer or url_for('ar_ap.ap_invoices'))
    
    void_reason = request.form.get('void_reason', '').strip()
    if not void_reason:
        flash('Void reason is required.', 'danger')
        return redirect(request.referrer or url_for('ar_ap.ap_invoices'))
    
    try:
        # Create reversing journal entry (reverses DR Inventory/VAT Input, CR A/P)
        original_je = JournalEntry.query.filter(
            JournalEntry.description.like(f'%AP Invoice #{invoice.id}%')
        ).filter(JournalEntry.voided_at.is_(None)).first()
        
        if original_je:
            create_reversing_je(original_je, f'AP Invoice #{invoice.id} ({invoice.invoice_number})', void_reason)
        
        # Mark invoice as voided
        invoice.voided_at = datetime.utcnow()
        invoice.voided_by = current_user.id
        invoice.void_reason = void_reason
        invoice.status = 'Voided'
        
        log_action(f'Voided AP Invoice #{invoice.id} ({invoice.invoice_number}). Reason: {void_reason}')
        db.session.commit()
        
        flash(f'AP Invoice #{invoice.id} has been voided successfully.', 'success')
        
    except Exception as e:
        db.session.rollback()
        flash(f'Error voiding invoice: {str(e)}', 'danger')
    
    return redirect(url_for('ar_ap.ap_invoices'))


# --- 5. VOID PAYMENT ---
@void_bp.route('/payment/<int:payment_id>', methods=['POST'])
@login_required
@role_required('Admin', 'Accountant')
def void_payment(payment_id):
    """Void an AR or AP Payment (Reverses DR Cash/Bank, CR A/R/A/P, and restores invoice balance)."""
    payment = Payment.query.get_or_404(payment_id)
    
    if payment.voided_at:
        flash('This payment has already been voided.', 'warning')
        return redirect(request.referrer or url_for('core.index'))
    
    void_reason = request.form.get('void_reason', '').strip()
    if not void_reason:
        flash('Void reason is required.', 'danger')
        return redirect(request.referrer or url_for('core.index'))
    
    try:
        # 1. Restore invoice/purchase balance
        payment_amount = payment.amount
        
        if payment.ref_type == 'ARInvoice':
            invoice = ARInvoice.query.get(payment.ref_id)
            if invoice:
                invoice.paid -= (payment.amount + payment.wht_amount)
                invoice.paid = max(Decimal('0.00'), invoice.paid)
                
                # Update status
                if invoice.paid == Decimal('0.00'):
                    invoice.status = 'Open'
                elif invoice.paid < invoice.total:
                    invoice.status = 'Partially Paid'
                    
        elif payment.ref_type == 'APInvoice':
            invoice = APInvoice.query.get(payment.ref_id)
            if invoice:
                invoice.paid -= payment_amount
                invoice.paid = max(Decimal('0.00'), invoice.paid)
                
                if invoice.paid == Decimal('0.00'):
                    invoice.status = 'Open'
                elif invoice.paid < invoice.total:
                    invoice.status = 'Partially Paid'
        
        # Handle Purchase payment if model supports it (like the one we built)
        elif payment.ref_type == 'Purchase':
            purchase = Purchase.query.get(payment.ref_id)
            if purchase:
                purchase.paid -= payment_amount
                purchase.paid = max(Decimal('0.00'), purchase.paid)
                
                if purchase.paid == Decimal('0.00'):
                    purchase.status = 'Open'
                elif purchase.paid < purchase.total:
                    purchase.status = 'Partial'


        # 2. Create reversing journal entry
        original_je = JournalEntry.query.filter(
            JournalEntry.description.like(f'%Payment for {payment.ref_type} #{payment.ref_id}%')
        ).filter(JournalEntry.voided_at.is_(None)).first()
        
        if original_je:
            create_reversing_je(original_je, f'Payment #{payment.id} for {payment.ref_type} #{payment.ref_id}', void_reason)
        
        # 3. Mark payment as voided
        payment.voided_at = datetime.utcnow()
        payment.voided_by = current_user.id
        payment.void_reason = void_reason
        
        log_action(f'Voided Payment #{payment.id} for {payment.ref_type} #{payment.ref_id}. Reason: {void_reason}')
        db.session.commit()
        
        flash(f'Payment #{payment.id} has been voided successfully. Invoice balance restored.', 'success')
        
    except Exception as e:
        db.session.rollback()
        flash(f'Error voiding payment: {str(e)}', 'danger')
    
    return redirect(request.referrer or url_for('core.index'))


# --- 6. VOID STOCK ADJUSTMENT ---
@void_bp.route('/stock-adjustment/<int:adjustment_id>', methods=['POST'])
@login_required
@role_required('Admin', 'Accountant')
def void_stock_adjustment(adjustment_id):
    """Void a Stock Adjustment (Reverses quantity change and accounting entry)."""
    adjustment = StockAdjustment.query.get_or_404(adjustment_id)
    
    if adjustment.voided_at:
        flash('This adjustment has already been voided.', 'warning')
        return redirect(url_for('core.inventory'))
    
    void_reason = request.form.get('void_reason', '').strip()
    if not void_reason:
        flash('Void reason is required.', 'danger')
        return redirect(request.referrer or url_for('core.inventory'))
    
    try:
        # 1. Reverse the quantity change
        product = adjustment.product
        product.quantity -= adjustment.quantity_changed
        product.quantity = max(Decimal('0.00'), product.quantity)
        
        # 2. Remove any lots created by this adjustment
        lots = InventoryLot.query.filter_by(adjustment_id=adjustment.id).all()
        for lot in lots:
            db.session.delete(lot)
        
        # 3. Create reversing journal entry
        original_je = JournalEntry.query.filter(
            JournalEntry.description.like(f'%Stock Adjustment #{adjustment.id}%')
        ).filter(JournalEntry.voided_at.is_(None)).first()
        
        if original_je:
            create_reversing_je(original_je, f'Stock Adjustment #{adjustment.id} for {product.name}', void_reason)
        
        # 4. Mark adjustment as voided
        adjustment.voided_at = datetime.utcnow()
        adjustment.voided_by = current_user.id
        adjustment.void_reason = void_reason
        
        log_action(f'Voided Stock Adjustment #{adjustment.id} for {product.name}. Reason: {void_reason}')
        db.session.commit()
        
        flash(f'Stock Adjustment #{adjustment.id} has been voided successfully.', 'success')
        
    except Exception as e:
        db.session.rollback()
        flash(f'Error voiding adjustment: {str(e)}', 'danger')
    
    return redirect(url_for('core.inventory'))


# --- 7. VOID JOURNAL ENTRY ---
@void_bp.route('/journal-entry/<int:je_id>', methods=['POST'])
@login_required
@role_required('Admin', 'Accountant')
def void_journal_entry(je_id):
    """Void a Manual Journal Entry (Simplest form of reversal)."""
    journal_entry = JournalEntry.query.get_or_404(je_id)
    
    if journal_entry.voided_at:
        flash('This journal entry has already been voided.', 'warning')
        return redirect(url_for('core.journal_entries'))
    
    void_reason = request.form.get('void_reason', '').strip()
    if not void_reason:
        flash('Void reason is required.', 'danger')
        return redirect(request.referrer or url_for('core.journal_entries'))
    
    try:
        # 1. Create reversing entry
        create_reversing_je(journal_entry, f'JE #{journal_entry.id}', void_reason)
        
        # 2. Mark original as voided (done inside helper, but setting the rest of the object here for clarity)
        journal_entry.voided_at = datetime.utcnow()
        journal_entry.voided_by = current_user.id
        journal_entry.void_reason = void_reason
        
        log_action(f'Voided Journal Entry #{journal_entry.id}. Reason: {void_reason}')
        db.session.commit()
        
        flash(f'Journal Entry #{journal_entry.id} has been voided successfully.', 'success')
        
    except Exception as e:
        db.session.rollback()
        flash(f'Error voiding journal entry: {str(e)}', 'danger')
    
    return redirect(url_for('core.journal_entries'))