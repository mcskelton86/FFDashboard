from flask import Flask, render_template, request, jsonify
import re
from datetime import datetime, timedelta
import gspread
from google.oauth2.service_account import Credentials
import os
import json
import base64
from PIL import Image
import pytesseract
import io

app = Flask(__name__)

# Google Sheets setup
SCOPES = ['https://www.googleapis.com/auth/spreadsheets']
SHEET_ID = '1nnEwkIrvAQwIIQDnHBPAYumoTBv04wgOnq_R8A7xuIM'

# File upload configuration
UPLOAD_FOLDER = '/tmp/payslips'
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'bmp'}
MAX_FILE_SIZE = 16 * 1024 * 1024  # 16MB

if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = MAX_FILE_SIZE

def get_sheets_client():
    """Get authenticated Google Sheets client"""
    try:
        creds_json = os.getenv('GOOGLE_CREDENTIALS')
        if creds_json:
            creds_dict = json.loads(creds_json)
            creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
        else:
            creds = Credentials.from_service_account_file('creds.json', scopes=SCOPES)
        return gspread.authorize(creds)
    except:
        return None

# ===== STATEMENT PARSING =====

def parse_nationwide_pdf(pdf_bytes):
    """Parse a Nationwide credit card / bank statement PDF.

    Each transaction line on the statement is:
        DD/MM/YY REFNO DESCRIPTION £AMOUNT
    where REFNO is the bank's unique reference (we use this as the FITID).
    """
    import pdfplumber
    import io

    transactions = []
    line_pattern = re.compile(
        r'^(\d{2}/\d{2}/\d{2})\s+(\d+)\s+(.+?)\s+(-?)£([\d,]+\.\d{2})\s*(CR)?$'
    )

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ''
            for line in text.split('\n'):
                line = line.strip()
                m = line_pattern.match(line)
                if not m:
                    continue
                date_raw, ref_no, description, neg_sign, amount_str, cr_marker = m.groups()
                try:
                    amount = float(amount_str.replace(',', ''))
                except ValueError:
                    continue

                # On a credit card statement, "CR" or a leading minus marks a
                # credit (money in / payment received). Everything else is a
                # charge (money out).
                is_credit = bool(cr_marker) or bool(neg_sign)
                amount_in = amount if is_credit else 0
                amount_out = 0 if is_credit else amount

                # Convert DD/MM/YY -> DD Mmm YYYY
                try:
                    parsed_date = datetime.strptime(date_raw, '%d/%m/%y')
                    date_str = parsed_date.strftime('%d %b %Y')
                except ValueError:
                    date_str = date_raw

                transactions.append({
                    'date': date_str,
                    'description': description.strip()[:100],
                    'amountOut': round(amount_out, 2),
                    'amountIn': round(amount_in, 2),
                    'category': categorize_transaction(description, is_credit),
                    'fitid': ref_no,
                    'keepUncategorized': False
                })

    return transactions

def normalize_date(date_str):
    """Normalize date from various formats to 'DD Mmm YYYY'"""
    if not date_str:
        return None

    # Try parsing as raw OFX format (YYYYMMDD...)
    try:
        date_part = date_str[:8]
        dt = datetime.strptime(date_part, '%Y%m%d')
        return dt.strftime('%d %b %Y')
    except:
        pass

    # Try parsing as already formatted (DD Mmm YYYY)
    try:
        dt = datetime.strptime(date_str, '%d %b %Y')
        return dt.strftime('%d %b %Y')
    except:
        pass

    return date_str

