from flask import Blueprint, request, flash, redirect, url_for, jsonify
from flask_login import login_required, current_user
from models import (db, Sale, Purchase, ARInvoice, APInvoice, Payment, 
                   JournalEntry, StockAdjustment, Product, InventoryLot, SaleItem, InventoryTransaction)
from datetime import datetime
import json
from .decorators import role_required
from .utils import log_action, get_system_account_code
from routes.fifo_utils import reverse_inventory_consumption
from sqlalchemy import func

void_bp = Blueprint('void', __name__, url_prefix='/void')


@void_bp.route('/sale/<int:sale_id>', methods=['POST'])
@login_required
@role_required('Admin', 'Accountant')
def void_sale(sale_id):
    """Void a POS/Cash Sale transaction"""
    sale = Sale.query.get_or_404(sale_id)
    
    # Check if already voided
    if sale.voided_at:
        flash('This sale has already been voided.', 'warning')
        return redirect(url_for('core.sales'))
    
    void_reason = request.form.get('void_reason', '').strip()
    if not void_reason:
        flash('Void reason is required.', 'danger')
        return redirect(request.referrer or url_for('core.sales'))
    
    try:
        # 1. Reverse FIFO inventory consumption
        reversed_qty = reverse_inventory_consumption(sale_id=sale.id)
        
        # 2. Restore product quantities
        for item in sale.items:
            if item.product_id:
                product = Product.query.get(item.product_id)
                if product:
                    product.quantity += item.qty
        
        # 3. Create reversing journal entry
        # Find original journal entry
        original_je = JournalEntry.query.filter(
            JournalEntry.description.like(f'%Sale #{sale.id}%')
        ).filter(JournalEntry.voided_at.is_(None)).first()
        
        if original_je:
            # Create exact reverse
            original_entries = original_je.entries()
            reversed_entries = []
            
            for entry in original_entries:
                reversed_entries.append({
                    'account_code': entry['account_code'],
                    'debit': entry.get('credit', 0),  # Swap debit/credit
                    'credit': entry.get('debit', 0)
                })
            
            reversing_je = JournalEntry(
                description=f'[VOID] Sale #{sale.id} ({sale.document_number}) - {void_reason}',
                entries_json=json.dumps(reversed_entries),
                created_at=datetime.utcnow()
            )
            db.session.add(reversing_je)
            
            # Mark original as voided
            original_je.voided_at = datetime.utcnow()
            original_je.voided_by = current_user.id
            original_je.void_reason = void_reason
        
        # 4. Mark sale as voided
        sale.voided_at = datetime.utcnow()
        sale.voided_by = current_user.id
        sale.void_reason = void_reason
        sale.status = 'voided'
        
        # 5. Log action
        log_action(f'Voided Sale #{sale.id} ({sale.document_number}). Reason: {void_reason}')
        
        db.session.commit()
        flash(f'Sale #{sale.id} has been voided successfully.', 'success')
        
    except Exception as e:
        db.session.rollback()
        flash(f'Error voiding sale: {str(e)}', 'danger')
    
    return redirect(url_for('core.sales'))


