from flask import Blueprint, request, flash, redirect, url_for, jsonify
from flask_login import login_required, current_user
from models import (db, Sale, Purchase, ARInvoice, APInvoice, Payment, 
                   JournalEntry, StockAdjustment, Product, InventoryLot, SaleItem, 
                   InventoryTransaction, ARInvoiceItem, APInvoiceItem) # Added Invoice Item Models
from datetime import datetime
import json
from .decorators import role_required
from .utils import log_action, get_system_account_code
from routes.fifo_utils import reverse_inventory_consumption
from sqlalchemy import func
from decimal import Decimal, ROUND_HALF_UP

void_bp = Blueprint('void', __name__, url_prefix='/void')

def create_reversing_je(original_je, description_prefix, void_reason):
    """Helper to create a standard reversing JE and mark the original voided."""
    original_entries = original_je.entries()
    reversed_entries = []
    
    for entry in original_entries:
        reversed_entries.append({
            'account_code': entry['account_code'],
            'debit': entry.get('credit', '0.00'),  # Swap debit/credit
            'credit': entry.get('debit', '0.00')
        })
    
    reversing_je = JournalEntry(
        description=f'[VOID] {description_prefix} - {void_reason}',
        entries_json=json.dumps(reversed_entries),
        created_at=datetime.utcnow()
    )
    db.session.add(reversing_je)
    
    # Mark original as voided
    original_je.voided_at = datetime.utcnow()
    original_je.voided_by = current_user.id
    original_je.void_reason = void_reason
    
    return reversing_je

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
    Void a Purchase transaction. Handles accounting reversal based on paid status (Xero method).
    - If fully paid: Reverses the net cash/asset flow (DR Cash, CR Inventory/VAT Input).
    - If open/partial: Reverses the original purchase liability (DR A/P, CR Inventory/VAT Input).
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
        # 1. Inventory Check, Lot Deletion, and Quantity Reversal
        for item in purchase.items:
            lots = InventoryLot.query.filter_by(
                purchase_id=purchase.id,
                purchase_item_id=item.id
            ).all()
            
            for lot in lots:
                consumed = InventoryTransaction.query.filter_by(lot_id=lot.id).first()
                if consumed:
                    flash(f'Cannot void purchase: Inventory from this purchase has been sold (Product: {item.product_name}).', 'danger')
                    return redirect(url_for('core.purchases'))
                db.session.delete(lot)
        
        for item in purchase.items:
            product = Product.query.get(item.product_id)
            if product:
                product.quantity -= item.qty
                product.quantity = max(0, product.quantity)

        # 2. Accounting Reversal Logic (Conditional)
        total_gross = purchase.total.quantize(Decimal('0.01'))
        total_net = (purchase.total - purchase.vat).quantize(Decimal('0.01'))
        total_vat = purchase.vat.quantize(Decimal('0.01'))
        
        # Mark all related JEs (Purchase JE and all Payment JEs) as voided
        purchase_jes = JournalEntry.query.filter(
            (JournalEntry.description.like(f'%Purchase #{purchase.id}%')) | 
            (JournalEntry.description.like(f'%Payment for Purchase #{purchase.id}%'))
        ).filter(JournalEntry.voided_at.is_(None)).all()
        
        for je in purchase_jes:
            je.voided_at = datetime.utcnow()
            je.voided_by = current_user.id
            je.void_reason = void_reason

        # Setup account codes
        inventory_code = get_system_account_code('Inventory')
        vat_input_code = get_system_account_code('VAT Input')
        ap_code = get_system_account_code('Accounts Payable')
        cash_code = get_system_account_code('Cash')
        
        is_fully_paid = purchase.paid.quantize(Decimal('0.01')) >= total_gross

        if is_fully_paid:
            # CASE A: FULLY PAID - Reverse the NET flow (DR Cash, CR Inventory/VAT Input)
            reversed_entries = [
                # DR: Cash (Full Gross Total is returned/reconciled)
                {'account_code': cash_code, 'debit': format(total_gross, '0.2f'), 'credit': "0.00"},
                # CR: Inventory (Net Cost removal)
                {'account_code': inventory_code, 'debit': "0.00", 'credit': format(total_net, '0.2f')},
            ]
            if total_vat > Decimal('0.00'):
                reversed_entries.append({'account_code': vat_input_code, 'debit': "0.00", 'credit': format(total_vat, '0.2f')})
            
            reversing_description = f'[VOID - PAID REVERSAL] Purchase #{purchase.id}'
            
        else:
            # CASE B: OPEN or PARTIAL PAYMENT - Reverse original Purchase JE (DR A/P, CR Inventory/VAT Input)
            # This clears the remaining A/P balance and removes the asset.
            reversed_entries = [
                # DR: Accounts Payable (Liability decrease - total gross amount)
                {'account_code': ap_code, 'debit': format(total_gross, '0.2f'), 'credit': "0.00"},
                # CR: Inventory (Net Cost removal)
                {'account_code': inventory_code, 'debit': "0.00", 'credit': format(total_net, '0.2f')},
            ]
            if total_vat > Decimal('0.00'):
                reversed_entries.append({'account_code': vat_input_code, 'debit': "0.00", 'credit': format(total_vat, '0.2f')})

            reversing_description = f'[VOID - LIABILITY REVERSAL] Purchase #{purchase.id}'

        reversing_je = JournalEntry(
            description=f'{reversing_description} - {void_reason}',
            entries_json=json.dumps(reversed_entries),
            created_at=datetime.utcnow()
        )
        db.session.add(reversing_je)
        
        # 3. Mark purchase as voided
        purchase.voided_at = datetime.utcnow()
        purchase.voided_by = current_user.id
        purchase.void_reason = void_reason
        purchase.status = 'Voided'
        purchase.paid = Decimal('0.00') # Reset paid amount since cash reversal is handled

        log_action(f'Voided Purchase #{purchase.id}. Reason: {void_reason}. Status: {purchase.status}.')
        db.session.commit()
        
        flash(f'Purchase #{purchase.id} has been voided successfully. All accounting entries reconciled.', 'success')
        
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