def categorize_transaction(description, is_income=False):
    """Categorize transaction based on description"""
    desc_upper = description.upper()

    # Any positive-amount transaction defaults to Income.
    if is_income:
        return 'Income'

    # Expense categorization. Order matters — first match wins, so put more
    # specific categories before broader ones.
    categories = {
        'Eating Out': ['CAFE', 'COFFEE', 'COSTA', 'STARBUCKS', 'PUB', 'BAR ',
                        'BREWHOUSE', 'RESTAURANT', 'MAYFLOWER', 'TRES BON',
                        'STONEGATE', 'MCDONALD', 'GREGGS', 'PIZZA', 'KFC',
                        'NANDOS', 'WAGAMAMA', 'BURGER'],
        'Groceries': ['TESCO', 'SAINSBURY', 'ASDA', 'MORRISONS', 'WAITROSE', 'LIDL', 'ALDI', 'CO-OP', 'COOP', 'M&S FOOD'],
        'Shopping': ['AMAZON', 'EBAY', 'ARGOS', 'JOHN LEWIS', 'MARKS', 'DEBENHAMS'],
        'Utilities': ['OCTOPUS', 'WATER', 'GAS', 'ELECTRIC', 'EDFENERGY'],
        'Entertainment': ['CINEMA', 'NETFLIX', 'SPOTIFY', 'BT SPORT', 'NOW TV'],
        'Transport': ['UBER', 'TFL', 'SERVICE STATION', 'PETROL', 'FUEL', 'SHELL ', ' BP ', 'ESSO', 'WIGHTLINK', 'PAYBYPHONE'],
        'Subscriptions': ['APPLE.COM', 'MICROSOFT', 'PARAMOUNT', 'CRUNCHYROLL', 'ADOBE', 'SLACK'],
        'Phone & Internet': ['VODAFONE', 'TROOLI', 'VIRGIN MEDIA', 'SKY '],
        'Healthcare': ['SPECSAVERS', 'BOOTS', 'PHARMACY', 'DOCTOR', 'DENTIST', 'NHS'],
        'Council Tax': ['FOREST DC', 'COUNCIL'],
        'Housing': ['RENT', 'MORTGAGE', 'LANDLORD'],
    }

    for category, keywords in categories.items():
        for keyword in keywords:
            if keyword in desc_upper:
                return category

    return 'Other'

# ===== PAYSLIP HANDLING =====

def parse_payslip_text(ocr_text):
    """Extract income amount, hours, and date from OCR'd payslip text"""
    result = {
        'amount': None,
        'hours': None,
        'date': None
    }

    # Extract income amount (look for patterns like "£1,234.56" or "1234.56")
    amount_match = re.search(r'[£]?(\d{1,5}[,.]?\d{0,2})', ocr_text, re.IGNORECASE)
    if amount_match:
        amount_str = amount_match.group(1).replace(',', '')
        try:
            result['amount'] = float(amount_str)
        except:
            pass

    # Extract hours worked (look for patterns like "40 hours", "40hrs", etc.)
    hours_match = re.search(r'(\d+\.?\d*)\s*(?:hours?|hrs?)', ocr_text, re.IGNORECASE)
    if hours_match:
        try:
            result['hours'] = float(hours_match.group(1))
        except:
            pass

    # Extract date (look for DD/MM/YY format)
    date_match = re.search(r'(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})', ocr_text)
    if date_match:
        try:
            day, month, year = date_match.groups()
            # Handle 2-digit year
            if len(year) == 2:
                year = '20' + year
            dt = datetime(int(year), int(month), int(day))
            result['date'] = dt.strftime('%d %b %Y')
        except:
            pass

    return result

# ===== API ROUTES =====

@app.route('/')
def index():
    return render_template('dashboard.html')

@app.route('/upload')
def upload():
    return render_template('index.html')

@app.route('/dashboard')
def dashboard():
    return render_template('dashboard.html')

@app.route('/payslip-review')
def payslip_review():
    return render_template('payslip_review.html')

