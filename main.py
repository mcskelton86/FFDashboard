from flask import Flask, render_template, request, jsonify
import re
from datetime import datetime, timedelta
import gspread
from google.oauth2.service_account import Credentials
import os
import json

app = Flask(__name__)

# Google Sheets setup
SCOPES = ['https://www.googleapis.com/auth/spreadsheets']
SHEET_ID = '1nnEwkIrvAQwIIQDnHBPAYumoTBv04wgOnq_R8A7xuIM'

def get_sheets_client():
    try:
        creds_json = os.environ.get('GOOGLE_CREDENTIALS')
        if not creds_json:
            print("GOOGLE_CREDENTIALS not found in environment")
            return None
        creds_dict = json.loads(creds_json)
        creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
        return gspread.authorize(creds)
    except Exception as e:
        print(f"Auth error: {e}")
        return None

def get_all_transactions():
    """Fetch all transactions from Google Sheet"""
    try:
        client = get_sheets_client()
        if not client:
            return []

        sheet = client.open_by_key(SHEET_ID).worksheet('Transactions')
        rows = sheet.get_all_records()
        return rows
    except Exception as e:
        print(f"Error fetching transactions: {e}")
        return []

def parse_date(date_str):
    """Parse date string in format 'DD MMM YYYY'"""
    try:
        return datetime.strptime(date_str, '%d %b %Y')
    except:
        return None

def get_this_month_summary():
    """Get summary for current month"""
    transactions = get_all_transactions()
    now = datetime.now()
    current_month = now.month
    current_year = now.year

    total_in = 0
    total_out = 0
    by_category = {}

    for txn in transactions:
        try:
            date_obj = parse_date(txn.get('Date', ''))
            if not date_obj or date_obj.month != current_month or date_obj.year != current_year:
                continue

            amount_in = float(txn.get('Amount In', 0) or 0)
            amount_out = float(txn.get('Amount Out', 0) or 0)
            category = txn.get('Category', 'Other')

            total_in += amount_in
            total_out += amount_out

            if category not in by_category:
                by_category[category] = 0
            by_category[category] += amount_out
        except:
            continue

    net = total_in - total_out
    safe_to_spend = max(0, net)

    return {
        'total_in': round(total_in, 2),
        'total_out': round(total_out, 2),
        'net': round(net, 2),
        'safe_to_spend': round(safe_to_spend, 2),
        'by_category': {k: round(v, 2) for k, v in sorted(by_category.items(), key=lambda x: x[1], reverse=True)}
    }

def get_12_month_trend():
    """Get monthly totals for last 12 months"""
    transactions = get_all_transactions()
    now = datetime.now()

    months = {}
    for i in range(12):
        date = now - timedelta(days=30*i)
        key = date.strftime('%b %Y')
        months[key] = {'in': 0, 'out': 0}

    for txn in transactions:
        try:
            date_obj = parse_date(txn.get('Date', ''))
            if not date_obj:
                continue

            if (now - date_obj).days > 365:
                continue

            key = date_obj.strftime('%b %Y')
            if key not in months:
                months[key] = {'in': 0, 'out': 0}

            amount_in = float(txn.get('Amount In', 0) or 0)
            amount_out = float(txn.get('Amount Out', 0) or 0)

            months[key]['in'] += amount_in
            months[key]['out'] += amount_out
        except:
            continue

    sorted_months = sorted(months.items(), key=lambda x: datetime.strptime(x[0], '%b %Y'))

    return {
        'labels': [m[0] for m in sorted_months],
        'income': [round(m[1]['in'], 2) for m in sorted_months],
        'expenses': [round(m[1]['out'], 2) for m in sorted_months]
    }

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/dashboard')
def dashboard():
    return render_template('dashboard.html')

@app.route('/api/dashboard-data')
def api_dashboard_data():
    """API endpoint for dashboard data"""
    try:
        summary = get_this_month_summary()
        trend = get_12_month_trend()

        return jsonify({
            'success': True,
            'summary': summary,
            'trend': trend
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

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

def parse_ofx_content(ofx_text):
    """Parse OFX format and extract transactions"""
    transactions = []

    pattern = r'<STMTTRN>.*?</STMTTRN>'
    matches = re.finditer(pattern, ofx_text, re.DOTALL)

    for match in matches:
        stmttrn = match.group(0)

        trntype = extract_ofx_field(stmttrn, 'TRNTYPE')
        dtposted = extract_ofx_field(stmttrn, 'DTPOSTED')
        trnamt = extract_ofx_field(stmttrn, 'TRNAMT')
        name = extract_ofx_field(stmttrn, 'NAME')
        memo = extract_ofx_field(stmttrn, 'MEMO')

        if not (dtposted and trnamt and name):
            continue

        date_str = format_ofx_date(dtposted)

        try:
            amount = float(trnamt)
        except:
            continue

        amount_in = 0
        amount_out = 0
        if amount > 0:
            amount_in = amount
        else:
            amount_out = abs(amount)

        description = name
        if memo:
            description = f"{name} - {memo}"

        transactions.append({
            'date': date_str,
            'description': description[:100],
            'amountOut': round(amount_out, 2),
            'amountIn': round(amount_in, 2),
            'category': categorize_transaction(description),
            'keepUncategorized': False
        })

    return transactions

def extract_ofx_field(text, field_name):
    """Extract a field value from OFX text"""
    pattern = f'<{field_name}>([^<]+)</{field_name}>'
    match = re.search(pattern, text)
    return match.group(1).strip() if match else ''

def format_ofx_date(ofx_date):
    """Convert OFX date format (YYYYMMDD) to readable format"""
    try:
        dt = datetime.strptime(ofx_date, '%Y%m%d')
        return dt.strftime('%d %b %Y')
    except:
        return ofx_date

def categorize_transaction(description):
    """Categorize transaction based on description"""
    desc_upper = description.upper()

    categories = {
        'Groceries': ['TESCO', 'SAINSBURY', 'ASDA', 'MORRISONS', 'WAITROSE'],
        'Shopping': ['AMAZON', 'EBAY', 'ARGOS'],
        'Utilities': ['OCTOPUS', 'WATER', 'GAS', 'ELECTRIC'],
        'Entertainment': ['MONKEY BREWHOUSE', 'CINEMA', 'NETFLIX', 'SPOTIFY'],
        'Transport': ['UBER', 'TFL', 'LYMINGTON', 'PETROL', 'FUEL'],
        'Subscriptions': ['APPLE', 'MICROSOFT', 'PARAMOUNT', 'CRUNCHYROLL', 'NOW'],
        'Phone & Internet': ['EE', 'VODAFONE', 'O2', 'TROOLI', 'BT'],
        'Healthcare': ['SPECSAVERS', 'BOOTS', 'PHARMACY'],
        'Council Tax': ['FOREST DC', 'COUNCIL'],
    }

    for category, keywords in categories.items():
        for keyword in keywords:
            if keyword in desc_upper:
                return category

    return 'UNCATEGORIZED'

@app.route('/save-transactions', methods=['POST'])
def save_transactions():
    try:
        data = request.json
        transactions = data.get('transactions', [])

        if not transactions:
            return jsonify({'success': False, 'error': 'No transactions to save'})

        client = get_sheets_client()
        if not client:
            return jsonify({'success': False, 'error': 'Google Sheets authentication failed. Check GOOGLE_CREDENTIALS secret.'})

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

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
