from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from datetime import datetime
from sqlalchemy import func
import json
from sqlalchemy.orm import validates
from decimal import Decimal, ROUND_HALF_UP, getcontext
from sqlalchemy.types import TypeDecorator, Numeric as SA_Numeric


db = SQLAlchemy()

getcontext().prec = 28

class Money(TypeDecorator):
    """
    SQLAlchemy TypeDecorator to store Decimal values in a NUMERIC/DECIMAL column.
    - Python value: decimal.Decimal (quantized to 2 decimal places, ROUND_HALF_UP)
    - DB value: Decimal stored in NUMERIC(18,2)
    """
    impl = SA_Numeric(precision=18, scale=2)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        # Called when saving to DB. Accepts Decimal, float, int, or str.
        if value is None:
            return None
        if not isinstance(value, Decimal):
            try:
                value = Decimal(str(value))
            except Exception:
                raise ValueError(f"Cannot convert {value!r} to Decimal")
        # Quantize to 2 decimal places using banker's or HALF_UP? Use HALF_UP for currency
        return value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

    def process_result_value(self, value, dialect):
        # Called when reading from DB; returns Decimal or None
        if value is None:
            return None
        if not isinstance(value, Decimal):
            value = Decimal(value)
        return value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

    def python_type(self):
        return Decimal


