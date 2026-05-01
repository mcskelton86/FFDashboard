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

# ===== OFX PARSING =====

def parse_ofx_content(ofx_text):
    """Parse OFX format and extract transactions"""
    transactions = []

    # Find all STMTTRN blocks
    pattern = r'<STMTTRN>.*?</STMTTRN>'
    matches = re.finditer(pattern, ofx_text, re.DOTALL)

    for match in matches:
        stmttrn = match.group(0)

        # Extract transaction details
        trntype = extract_ofx_field(stmttrn, 'TRNTYPE')
        dtposted = extract_ofx_field(stmttrn, 'DTPOSTED')
        trnamt = extract_ofx_field(stmttrn, 'TRNAMT')
        name = extract_ofx_field(stmttrn, 'NAME')
        memo = extract_ofx_field(stmttrn, 'MEMO')

        if not (dtposted and trnamt and name):
            continue

        # Parse date
        date_str = format_ofx_date(dtposted)

        # Parse amount
        try:
            amount = float(trnamt)
        except:
            continue

        # Determine if income or expense
        amount_in = 0
        amount_out = 0
        if amount > 0:
            amount_in = amount
        else:
            amount_out = abs(amount)

        # Build description
        description = name
        if memo:
            description = f"{name} - {memo}"

        transactions.append({
            'date': date_str,
            'description': description[:100],
            'amountOut': round(amount_out, 2),
            'amountIn': round(amount_in, 2),
            'category': categorize_transaction(description, amount > 0),
            'keepUncategorized': False
        })

    return transactions

def extract_ofx_field(text, field_name):
    """Extract a field value from OFX text"""
    pattern = f'<{field_name}>([^<]+)</{field_name}>'
    match = re.search(pattern, text)
    return match.group(1).strip() if match else ''

def format_ofx_date(ofx_date):
    """Convert OFX date format to readable format"""
    try:
        # Handle both YYYYMMDD and longer formats with timestamps
        date_part = ofx_date[:8]
        dt = datetime.strptime(date_part, '%Y%m%d')
        return dt.strftime('%d %b %Y')
    except:
        return ofx_date

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

    # Income categorization - check for work locations first
    if is_income:
        income_keywords = ['MONKEY', 'CAFE', 'OLD SCHOOL']
        for keyword in income_keywords:
            if keyword in desc_upper:
                return 'Income'

    # Expense categorization
    categories = {
        'Groceries': ['TESCO', 'SAINSBURY', 'ASDA', 'MORRISONS', 'WAITROSE', 'LIDL', 'ALDI'],
        'Shopping': ['AMAZON', 'EBAY', 'ARGOS', 'JOHN LEWIS', 'MARKS', 'DEBENHAMS'],
        'Utilities': ['OCTOPUS', 'WATER', 'GAS', 'ELECTRIC', 'EDFENERGY'],
        'Entertainment': ['MONKEY BREWHOUSE', 'CINEMA', 'NETFLIX', 'SPOTIFY', 'BT SPORT', 'NOW TV'],
        'Transport': ['UBER', 'TFL', 'LYMINGTON', 'PETROL', 'FUEL', 'SHELL', 'BP'],
        'Subscriptions': ['APPLE', 'MICROSOFT', 'PARAMOUNT', 'CRUNCHYROLL', 'NOW', 'ADOBE', 'SLACK'],
        'Phone & Internet': ['EE', 'VODAFONE', 'O2', 'TROOLI', 'BT', 'VIRGIN'],
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
    return render_template('index.html')

@app.route('/dashboard')
def dashboard():
    return render_template('dashboard.html')

@app.route('/payslip-review')
def payslip_review():
    return render_template('payslip_review.html')

@app.route('/parse-ofx', methods=['POST'])
def parse_ofx():
    try:
        data = request.json
        ofx_content = data.get('content', '')

        if not ofx_content:
            return jsonify({'success': False, 'error': 'No content provided'})

        transactions = parse_ofx_content(ofx_content)

        if not transactions:
            return jsonify({'success': False, 'error': 'No transactions found in OFX file'})

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

        saved_count = 0
        for txn in transactions:
            row = [
                txn['date'],
                txn['description'],
                txn['amountOut'],
                txn['amountIn'],
                txn['category'],
                datetime.now().strftime('%B'),
                datetime.now().strftime('%Y'),
                'OFX Import',
                datetime.now().strftime('%d/%m/%Y')
            ]
            sheet.append_row(row)
            saved_count += 1

        return jsonify({
            'success': True,
            'message': f'Saved {saved_count} transactions'
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
    """Get summary for current month including transactions and payslips"""
    transactions = get_all_transactions()
    payslips = get_all_payslips()

    now = datetime.now()
    current_month = now.strftime('%B')
    current_year = now.strftime('%Y')

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