@void_bp.route('/purchase/<int:purchase_id>', methods=['POST'])
@login_required
@role_required('Admin', 'Accountant')
def void_purchase(purchase_id):
    """Void a Purchase transaction"""
    purchase = Purchase.query.get_or_404(purchase_id)
    
    if purchase.voided_at:
        flash('This purchase has already been voided.', 'warning')
        return redirect(url_for('core.purchases'))
    
    void_reason = request.form.get('void_reason', '').strip()
    if not void_reason:
        flash('Void reason is required.', 'danger')
        return redirect(request.referrer or url_for('core.purchases'))
    
    try:
        # 1. Check if any inventory from this purchase has been sold
        for item in purchase.items:
            lots = InventoryLot.query.filter_by(
                purchase_id=purchase.id,
                purchase_item_id=item.id
            ).all()
            
            for lot in lots:
                # Check if this lot has been used in any sales
                consumed = InventoryTransaction.query.filter_by(lot_id=lot.id).first()
                if consumed:
                    flash(f'Cannot void purchase: Inventory from this purchase has been sold (Product: {item.product_name}).', 'danger')
                    return redirect(url_for('core.purchases'))
                
                # Safe to delete the lot
                db.session.delete(lot)
        
        # 2. Restore product quantities
        for item in purchase.items:
            product = Product.query.get(item.product_id)
            if product:
                product.quantity -= item.qty
                product.quantity = max(0, product.quantity)
        
        # 3. Create reversing journal entry
        original_je = JournalEntry.query.filter(
            JournalEntry.description.like(f'%Purchase #{purchase.id}%')
        ).filter(JournalEntry.voided_at.is_(None)).first()
        
        if original_je:
            original_entries = original_je.entries()
            reversed_entries = []
            
            for entry in original_entries:
                reversed_entries.append({
                    'account_code': entry['account_code'],
                    'debit': entry.get('credit', 0),
                    'credit': entry.get('debit', 0)
                })
            
            reversing_je = JournalEntry(
                description=f'[VOID] Purchase #{purchase.id} - {void_reason}',
                entries_json=json.dumps(reversed_entries),
                created_at=datetime.utcnow()
            )
            db.session.add(reversing_je)
            
            original_je.voided_at = datetime.utcnow()
            original_je.voided_by = current_user.id
            original_je.void_reason = void_reason
        
        # 4. Mark purchase as voided
        purchase.voided_at = datetime.utcnow()
        purchase.voided_by = current_user.id
        purchase.void_reason = void_reason
        purchase.status = 'Voided'
        
        log_action(f'Voided Purchase #{purchase.id}. Reason: {void_reason}')
        db.session.commit()
        
        flash(f'Purchase #{purchase.id} has been voided successfully.', 'success')
        
    except Exception as e:
        db.session.rollback()
        flash(f'Error voiding purchase: {str(e)}', 'danger')
    
    return redirect(url_for('core.purchases'))


@void_bp.route('/ar-invoice/<int:invoice_id>', methods=['POST'])
@login_required
@role_required('Admin', 'Accountant')
def void_ar_invoice(invoice_id):
    """Void a Billing Invoice (AR)"""
    invoice = ARInvoice.query.get_or_404(invoice_id)
    
    if invoice.voided_at:
        flash('This invoice has already been voided.', 'warning')
        return redirect(url_for('ar_ap.billing_invoices'))
    
    # Check for payments
    if invoice.paid > 0:
        flash('Cannot void invoice with payments. Void the payments first.', 'danger')
        return redirect(url_for('ar_ap.billing_invoices'))
    
    void_reason = request.form.get('void_reason', '').strip()
    if not void_reason:
        flash('Void reason is required.', 'danger')
        return redirect(request.referrer or url_for('ar_ap.billing_invoices'))
    
    try:
        # 1. Reverse FIFO inventory consumption
        reversed_qty = reverse_inventory_consumption(ar_invoice_id=invoice.id)
        
        # 2. Restore product quantities
        for item in invoice.items:
            product = Product.query.get(item.product_id)
            if product:
                product.quantity += item.qty
        
        # 3. Create reversing journal entry
        original_je = JournalEntry.query.filter(
            JournalEntry.description.like(f'%Billing Invoice {invoice.invoice_number}%')
        ).filter(JournalEntry.voided_at.is_(None)).first()
        
        if original_je:
            original_entries = original_je.entries()
            reversed_entries = []
            
            for entry in original_entries:
                reversed_entries.append({
                    'account_code': entry['account_code'],
                    'debit': entry.get('credit', 0),
                    'credit': entry.get('debit', 0)
                })
            
            reversing_je = JournalEntry(
                description=f'[VOID] Billing Invoice {invoice.invoice_number} - {void_reason}',
                entries_json=json.dumps(reversed_entries),
                created_at=datetime.utcnow()
            )
            db.session.add(reversing_je)
            
            original_je.voided_at = datetime.utcnow()
            original_je.voided_by = current_user.id
            original_je.void_reason = void_reason
        
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