class CompanyProfile(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    business_style = db.Column(db.String(200))
    tin = db.Column(db.String(50), nullable=False)
    address = db.Column(db.String(300), nullable=False)
    license_key = db.Column(db.String(100))
    next_or_number = db.Column(db.Integer, default=1)
    next_si_number = db.Column(db.Integer, default=1)
    next_invoice_number = db.Column(db.Integer, default=1)
    next_consignment_number = db.Column(db.Integer, default=1)
    branch = db.Column(db.String(100), nullable=True)

class Account(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(32), unique=True, nullable=False)
    name = db.Column(db.String(200), nullable=False)
    type = db.Column(db.String(50), nullable=False)  # Asset, Liability, Equity, Revenue, Expense
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class Product(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    sku = db.Column(db.String(64), unique=True, nullable=False)
    name = db.Column(db.String(200), nullable=False)
    category = db.Column(db.String(50), nullable=True)
    sale_price = db.Column(Money(), nullable=False, default=Decimal('0.00'))
    cost_price = db.Column(Money(), nullable=False, default=Decimal('0.00'))
    quantity = db.Column(db.Integer, nullable=False, default=0)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    LOW_STOCK_THRESHOLD = 5

    def is_low_stock(self):
        return self.quantity <= self.LOW_STOCK_THRESHOLD

    def to_dict(self):
        return {
            "id": self.id,
            "sku": self.sku,
            "name": self.name,
            "sale_price": format(self.sale_price, '0.2f') if self.sale_price is not None else "0.00",
            "cost_price": format(self.cost_price, '0.2f') if self.cost_price is not None else "0.00",
            "quantity": self.quantity,
            "low": self.is_low_stock()
        }

    def adjust_stock(self, change):
        self.quantity = max(self.quantity + change, 0)
    
    @validates('sale_price', 'cost_price')
    def validate_prices(self, key, value):
        if value is None:
            return Decimal('0.00')
        if not isinstance(value, Decimal):
            value = Decimal(str(value))
        if value < 0:
            raise ValueError(f'{key} cannot be negative')
        return value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    
    @validates('quantity')
    def validate_quantity(self, key, value):
        if value < 0:
            raise ValueError('Quantity cannot be negative')
        return value


# Add this new model after the Product model

class InventoryLot(db.Model):
    """Tracks inventory purchases in chronological order for FIFO costing"""
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'), nullable=False)
    product = db.relationship('Product', backref='inventory_lots')
    
    quantity_remaining = db.Column(db.Integer, nullable=False)  # How many units left in this lot
    unit_cost = db.Column(Money(), nullable=False)  # Cost per unit for this lot
    
    # Reference to the source transaction
    purchase_id = db.Column(db.Integer, db.ForeignKey('purchase.id'), nullable=True)
    purchase_item_id = db.Column(db.Integer, db.ForeignKey('purchase_item.id'), nullable=True)
    adjustment_id = db.Column(db.Integer, db.ForeignKey('stock_adjustment.id'), nullable=True)
    
    movement_id = db.Column(db.Integer, db.ForeignKey('inventory_movement.id'), nullable=True)
    movement = db.relationship('InventoryMovement', backref='lots')

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    
    # For tracking initial inventory from bulk uploads
    is_opening_balance = db.Column(db.Boolean, default=False)
    
    def __repr__(self):
        return f'<InventoryLot {self.id}: Product {self.product_id}, Qty: {self.quantity_remaining}, Cost: {self.unit_cost}>'


class InventoryTransaction(db.Model):
    """Records the consumption of inventory lots (for audit trail)"""
    id = db.Column(db.Integer, primary_key=True)
    lot_id = db.Column(db.Integer, db.ForeignKey('inventory_lot.id'), nullable=False)
    lot = db.relationship('InventoryLot')
    
    quantity_used = db.Column(db.Integer, nullable=False)
    unit_cost = db.Column(Money(), nullable=False)
    total_cost = db.Column(Money(), nullable=False)
    
    # Reference to the transaction that consumed inventory
    sale_id = db.Column(db.Integer, db.ForeignKey('sale.id'), nullable=True)
    sale_item_id = db.Column(db.Integer, db.ForeignKey('sale_item.id'), nullable=True)
    ar_invoice_id = db.Column(db.Integer, db.ForeignKey('ar_invoice.id'), nullable=True)
    ar_invoice_item_id = db.Column(db.Integer, db.ForeignKey('ar_invoice_item.id'), nullable=True)
    adjustment_id = db.Column(db.Integer, db.ForeignKey('stock_adjustment.id'), nullable=True)
    
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    
    def __repr__(self):
        return f'<InventoryTransaction {self.id}: Lot {self.lot_id}, Qty: {self.quantity_used}, Cost: {self.total_cost}>'

class Sale(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    customer_name = db.Column(db.String(200), nullable=True)
    total = db.Column(Money(), nullable=False, default=Decimal('0.00'))
    vat = db.Column(Money(), nullable=False, default=Decimal('0.00'))
    is_vatable = db.Column(db.Boolean, nullable=False, default=True)
    status = db.Column(db.String(50), default='paid')
    items = db.relationship('SaleItem', backref='sale', cascade='all, delete-orphan')
    document_number = db.Column(db.String(50), unique=True)
    document_type = db.Column(db.String(10)) # To store 'OR' or 'SI'
    discount_type = db.Column(db.String(20), nullable=True)     # 'percent' or 'fixed'
    discount_input = db.Column(db.Float, nullable=True)         # the user-entered percentage or fixed amount
    discount_value = db.Column(Money(), nullable=True, default=Decimal('0.00'))

    voided_at = db.Column(db.DateTime, nullable=True)
    voided_by = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    void_reason = db.Column(db.String(500), nullable=True)
    voided_by_user = db.relationship('User', foreign_keys=[voided_by])


class SaleItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    sale_id = db.Column(db.Integer, db.ForeignKey('sale.id'), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'), nullable=True)  # ✅ Allow NULL for consignment
    product_name = db.Column(db.String(200))
    sku = db.Column(db.String(64))
    qty = db.Column(db.Integer, nullable=False)
    unit_price = db.Column(Money(), nullable=False)
    line_total = db.Column(Money(), nullable=False)
    cogs = db.Column(Money(), nullable=False, default=Decimal('0.00'))

class Purchase(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    supplier = db.Column(db.String(200))
    total = db.Column(Money(), nullable=False, default=Decimal('0.00'))
    vat = db.Column(Money(), nullable=False, default=Decimal('0.00'))
    is_vatable = db.Column(db.Boolean, nullable=False, default=True)
    status = db.Column(db.String(50), default='Recorded', nullable=False)
    items = db.relationship('PurchaseItem', backref='purchase', cascade='all, delete-orphan')

    due_date = db.Column(db.DateTime, nullable=True)
    payment_type = db.Column(db.String(20), nullable=False, default='Credit')
    paid = db.Column(Money(), nullable=False, default=Decimal('0.00'))

    voided_at = db.Column(db.DateTime, nullable=True)
    voided_by = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    void_reason = db.Column(db.String(500), nullable=True)
    voided_by_user = db.relationship('User', foreign_keys=[voided_by])

class PurchaseItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    purchase_id = db.Column(db.Integer, db.ForeignKey('purchase.id'), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'), nullable=False)
    product_name = db.Column(db.String(200))
    sku = db.Column(db.String(64))
    qty = db.Column(db.Integer, nullable=False)
    unit_cost = db.Column(Money(), nullable=False)
    line_total = db.Column(Money(), nullable=False)

class JournalEntry(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    description = db.Column(db.String(400))
    entries_json = db.Column(db.Text)

    voided_at = db.Column(db.DateTime, nullable=True)
    voided_by = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    void_reason = db.Column(db.String(500), nullable=True)
    voided_by_user = db.relationship('User', foreign_keys=[voided_by])

    def entries(self):
        try:
            return json.loads(self.entries_json)
        except (json.JSONDecodeError, TypeError):
            return []

# ✅ --- FIX: Inherits from UserMixin to integrate with Flask-Login ---
class User(db.Model, UserMixin):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(100), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)
    role = db.Column(db.String(50), nullable=False, default='Cashier')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class Customer(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    tin = db.Column(db.String(50))
    address = db.Column(db.String(300))
    wht_rate_percent = db.Column(db.Float, default=0.0)
    payment_terms_days = db.Column(db.Integer, default=30)  # ADD THIS LINE - default 30 days

class Supplier(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    tin = db.Column(db.String(50))
    address = db.Column(db.String(300))

class ARInvoice(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey('customer.id'), nullable=True)
    customer = db.relationship('Customer')
    date = db.Column(db.DateTime, default=datetime.utcnow)
    due_date = db.Column(db.DateTime, nullable=True)  # ADD THIS LINE
    total = db.Column(Money(), nullable=False, default=Decimal('0.00'))
    vat = db.Column(Money(), nullable=False, default=Decimal('0.00'))
    paid = db.Column(Money(), nullable=False, default=Decimal('0.00'))
    status = db.Column(db.String(50), default='Open')
    
    # NEW FIELDS
    is_vatable = db.Column(db.Boolean, nullable=False, default=True)
    invoice_number = db.Column(db.String(50), unique=True, nullable=True)
    description = db.Column(db.String(400))
    items = db.relationship('ARInvoiceItem', backref='ar_invoice', cascade='all, delete-orphan')

    voided_at = db.Column(db.DateTime, nullable=True)
    voided_by = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    void_reason = db.Column(db.String(500), nullable=True)
    voided_by_user = db.relationship('User', foreign_keys=[voided_by])
    
    # ADD THIS METHOD
    def days_overdue(self):
        """Calculate how many days overdue this invoice is"""
        if self.status == 'Paid' or not self.due_date:
            return 0
        today = datetime.utcnow()
        if today > self.due_date:
            return (today - self.due_date).days
        return 0

class APInvoice(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    supplier_id = db.Column(db.Integer, db.ForeignKey('supplier.id'), nullable=True)
    supplier = db.relationship('Supplier')
    date = db.Column(db.DateTime, default=datetime.utcnow)
    invoice_number = db.Column(db.String(100), nullable=True)
    description = db.Column(db.String(400), nullable=True)
    due_date = db.Column(db.DateTime, nullable=True)
    total = db.Column(Money(), nullable=False)
    vat = db.Column(Money(), nullable=False, default=Decimal('0.00'))
    paid = db.Column(Money(), nullable=False, default=Decimal('0.00'))
    status = db.Column(db.String(50), default='Open')
    is_vatable = db.Column(db.Boolean, nullable=False, default=True)
    expense_account_code = db.Column(db.String(32), db.ForeignKey('account.code'))


    voided_at = db.Column(db.DateTime, nullable=True)
    voided_by = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    void_reason = db.Column(db.String(500), nullable=True)
    voided_by_user = db.relationship('User', foreign_keys=[voided_by])

class Payment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.DateTime, default=datetime.utcnow)
    amount = db.Column(Money(), nullable=False)
    ref_type = db.Column(db.String(20))
    ref_id = db.Column(db.Integer)
    method = db.Column(db.String(50))
    wht_amount = db.Column(Money(), default=Decimal('0.00'))

    voided_at = db.Column(db.DateTime, nullable=True)
    voided_by = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    void_reason = db.Column(db.String(500), nullable=True)
    voided_by_user = db.relationship('User', foreign_keys=[voided_by])

class CreditMemo(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.DateTime, default=datetime.utcnow)
    customer_id = db.Column(db.Integer, db.ForeignKey('customer.id'), nullable=False)
    ar_invoice_id = db.Column(db.Integer, db.ForeignKey('ar_invoice.id'), nullable=True)
    reason = db.Column(db.String(300))
    amount_net = db.Column(Money(), nullable=False)
    vat = db.Column(Money(), nullable=False)
    total_amount = db.Column(Money(), nullable=False)
    # Relationships
    customer = db.relationship('Customer')
    ar_invoice = db.relationship('ARInvoice')

class StockAdjustment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'), nullable=False)
    product = db.relationship('Product')
    quantity_changed = db.Column(db.Integer, nullable=False) # e.g., -5 for loss, 10 for found
    reason = db.Column(db.String(255), nullable=False) # e.g., 'Spoilage', 'Physical Count Correction'
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    user = db.relationship('User', foreign_keys=[user_id])

    voided_at = db.Column(db.DateTime, nullable=True)
    voided_by = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    void_reason = db.Column(db.String(500), nullable=True)
    voided_by_user = db.relationship('User', foreign_keys=[voided_by])

class AuditLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    user = db.relationship('User')
    action = db.Column(db.String(255), nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    ip_address = db.Column(db.String(45)) # To store user's IP

    def __repr__(self):
        username = self.user.username if self.user else 'System'
        return f'<AuditLog {self.timestamp} - {username}: {self.action}>'


# Add this new model after ARInvoice class
class ARInvoiceItem(db.Model):
    """Line items for product-based AR invoices"""
    id = db.Column(db.Integer, primary_key=True)
    ar_invoice_id = db.Column(db.Integer, db.ForeignKey('ar_invoice.id'), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'), nullable=False)
    product_name = db.Column(db.String(200))
    sku = db.Column(db.String(64))
    qty = db.Column(db.Integer, nullable=False)
    unit_price = db.Column(Money(), nullable=False)
    line_total = db.Column(Money(), nullable=False)
    cogs = db.Column(Money(), nullable=False, default=Decimal('0.00'))
    is_vatable = db.Column(db.Boolean, nullable=False, default=True)
    
    # Relationship
    product = db.relationship('Product')

class RecurringBill(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    supplier_id = db.Column(db.Integer, db.ForeignKey('supplier.id'), nullable=False)
    supplier = db.relationship('Supplier')
    expense_account_code = db.Column(db.String(32), db.ForeignKey('account.code'))
    description = db.Column(db.String(400))
    total = db.Column(Money(), nullable=False)
    vat = db.Column(Money(), default=Decimal('0.00'))
    is_vatable = db.Column(db.Boolean, default=True)
    frequency = db.Column(db.String(50)) # e.g., 'monthly', 'quarterly'
    next_due_date = db.Column(db.DateTime)
    is_active = db.Column(db.Boolean, default=True)

    # Add these models to your existing models.py file

class ConsignmentSupplier(db.Model):
    """Suppliers who consign goods to you (Consignors)"""
    __tablename__ = 'consignment_supplier'
    
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False, unique=True)
    business_type = db.Column(db.String(100))  # e.g., "Manufacturer", "Distributor"
    tin = db.Column(db.String(50))
    address = db.Column(db.String(300))
    contact_person = db.Column(db.String(200))
    phone = db.Column(db.String(50))
    email = db.Column(db.String(100))
    
    # Commission you earn for selling their goods
    default_commission_rate = db.Column(db.Float, default=15.0)  # % (e.g., 15%)
    
    # Payment terms (how often you remit to them)
    payment_terms_days = db.Column(db.Integer, default=30)  # e.g., every 30 days
    
    is_active = db.Column(db.Boolean, default=True)
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    # Relationships
    consignments = db.relationship('ConsignmentReceived', backref='supplier', lazy='dynamic')

class ConsignmentReceived(db.Model):
    """Goods received on consignment (YOU are the Consignee/Retailer)"""
    __tablename__ = 'consignment_received'
    
    id = db.Column(db.Integer, primary_key=True)
    receipt_number = db.Column(db.String(50), unique=True, nullable=False)
    
    supplier_id = db.Column(db.Integer, db.ForeignKey('consignment_supplier.id'), nullable=False)
    
    date_received = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    expected_return_date = db.Column(db.DateTime, nullable=True)
    
    # Commission rate for this specific consignment
    commission_rate = db.Column(db.Float, default=15.0)
    
    total_items = db.Column(db.Integer, default=0)
    total_value = db.Column(Money(), default=Decimal('0.00'))  # Total retail value
    
    status = db.Column(db.String(50), default='Active', nullable=False)
    # Status: Active, Partial (some sold), Closed (all sold/returned), Cancelled
    
    notes = db.Column(db.Text)
    
    # Relationships
    items = db.relationship('ConsignmentItem', backref='consignment', cascade='all, delete-orphan', lazy='dynamic')

    remittances = db.relationship('ConsignmentRemittance', back_populates='consignment', lazy='dynamic')
    
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    created_by_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    created_by = db.relationship('User')
    
    def get_total_sold_value(self):
        """Calculate total value of items sold"""
        sold = db.session.query(func.sum(ConsignmentItem.quantity_sold * ConsignmentItem.retail_price))\
            .filter(ConsignmentItem.consignment_id == self.id).scalar()
        if sold is None:
            return Decimal('0.00')
        # Ensure Decimal
        if not isinstance(sold, Decimal):
            sold = Decimal(str(sold))
        return sold.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    
    def get_commission_earned(self):
        sold_value = self.get_total_sold_value()
        # commission_rate is float (percent)
        pct = Decimal(str(self.commission_rate or 0.0)) / Decimal('100')
        commission = (sold_value * pct).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        return commission
    
    def get_amount_due_to_supplier(self):
        sold_value = self.get_total_sold_value()
        commission = self.get_commission_earned()
        net = (sold_value - commission).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        return net

class ConsignmentItem(db.Model):
    """Individual consigned products (NOT in your regular inventory)"""
    __tablename__ = 'consignment_item'
    
    id = db.Column(db.Integer, primary_key=True)
    consignment_id = db.Column(db.Integer, db.ForeignKey('consignment_received.id'), nullable=False)
    
    # Product info (separate from your regular products)
    sku = db.Column(db.String(64), nullable=False)  # Supplier's SKU
    product_name = db.Column(db.String(200), nullable=False)
    description = db.Column(db.String(500))
    barcode = db.Column(db.String(100))  # For scanning in POS
    
    # Quantities
    quantity_received = db.Column(db.Integer, nullable=False)
    quantity_sold = db.Column(db.Integer, default=0)
    quantity_returned = db.Column(db.Integer, default=0)
    quantity_damaged = db.Column(db.Integer, default=0)
    
    # Pricing
    retail_price = db.Column(Money(), nullable=False)  # Agreed selling price
    
    is_active = db.Column(db.Boolean, default=True)  # Can be sold in POS
    
    @property
    def quantity_available(self):
        """Calculate available quantity for sale"""
        return self.quantity_received - self.quantity_sold - self.quantity_returned - self.quantity_damaged
    
    def to_dict(self):
        """Convert to dict for POS JSON"""
        return {
            'id': self.id,
            'sku': self.sku,
            'name': self.product_name,
            'price': format(self.retail_price, '0.2f') if self.retail_price is not None else "0.00",
            'quantity': self.quantity_available,
            'is_consignment': True,
            'consignment_id': self.consignment_id
        }

class ConsignmentSale(db.Model):
    """Track individual sales of consigned goods"""
    __tablename__ = 'consignment_sale'
    
    id = db.Column(db.Integer, primary_key=True)
    consignment_id = db.Column(db.Integer, db.ForeignKey('consignment_received.id'), nullable=False)
    consignment = db.relationship('ConsignmentReceived')
    
    # Link to regular sale (if sold through POS)
    sale_id = db.Column(db.Integer, db.ForeignKey('sale.id'), nullable=True)
    sale = db.relationship('Sale')
    
    sale_date = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    
    total_amount = db.Column(Money(), nullable=False)  # Retail value
    commission_rate = db.Column(db.Float, nullable=False)
    commission_amount = db.Column(Money(), nullable=False)
    amount_due_to_supplier = db.Column(Money(), nullable=False)  # Total - Commission
    
    vat = db.Column(Money(), default=Decimal('0.00'))
    is_vatable = db.Column(db.Boolean, default=True)
    
    payment_status = db.Column(db.String(50), default='Pending')  # Pending, Paid
    
    items = db.relationship('ConsignmentSaleItem', backref='consignment_sale', cascade='all, delete-orphan')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class ConsignmentSaleItem(db.Model):
    """Line items for consignment sales"""
    __tablename__ = 'consignment_sale_item'
    
    id = db.Column(db.Integer, primary_key=True)
    consignment_sale_id = db.Column(db.Integer, db.ForeignKey('consignment_sale.id'), nullable=False)
    consignment_item_id = db.Column(db.Integer, db.ForeignKey('consignment_item.id'), nullable=False)
    consignment_item = db.relationship('ConsignmentItem')
    
    quantity_sold = db.Column(db.Integer, nullable=False)
    unit_price = db.Column(Money(), nullable=False)
    line_total = db.Column(Money(), nullable=False)

class ConsignmentPayment(db.Model):
    """Track payments remitted to consignors"""
    __tablename__ = 'consignment_payment'
    
    id = db.Column(db.Integer, primary_key=True)
    payment_number = db.Column(db.String(50), unique=True)
    
    supplier_id = db.Column(db.Integer, db.ForeignKey('consignment_supplier.id'), nullable=False)
    supplier = db.relationship('ConsignmentSupplier')
    
    payment_date = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    
    # Amounts
    total_sales = db.Column(Money(), nullable=False)  # Gross sales
    commission_amount = db.Column(Money(), nullable=False)  # Your commission
    wht_amount = db.Column(Money(), default=Decimal('0.00')) # Withholding tax (if applicable)
    net_payment = db.Column(Money(), nullable=False)  # Sales - Commission - WHT
    
    payment_method = db.Column(db.String(50))  # Cash, Bank, Check
    reference_number = db.Column(db.String(100))  # Bank ref, check number
    
    notes = db.Column(db.Text)
    
    created_by_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    created_by = db.relationship('User')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class ConsignmentReturn(db.Model):
    """Track returns of unsold consignment goods to supplier"""
    __tablename__ = 'consignment_return'
    
    id = db.Column(db.Integer, primary_key=True)
    return_number = db.Column(db.String(50), unique=True)
    
    consignment_id = db.Column(db.Integer, db.ForeignKey('consignment_received.id'), nullable=False)
    consignment = db.relationship('ConsignmentReceived')
    
    return_date = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    reason = db.Column(db.String(300))
    
    items = db.relationship('ConsignmentReturnItem', backref='consignment_return', cascade='all, delete-orphan')
    
    created_by_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    created_by = db.relationship('User')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class ConsignmentReturnItem(db.Model):
    """Line items for consignment returns"""
    __tablename__ = 'consignment_return_item'
    
    id = db.Column(db.Integer, primary_key=True)
    consignment_return_id = db.Column(db.Integer, db.ForeignKey('consignment_return.id'), nullable=False)
    consignment_item_id = db.Column(db.Integer, db.ForeignKey('consignment_item.id'), nullable=False)
    consignment_item = db.relationship('ConsignmentItem')
    
    quantity_returned = db.Column(db.Integer, nullable=False)
    reason = db.Column(db.String(300))

class ConsignmentRemittance(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    consignment_id = db.Column(db.Integer, db.ForeignKey('consignment_received.id'), nullable=False)
    date_paid = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    amount_paid = db.Column(Money(), nullable=False)
    payment_method = db.Column(db.String(50))
    notes = db.Column(db.Text)
    created_by_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    
    consignment = db.relationship('ConsignmentReceived', back_populates='remittances')
    created_by = db.relationship('User')

    def __repr__(self):
        return f'<ConsignmentRemittance {self.id} for {self.consignment_id}>'


class Branch(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False, unique=True)
    address = db.Column(db.String(300), nullable=True)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class InventoryMovement(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    movement_type = db.Column(db.String(20), nullable=False)  # 'receive' or 'transfer'
    from_branch_id = db.Column(db.Integer, db.ForeignKey('branch.id'), nullable=True)
    to_branch_id = db.Column(db.Integer, db.ForeignKey('branch.id'), nullable=True)
    reference_number = db.Column(db.String(50), nullable=True)
    notes = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    created_by = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)

    from_branch = db.relationship('Branch', foreign_keys=[from_branch_id])
    to_branch = db.relationship('Branch', foreign_keys=[to_branch_id])
    items = db.relationship('InventoryMovementItem', backref='movement', cascade='all, delete-orphan')

class InventoryMovementItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    movement_id = db.Column(db.Integer, db.ForeignKey('inventory_movement.id'), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'), nullable=False)
    quantity = db.Column(db.Integer, nullable=False)
    unit_cost = db.Column(Money(), nullable=False)

    product = db.relationship('Product')