from flask import Flask, render_template, request, jsonify
import re
from datetime import datetime
import gspread
from google.oauth2.service_account import Credentials

app = Flask(__name__)

# Google Sheets setup
SCOPES = ['https://www.googleapis.com/auth/spreadsheets']
SHEET_ID = '1nnEwkIrvAQwIIQDnHBPAYumoTBv04wgOnq_R8A7xuIM'

def get_sheets_client():
    try:
        creds = Credentials.from_service_account_file('creds.json', scopes=SCOPES)
        return gspread.authorize(creds)
    except:
        return None

@app.route('/')
def index():
    return render_template('index.html')

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
            return jsonify({'success': False, 'error': 'Google Sheets authentication failed. Create creds.json file.'})

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