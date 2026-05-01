# Household Financial Dashboard - Architecture & Design

## System Architecture

```
User Browser
    ↓
Web App (UI)
    ↓
Backend API (OFX Parser + Google Sheets)
    ↓
Google Sheets (Data Storage)
```

## Components

### 1. Frontend
- HTML/CSS/JavaScript
- File upload for OFX
- Transaction review table
- Category dropdown
- Dashboard views (charts/tables)

### 2. Backend
- Python Flask server
- OFX parsing logic
- Google Sheets API integration
- Data validation

### 3. Data Layer
- Google Sheets (single source of truth)
- Four tabs: Transactions, Payslips, Merchant Rules, Summary

## User Flows

### Upload & Categorize
1. User uploads OFX file
2. Backend parses transactions
3. Frontend displays review table
4. User reviews/categorizes each transaction
5. User clicks "Save"
6. Backend writes to Google Sheet

### View Dashboard
1. User opens dashboard
2. Frontend fetches data from Google Sheet
3. Display: income vs expenses, by category, by month
4. Show year-over-year trends
5. Display savings goals progress

### Manage Savings Goals
1. User clicks "Add Goal"
2. Enters: goal name, target amount, deadline
3. System calculates progress based on savings
4. Dashboard shows progress bar

## Data Flow

### Transaction Import
```
OFX File
    ↓
Parser (extract date, amount, merchant)
    ↓
Categorizer (match merchant to rule)
    ↓
Review UI (user can edit category)
    ↓
Google Sheet (append row to Transactions tab)
```

### Dashboard Generation
```
Google Sheet (Transactions tab)
    ↓
Aggregate by Category/Month/Year
    ↓
Calculate totals and trends
    ↓
Render charts/tables
```

## Technology Stack

### Option 1: Google Apps Script (Native to Google Sheets)
**Pros:**
- Direct Sheet access
- No external hosting needed
- Built into Google

**Cons:**
- Limited to Apps Script environment
- Slower OFX parsing

### Option 2: Python + Replit/Glitch (Recommended)
**Pros:**
- Real OFX libraries (ofxparse)
- Fast, reliable parsing
- Easy to deploy

**Cons:**
- Requires external hosting
- Need to manage Google API credentials

### Option 3: Google Cloud Functions
**Pros:**
- Serverless
- Google-native

**Cons:**
- More complex setup
- Cold start delays

## Recommended: Option 2 (Python + Replit)

**Deployment:**
1. Code on Replit
2. Public URL for users
3. Flask backend handles parsing
4. Google Sheets API for data storage

## Google Sheets Integration

### Authentication
- Service Account (for automated writes)
- OAuth (for user-initiated writes)

### Sheet Structure

**Transactions Tab:**
```
Date | Description | Amount Out | Amount In | Category | Month | Year | Source | Upload Date
```

**Payslips Tab:**
```
Date | Employer | Hours | Hourly Rate | Gross | Pension | Tax | NI | Net | Month | Year | Upload Date
```

**Merchant Rules Tab:**
```
Merchant Name | Category
```

**Summary Tab:**
```
(Auto-calculated)
Month | Total Income | Total Out | Net | By Category breakdown
```

## Dashboard Design

### Main View
```
┌─────────────────────────────────────┐
│  Household Financial Dashboard      │
├─────────────────────────────────────┤
│  This Month: £X in, £Y out, £Z net  │
├─────────────────────────────────────┤
│  Spending by Category (pie chart)   │
│  [Groceries] [Transport] [Utilities]│
├─────────────────────────────────────┤
│  Monthly Trend (line chart)         │
│  Jan Feb Mar Apr May Jun...         │
├─────────────────────────────────────┤
│  Savings Goals                      │
│  Emergency Fund: £5000 (60%)        │
│  Holiday Fund: £2000 (40%)          │
├─────────────────────────────────────┤
│  [Upload Statement] [View Payslips] │
└─────────────────────────────────────┘
```

### Upload Flow
```
┌─────────────────────────────────┐
│  Upload OFX Statement           │
├─────────────────────────────────┤
│  [Choose File] or [Paste Text]  │
│  [Parse]                        │
├─────────────────────────────────┤
│  Found 50 transactions          │
│  Date | Desc | Out | In | Cat   │
│  .... | .... | ... | .. | [dd]  │
│  [Save to Sheet] [Cancel]       │
└─────────────────────────────────┘
```

## Implementation Phases

### Phase 1: Core (MVP)
- OFX upload & parse
- Transaction review & save
- Basic transaction list view

### Phase 2: Dashboard
- Category breakdown (pie/bar chart)
- Monthly trends (line chart)
- Year-over-year comparison

### Phase 3: Payslips
- Payslip upload
- Auto-extract income data
- Compare projected vs actual

### Phase 4: Projections & Goals
- Savings goals tracking
- Monthly/yearly projections
- Budget vs actual

## Security & Privacy

- Google Sheets API credentials stored securely (environment variables)
- No sensitive data logged
- HTTPS only (enforced by hosting platform)
- Data owned by user (stored in their Google account)
