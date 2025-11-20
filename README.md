# PH-Approve Accounting System — Full Accounting MVP (with VAT & COGS)

📊 Core Accounting Features
Double-Entry Bookkeeping System - Full journal entry tracking with debit/credit balancing
 -  Files: models.py (JournalEntry), routes/core.py, routes/accounts.py
Chart of Accounts Management - Customizable account structure (Assets, Liabilities, Equity, Revenue, Expenses)
 - Files: models.py (Account), routes/accounts.py, templates/chart_of_accounts.html
FIFO Inventory Costing - First-In-First-Out cost tracking for accurate COGS calculation
 - Files: routes/fifo_utils.py, models.py (InventoryLot, InventoryTransaction)

🏪 Point of Sale (POS)

- Cash Sales with VAT - 12% Philippine VAT calculation, SC/PWD discounts
- Multiple Document Types - Official Receipts (OR), Sales Invoices (SI)
- Discount Management - Percentage, fixed amount, and senior citizen/PWD discounts
    - Files: routes/core.py (api_sale), templates/pos.html

📦 Inventory Management
- Product Management - SKU auto-generation, stock tracking, low-stock alerts
-  Bulk Import/Export - CSV upload for mass product creation
- Stock Adjustments - Inventory gains/losses with journal entries
- FIFO Lot Tracking - View inventory lots and costs per product
- Multi-Branch Transfers - Inter-branch inventory movement
    -Files: routes/core.py, routes/sku_utils.py, models.py (Product, InventoryLot)

💰 Accounts Receivable (AR)
- Customer Management - Track customers with TIN, address, payment terms
- Billing Invoices - Product-based AR invoices with line items
- Payment Recording - Track payments with withholding tax (CWT)
- Credit Memos - Sales returns with inventory restoration
- AR Aging Report - Track overdue receivables
    - Files: routes/ar_ap.py, models.py (Customer, ARInvoice, ARInvoiceItem)

💸 Accounts Payable (AP)
- Supplier Management - Maintain supplier database
- AP Invoices - Expense tracking with customizable debit accounts
- Payment Tracking - Record supplier payments
- Recurring Bills - Automated bill generation (monthly/quarterly)
- AP Aging Report - Monitor outstanding payables
    - Files: routes/ar_ap.py, models.py (Supplier, APInvoice, RecurringBill)

🤝 Consignment Management
- Consignment Suppliers - Track consignors and commission rates
- Consignment Receipts - Record goods received on consignment
- Consignment Sales - Separate tracking from regular inventory
- Commission Calculation - Automatic commission on consignment sales
- Remittance Management - Track payments to consignors
    - Files: routes/consignment.py, models.py (ConsignmentSupplier, ConsignmentReceived, ConsignmentItem)

📈 Financial Reports
- Income Statement - P&L with Revenue, COGS, Expenses breakdown
- Balance Sheet - Assets, Liabilities, Equity positioning
- Trial Balance - Account balances verification
- General Ledger - Account-by-account transaction history
- Stock Card - Product-level movement tracking
- VAT Reports - Input/Output VAT summary, VAT Return (2550M/Q)
- BIR Form 2307 - Withholding tax certificate
- Sales Reports - Summary of Purchase and Sales (SLP, SLS)
    - Files: routes/reports.py, templates/income_statement.html, templates/balance_sheet.html

👥 User Management
- Role-Based Access Control - Admin, Accountant, Cashier roles
- User Authentication - Secure login with password hashing
- Audit Log - Track all user actions with timestamps
- Password Recovery - TIN-based password reset
    - Files: routes/users.py, routes/core.py (login/logout), models.py (User, AuditLog)

🔧 System Features
- Company Profile Setup - Multi-step wizard (License, Company, Admin)
- Multi-Branch Support - Track inventory across branches
- Transaction Voiding - Void sales, purchases, adjustments with reversing entries
- Rate Limiting - Prevent brute-force attacks
- CSV Export - Export reports and transactions
- Auto SKU Generation - Category-based SKU creation
    -Files: app.py, routes/void_transactions.py, extensions.py

## Quick start
1. Create venv, install requirements:
```bash
python -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
```
2. Initialize DB and seed sample data:
```bash
python init_db.py
```
3. Run app:
```bash
python app.py
```
4. Visit `http://127.0.0.1:5000`

## Notes & Limitations
- This is a simplified accounting implementation for demo/MVP purposes only.
- For production or tax filing use, consult an accountant and add extensive validation, audits, permissions, and persistence best practices.
- VAT is fixed at 12% for this MVP but can be extended.