@void_bp.route('/ap-invoice/<int:invoice_id>', methods=['POST'])
@login_required
@role_required('Admin', 'Accountant')
def void_ap_invoice(invoice_id):
    """Void an AP Invoice"""
    invoice = APInvoice.query.get_or_404(invoice_id)
    
    if invoice.voided_at:
        flash('This invoice has already been voided.', 'warning')
        return redirect(url_for('ar_ap.ap_invoices'))
    
    if invoice.paid > 0:
        flash('Cannot void invoice with payments. Void the payments first.', 'danger')
        return redirect(url_for('ar_ap.ap_invoices'))
    
    void_reason = request.form.get('void_reason', '').strip()
    if not void_reason:
        flash('Void reason is required.', 'danger')
        return redirect(request.referrer or url_for('ar_ap.ap_invoices'))
    
    try:
        # Create reversing journal entry
        original_je = JournalEntry.query.filter(
            JournalEntry.description.like(f'%AP Invoice #{invoice.id}%')
        ).filter(JournalEntry.voided_at.is_(None)).first()
        
        if original_je:
            original_entries = original_je.entries()
            reversed_entries = []
            
            for entry in original_entries:
                reversed_entries.append({
                    'account_code': entry['account_code'],
                    'debit': entry.get('credit', 0),
                    'credit': entry.get('debit', 0)
                })
            
            reversing_je = JournalEntry(
                description=f'[VOID] AP Invoice #{invoice.id} ({invoice.invoice_number}) - {void_reason}',
                entries_json=json.dumps(reversed_entries),
                created_at=datetime.utcnow()
            )
            db.session.add(reversing_je)
            
            original_je.voided_at = datetime.utcnow()
            original_je.voided_by = current_user.id
            original_je.void_reason = void_reason
        
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


@void_bp.route('/payment/<int:payment_id>', methods=['POST'])
@login_required
@role_required('Admin', 'Accountant')
def void_payment(payment_id):
    """Void an AR or AP Payment"""
    payment = Payment.query.get_or_404(payment_id)
    
    if payment.voided_at:
        flash('This payment has already been voided.', 'warning')
        return redirect(request.referrer or url_for('core.index'))
    
    void_reason = request.form.get('void_reason', '').strip()
    if not void_reason:
        flash('Void reason is required.', 'danger')
        return redirect(request.referrer or url_for('core.index'))
    
    try:
        # Restore invoice balance
        if payment.ref_type == 'AR':
            invoice = ARInvoice.query.get(payment.ref_id)
            if invoice:
                invoice.paid -= (payment.amount + payment.wht_amount)
                invoice.paid = max(0, invoice.paid)
                
                # Update status
                if invoice.paid == 0:
                    invoice.status = 'Open'
                elif invoice.paid < invoice.total:
                    invoice.status = 'Partially Paid'
                    
        elif payment.ref_type == 'AP':
            invoice = APInvoice.query.get(payment.ref_id)
            if invoice:
                invoice.paid -= payment.amount
                invoice.paid = max(0, invoice.paid)
                
                if invoice.paid == 0:
                    invoice.status = 'Open'
                elif invoice.paid < invoice.total:
                    invoice.status = 'Partially Paid'
        
        # Create reversing journal entry
        original_je = JournalEntry.query.filter(
            JournalEntry.description.like(f'%Payment for {payment.ref_type} #{payment.ref_id}%')
        ).filter(JournalEntry.voided_at.is_(None)).first()
        
        if original_je:
            original_entries = original_je.entries()
            reversed_entries = []
            
            for entry in original_entries:
                reversed_entries.append({
                    'account_code': entry['account_code'],
                    'debit': entry.get('credit', 0),
                    'credit': entry.get('debit', 0)
                })
            
            reversing_je = JournalEntry(
                description=f'[VOID] Payment #{payment.id} for {payment.ref_type} #{payment.ref_id} - {void_reason}',
                entries_json=json.dumps(reversed_entries),
                created_at=datetime.utcnow()
            )
            db.session.add(reversing_je)
            
            original_je.voided_at = datetime.utcnow()
            original_je.voided_by = current_user.id
            original_je.void_reason = void_reason
        
        payment.voided_at = datetime.utcnow()
        payment.voided_by = current_user.id
        payment.void_reason = void_reason
        
        log_action(f'Voided Payment #{payment.id} for {payment.ref_type} #{payment.ref_id}. Reason: {void_reason}')
        db.session.commit()
        
        flash(f'Payment #{payment.id} has been voided successfully.', 'success')
        
    except Exception as e:
        db.session.rollback()
        flash(f'Error voiding payment: {str(e)}', 'danger')
    
    return redirect(request.referrer or url_for('core.index'))