@app.route('/parse-pdf', methods=['POST'])
def parse_pdf():
    try:
        if 'file' not in request.files:
            return jsonify({'success': False, 'error': 'No file uploaded'})
        f = request.files['file']
        pdf_bytes = f.read()
        if not pdf_bytes:
            return jsonify({'success': False, 'error': 'Empty file'})

        transactions = parse_nationwide_pdf(pdf_bytes)
        if not transactions:
            return jsonify({'success': False, 'error': 'No transactions found in PDF'})
        return jsonify({
            'success': True,
            'transactions': transactions,
            'count': len(transactions)
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/save-transactions', methods=['POST'])
def save_transactions():
    try:
        data = request.json
        transactions = data.get('transactions', [])

        if not transactions:
            return jsonify({'success': False, 'error': 'No transactions to save'})

        client = get_sheets_client()
        if not client:
            return jsonify({'success': False, 'error': 'Google Sheets authentication failed'})

        sheet = client.open_by_key(SHEET_ID).worksheet('Transactions')

        # Ensure the sheet has a FITID column (the bank's unique transaction ID,
        # which is the most reliable dedup key).
        header = sheet.row_values(1)
        if 'FITID' not in header:
            sheet.update_cell(1, len(header) + 1, 'FITID')
            header = sheet.row_values(1)

        # Signature: prefer FITID alone; fall back to date|desc|out|in for rows
        # without a FITID (e.g. older imports, or banks that don't supply it).
        def _num(v):
            try:
                return f'{float(v or 0):.2f}'
            except (TypeError, ValueError):
                return '0.00'

        def _sig(date, desc, out, inn, fitid):
            if fitid:
                return f'fitid:{str(fitid).strip()}'
            return f"{(date or '').strip()}|{(desc or '').strip().lower()}|{_num(out)}|{_num(inn)}"

        # Clean duplicates already in the sheet.
        existing_records = sheet.get_all_records()
        seen = {}
        rows_to_delete = []  # 1-indexed sheet row numbers
        for idx, r in enumerate(existing_records):
            sig = _sig(
                r.get('Date'),
                r.get('Description'),
                r.get('Amount Out'),
                r.get('Amount In'),
                r.get('FITID'),
            )
            if sig in seen:
                rows_to_delete.append(idx + 2)
            else:
                seen[sig] = idx + 2

        cleaned_count = 0
        for row_num in sorted(rows_to_delete, reverse=True):
            sheet.delete_rows(row_num)
            cleaned_count += 1

        existing_sigs = set(seen.keys())

        saved_count = 0
        skipped_count = 0
        rows_to_append = []
        for txn in transactions:
            sig = _sig(txn['date'], txn['description'], txn['amountOut'], txn['amountIn'], txn.get('fitid'))
            if sig in existing_sigs:
                skipped_count += 1
                continue
            existing_sigs.add(sig)
            rows_to_append.append([
                txn['date'],
                txn['description'],
                txn['amountOut'],
                txn['amountIn'],
                txn['category'],
                datetime.now().strftime('%B'),
                datetime.now().strftime('%Y'),
                'PDF Import',
                datetime.now().strftime('%d/%m/%Y'),
                txn.get('fitid', '')
            ])
            saved_count += 1

        if rows_to_append:
            sheet.append_rows(rows_to_append)

        msg_parts = [f'Saved {saved_count} transactions']
        if skipped_count:
            msg_parts.append(f'skipped {skipped_count} duplicate{"s" if skipped_count != 1 else ""} from upload')
        if cleaned_count:
            msg_parts.append(f'removed {cleaned_count} existing duplicate{"s" if cleaned_count != 1 else ""} from sheet')
        return jsonify({
            'success': True,
            'message': ', '.join(msg_parts),
            'saved': saved_count,
            'skipped': skipped_count,
            'cleaned': cleaned_count
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/upload-payslip', methods=['POST'])
def upload_payslip():
    """Upload and process payslip image with OCR"""
    try:
        if 'file' not in request.files:
            return jsonify({'success': False, 'error': 'No file provided'})

        file = request.files['file']
        if file.filename == '':
            return jsonify({'success': False, 'error': 'No file selected'})

        # Validate file extension
        if not ('.' in file.filename and file.filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS):
            return jsonify({'success': False, 'error': 'Invalid file type. Use PNG, JPG, etc.'})

        # Read and process image
        image_data = file.read()
        image = Image.open(io.BytesIO(image_data))

        # Convert to RGB if needed
        if image.mode in ('RGBA', 'LA', 'P'):
            rgb_image = Image.new('RGB', image.size, (255, 255, 255))
            rgb_image.paste(image, mask=image.split()[-1] if image.mode == 'RGBA' else None)
            image = rgb_image

        # Run OCR
        ocr_text = pytesseract.image_to_string(image)

        # Extract payslip data
        extracted = parse_payslip_text(ocr_text)

        # Convert image to base64 for display
        buffered = io.BytesIO()
        image.save(buffered, format='PNG')
        image_base64 = base64.b64encode(buffered.getvalue()).decode('utf-8')

        return jsonify({
            'success': True,
            'extracted': extracted,
            'image_base64': image_base64,
            'ocr_text': ocr_text
        })
    except Exception as e:
        return jsonify({'success': False, 'error': f'OCR processing failed: {str(e)}'})

@app.route('/save-payslip', methods=['POST'])
def save_payslip():
    """Save confirmed payslip data to Google Sheets"""
    try:
        data = request.json
        payslip_data = {
            'date': data.get('date'),
            'amount': data.get('amount'),
            'hours': data.get('hours'),
            'person': data.get('person', 'Matthew')  # Default to Matthew
        }

        if not all([payslip_data['date'], payslip_data['amount']]):
            return jsonify({'success': False, 'error': 'Date and amount are required'})

        client = get_sheets_client()
        if not client:
            return jsonify({'success': False, 'error': 'Google Sheets authentication failed'})

        # Get or create Payslips worksheet
        spreadsheet = client.open_by_key(SHEET_ID)
        try:
            sheet = spreadsheet.worksheet('Payslips')
        except:
            # Create worksheet if it doesn't exist
            sheet = spreadsheet.add_worksheet('Payslips', 1000, 10)
            sheet.append_row(['Date', 'Amount', 'Hours', 'Person', 'Saved On'])

        row = [
            payslip_data['date'],
            payslip_data['amount'],
            payslip_data['hours'] or '',
            payslip_data['person'],
            datetime.now().strftime('%d/%m/%Y %H:%M')
        ]
        sheet.append_row(row)

        return jsonify({
            'success': True,
            'message': 'Payslip saved successfully'
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

# ===== DATA RETRIEVAL =====

def get_all_transactions():
    """Fetch all transactions from Google Sheets"""
    try:
        client = get_sheets_client()
        if not client:
            return []

        sheet = client.open_by_key(SHEET_ID).worksheet('Transactions')
        rows = sheet.get_all_values()

        # Skip header row
        transactions = []
        for row in rows[1:]:
            if len(row) >= 5:
                # Normalize date from stored format
                date_str = normalize_date(row[0])
                transactions.append({
                    'date': date_str,
                    'description': row[1],
                    'amountOut': float(row[2]) if row[2] else 0,
                    'amountIn': float(row[3]) if row[3] else 0,
                    'category': row[4] if len(row) > 4 else 'Other'
                })

        return transactions
    except Exception as e:
        print(f"Error fetching transactions: {e}")
        return []

def get_all_payslips():
    """Fetch all payslips from Google Sheets"""
    try:
        client = get_sheets_client()
        if not client:
            return []

        spreadsheet = client.open_by_key(SHEET_ID)
        try:
            sheet = spreadsheet.worksheet('Payslips')
        except:
            return []

        rows = sheet.get_all_values()

        # Skip header row
        payslips = []
        for row in rows[1:]:
            if len(row) >= 2:
                date_str = normalize_date(row[0])
                payslips.append({
                    'date': date_str,
                    'amount': float(row[1]) if row[1] else 0,
                    'hours': float(row[2]) if len(row) > 2 and row[2] else 0,
                    'person': row[3] if len(row) > 3 else 'Matthew'
                })

        return payslips
    except Exception as e:
        print(f"Error fetching payslips: {e}")
        return []

def get_this_month_summary():
    """Get summary for the most recent month that has transaction data.
    We don't have a live bank feed, so rather than the literal current/previous
    calendar month (which may be empty), fall back to the latest month that
    actually has transactions in the sheet."""
    transactions = get_all_transactions()
    payslips = get_all_payslips()

    # Always show previous calendar month (we don't have a live bank feed,
    # so the current month is incomplete — last month is the most recent
    # complete view of household spend).
    now = datetime.now()
    last_month_date = now.replace(day=1) - timedelta(days=1)
    current_month = last_month_date.strftime('%B')
    current_year = last_month_date.strftime('%Y')

    total_in = 0
    total_out = 0
    by_category = {}

    # Process transactions
    for txn in transactions:
        try:
            txn_date = datetime.strptime(txn['date'], '%d %b %Y')
            if txn_date.strftime('%B') == current_month and txn_date.strftime('%Y') == current_year:
                total_in += txn['amountIn']
                total_out += txn['amountOut']

                category = txn['category']
                if category not in by_category:
                    by_category[category] = 0
                by_category[category] += txn['amountOut']
        except:
            continue

    # Process payslips
    for payslip in payslips:
        try:
            payslip_date = datetime.strptime(payslip['date'], '%d %b %Y')
            if payslip_date.strftime('%B') == current_month and payslip_date.strftime('%Y') == current_year:
                total_in += payslip['amount']
        except:
            continue

    net = total_in - total_out
    safe_to_spend = max(0, net)

    return {
        'month_label': f'{current_month} {current_year}',
        'total_in': round(total_in, 2),
        'total_out': round(total_out, 2),
        'net': round(net, 2),
        'safe_to_spend': round(safe_to_spend, 2),
        'by_category': {k: round(v, 2) for k, v in by_category.items()}
    }

def get_12_month_trend():
    """Get income and expenses for last 12 months"""
    transactions = get_all_transactions()
    payslips = get_all_payslips()

    # Get last 12 months
    months = {}
    for i in range(11, -1, -1):
        date = datetime.now() - timedelta(days=30*i)
        month_key = date.strftime('%b %Y')
        months[month_key] = {'income': 0, 'expenses': 0}

    # Process transactions
    for txn in transactions:
        try:
            txn_date = datetime.strptime(txn['date'], '%d %b %Y')
            month_key = txn_date.strftime('%b %Y')

            if month_key in months:
                months[month_key]['income'] += txn['amountIn']
                months[month_key]['expenses'] += txn['amountOut']
        except:
            continue

    # Process payslips
    for payslip in payslips:
        try:
            payslip_date = datetime.strptime(payslip['date'], '%d %b %Y')
            month_key = payslip_date.strftime('%b %Y')

            if month_key in months:
                months[month_key]['income'] += payslip['amount']
        except:
            continue

    labels = list(months.keys())
    income = [round(months[m]['income'], 2) for m in labels]
    expenses = [round(months[m]['expenses'], 2) for m in labels]

    return {
        'labels': labels,
        'income': income,
        'expenses': expenses
    }

def calculate_income_projections():
    """Calculate income projections with two modes"""
    payslips = get_all_payslips()

    if not payslips:
        return {
            'monthly_average': 0,
            'annual_average': 0,
            'by_schedule_hours': 0,
            'by_actual_hours': 0
        }

    # Get payslips from this year
    current_year = datetime.now().strftime('%Y')
    year_payslips = [p for p in payslips if datetime.strptime(p['date'], '%d %b %Y').strftime('%Y') == current_year]

    if not year_payslips:
        year_payslips = payslips[-3:] if len(payslips) >= 3 else payslips

    # Calculate averages
    total_amount = sum(p['amount'] for p in year_payslips)
    total_hours = sum(p['hours'] for p in year_payslips if p['hours'])

    monthly_average = total_amount / len(year_payslips) if year_payslips else 0
    annual_average = monthly_average * 12

    # Based on schedule hours (assume 40 hrs/week = ~173 hrs/month)
    by_schedule_hours = (monthly_average / total_hours * 173) if total_hours else monthly_average

    # Based on actual averaged hours
    avg_actual_hours = total_hours / len(year_payslips) if year_payslips else 0
    by_actual_hours = (monthly_average / avg_actual_hours * 173) if avg_actual_hours else monthly_average

    return {
        'monthly_average': round(monthly_average, 2),
        'annual_average': round(annual_average, 2),
        'by_schedule_hours': round(by_schedule_hours * 12, 2),
        'by_actual_hours': round(by_actual_hours * 12, 2),
        'average_hours_per_month': round(avg_actual_hours, 2)
    }

# ===== API ENDPOINTS =====

@app.route('/api/dashboard-data')
def api_dashboard_data():
    try:
        summary = get_this_month_summary()
        trend = get_12_month_trend()
        projections = calculate_income_projections()
        payslips = get_all_payslips()

        return jsonify({
            'this_month': {
                'month_label': summary['month_label'],
                'total_in': summary['total_in'],
                'total_out': summary['total_out'],
                'net': summary['net'],
                'safe_to_spend': summary['safe_to_spend'],
                'by_category': summary['by_category']
            },
            'trend_12_months': {
                'labels': trend['labels'],
                'income': trend['income'],
                'expenses': trend['expenses']
            },
            'projections': projections,
            'payslips': payslips,
            'last_update': datetime.now().strftime('%d %b %Y %H:%M')
        })
    except Exception as e:
        print(f"Error in dashboard_data: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)