@void_bp.route('/stock-adjustment/<int:adjustment_id>', methods=['POST'])
@login_required
@role_required('Admin', 'Accountant')
def void_stock_adjustment(adjustment_id):
    """Void a Stock Adjustment"""
    adjustment = StockAdjustment.query.get_or_404(adjustment_id)
    
    if adjustment.voided_at:
        flash('This adjustment has already been voided.', 'warning')
        return redirect(url_for('core.inventory'))
    
    void_reason = request.form.get('void_reason', '').strip()
    if not void_reason:
        flash('Void reason is required.', 'danger')
        return redirect(request.referrer or url_for('core.inventory'))
    
    try:
        # Reverse the quantity change
        product = adjustment.product
        product.quantity -= adjustment.quantity_changed
        product.quantity = max(0, product.quantity)
        
        # Remove any lots created by this adjustment
        lots = InventoryLot.query.filter_by(adjustment_id=adjustment.id).all()
        for lot in lots:
            db.session.delete(lot)
        
        # Create reversing journal entry
        original_je = JournalEntry.query.filter(
            JournalEntry.description.like(f'%Stock%{product.name}%{adjustment.reason}%')
        ).filter(JournalEntry.voided_at.is_(None)).first()
        
        if original_je:
            original_entries = original_je.entries()
            reversed_entries = []
            
            for entry in original_entries:
                reversed_entries.append({
                    'account_code': entry['account_code'],
                    'debit': entry.get('credit', 0),
                    'credit': entry.get('debit', 0)
                })
            
            reversing_je = JournalEntry(
                description=f'[VOID] Stock Adjustment #{adjustment.id} for {product.name} - {void_reason}',
                entries_json=json.dumps(reversed_entries),
                created_at=datetime.utcnow()
            )
            db.session.add(reversing_je)
            
            original_je.voided_at = datetime.utcnow()
            original_je.voided_by = current_user.id
            original_je.void_reason = void_reason
        
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


@void_bp.route('/journal-entry/<int:je_id>', methods=['POST'])
@login_required
@role_required('Admin', 'Accountant')
def void_journal_entry(je_id):
    """Void a Manual Journal Entry"""
    journal_entry = JournalEntry.query.get_or_404(je_id)
    
    if journal_entry.voided_at:
        flash('This journal entry has already been voided.', 'warning')
        return redirect(url_for('core.journal_entries'))
    
    void_reason = request.form.get('void_reason', '').strip()
    if not void_reason:
        flash('Void reason is required.', 'danger')
        return redirect(request.referrer or url_for('core.journal_entries'))
    
    try:
        # Create reversing entry
        original_entries = journal_entry.entries()
        reversed_entries = []
        
        for entry in original_entries:
            reversed_entries.append({
                'account_code': entry['account_code'],
                'debit': entry.get('credit', 0),
                'credit': entry.get('debit', 0)
            })
        
        reversing_je = JournalEntry(
            description=f'[VOID] JE #{journal_entry.id} - {journal_entry.description} - {void_reason}',
            entries_json=json.dumps(reversed_entries),
            created_at=datetime.utcnow()
        )
        db.session.add(reversing_je)
        
        # Mark original as voided
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