import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import pdfplumber
import re
import os
import io
import requests
import matplotlib.pyplot as plt
from datetime import datetime
from bs4 import BeautifulSoup

from reportlab.lib.pagesizes import letter, landscape
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image, KeepTogether, PageBreak
)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib import colors

# ============================================================
# 1. PAGE SETUP & GLOBALS
# ============================================================
st.set_page_config(
    page_title="MOD Strategic Intelligence",
    layout="wide",
    initial_sidebar_state="expanded"
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

ZONE_LABELS = [
    'Level 1: 0-5k MW (Base Load)',
    'Level 2: 5k-10k MW (Safe)',
    'Level 3: 10k-15k MW (Moderate Merit)',
    'Level 4: 15k-20k MW (High Merit)',
    'Level 5: 20k-25k MW (RSD Risk)',
    'Level 6: 25k-30k MW (High Curtailment)',
    'Level 7: >30k MW (Peaking/Emergency)'
]

# Reference ledger is used ONLY for two safe, non-destructive purposes:
#  (1) filling in a missing/blank capacity figure (e.g. "-" for gas peakers)
#  (2) normalising a station's display name across monthly filings
# It is NEVER used to override a rate that was actually parsed from the
# uploaded file. Rates always come from the file, not from this dict.
MOD_REFERENCE_CAPACITY = {
    "SSTPS-I Sipat": 510.0, "SSTPS-II Sipat": 258.0, "Lara": 230.0,
    "KSTPS-III Korba": 108.2, "KSTPS I AND II Korba": 610.0,
    "VSTP-IV Vindhyachal": 270.48, "VSTPS-III Vindhyachal": 258.0,
    "VSTP-II Vindhyachal": 319.0, "VSTPS-V Vindhyachal": 148.89,
    "VSTP-I Vindhyachal": 410.0, "RattanIndia Power Ltd, Amravati": 1200.0,
    "GMR-Warora NTPC": 200.0, "Jindal Power Limited,Tamnar (Interstate)": 250.0,
    "MSTPS-II Mauda": 500.2, "MSTPS-I Mauda": 370.48,
    "Jindal Power Ltd,Shirpur, Dhule to MSEDCL": 100.0,
    "SWPGL Unit 1234 -Sai Wardha": 240.0, "Gadarwara": 50.0,
    "KHTPS-II Kahlgaon": 148.0,
    "VIPL UNIT-1&2 Vidarbha Industries to MSEDCL (Powerpulse)": 543.0,
    "Khaperkheda Unit - 05": 500.0, "Koradi Unit - 08 to 10": 1980.0,
    "IEPL Case-IV Ideal Energy": 180.0, "Bhusawal Unit - 06": 660.0,
    "VMPL Vidarbha Minerals to MSEDCL Manikaran Power Ltd. (MPL))": 100.0,
    "Koradi Unit - 06": 210.0, "Khargone": 50.0,
    "Chandrapur Unit - 08,09": 1000.0, "Khaperkheda Unit - 01 to 04": 840.0,
    "APML, Unit 2 & 3 (PPA-1320 MW) Adani-Tiroda": 1320.0,
    "Paras Unit - 03 & 04": 500.0, "Bhusawal Unit - 04 & 05": 1000.0,
    "Chandrapur Unit - 03 to 07": 1920.0, "Parali Unit -08": 250.0,
    "Parali Unit - 06 & 07": 500.0,
    "APML, Unit 1,4 & 5 (PPA-125 MW) Adani-Tiroda": 125.0,
    "APML, Unit 1,4 & 5 (PPA-1200 MW) Adani-Tiroda": 1200.0,
    "Solapur STPS": 616.04, "APML, Unit 1,4 & 5 (PPA-440 MW)": 440.0,
    "JSW U1, Jaigad": 300.0, "CGPL Coastal Gujarat": 760.0,
    "Bhusawal Unit - 03": 210.0, "Nasik Unit - 03 to 05": 630.0,
    "Uran GTPS (Combined cycle operation)": 672.0,
    "JGPS (APM GAS)-Gandhar": 200.0, "KAWAS (APM GAS)": 204.0,
}

def classify_owner(station_name):
    """Classifies a station into MAHAGENCO, Central/ISGS, or Private IPP."""
    s = str(station_name).upper()
    mahagenco_kw = ['KHAPERKHEDA', 'KORADI', 'BHUSAWAL', 'CHANDRAPUR', 'PARAS', 'PARALI', 'PARLI', 'NASIK', 'URAN']
    isgs_kw = ['SIPAT', 'SSTPS', 'LARA', 'KORBA', 'KSTPS', 'VINDHYACHAL', 'VSTP', 'MAUDA', 'MSTPS',
               'GADARWARA', 'KAHLGAON', 'KHTPS', 'KHARGONE', 'SOLAPUR', 'GANDHAR', 'JGPS', 'KAWAS', 'NTPC']
    if any(k in s for k in mahagenco_kw):
        return "MAHAGENCO"
    if any(k in s for k in isgs_kw):
        return "Central / ISGS"
    return "Private IPP"

def get_dispatch_status(row, simulated_demand):
    """Returns tuple of (Status Label, Hex Color, Emoji)."""
    if simulated_demand <= row['MW_Ahead_In_Queue']:
        return "Critical / RSD", "#B71C1C", "🚨"
    elif simulated_demand <= row['Cumulative_MW']:
        return "Marginal", "#E65100", "⚠️"
    else:
        return "Safe", "#1B5E20", "✅"

# ============================================================
# 2. LIVE GRID SLDC DEMAND SCRAPER
# ============================================================
@st.cache_data(ttl=300)
def get_live_demand():
    try:
        url = "https://mahasldc.in/"
        response = requests.get(url, headers=HEADERS, verify=False, timeout=5)
        soup = BeautifulSoup(response.text, 'html.parser')
        text = soup.get_text()
        match = re.search(r'(\d+)\s*MW State Demand', text, re.IGNORECASE)
        if match:
            return int(match.group(1))
        return None
    except Exception:
        return None

# ============================================================
# 3. UNIVERSAL COLUMN DETECTION
# ============================================================
def _norm(s):
    return re.sub(r'\s+', ' ', str(s).strip().lower()) if s is not None else ""

def detect_header_columns(matrix, scan_rows=15, header_window=4):
    if not matrix:
        return None, None, None, None, 0

    scan_window = matrix[:scan_rows]
    ncols = max(len(r) for r in scan_window) if scan_window else 0

    header_anchor = None
    for r_idx, row in enumerate(scan_window):
        for cell in row:
            if 'generating station' in _norm(cell):
                header_anchor = r_idx
                break
        if header_anchor is not None:
            break

    if header_anchor is None:
        return None, None, None, None, 0

    window = matrix[header_anchor:header_anchor + header_window]
    combined = ["" for _ in range(ncols)]
    for row in window:
        for i in range(ncols):
            cell = row[i] if i < len(row) else None
            t = _norm(cell)
            if t and t != 'nan':
                combined[i] += " " + t

    def find(*keywords):
        for i, t in enumerate(combined):
            if all(k in t for k in keywords):
                return i
        return None

    station_col = find('generating station')
    capacity_col = find('installed capacity') or find('capacity')
    rate_col = find('total', 'variable charge')
    if rate_col is None:
        rate_col = find('approved', 'variable charge')
    if rate_col is None:
        nonempty = [i for i, t in enumerate(combined) if t.strip()]
        rate_col = nonempty[-1] if nonempty else ncols - 1

    discom_col = find('discom')

    return station_col, capacity_col, rate_col, discom_col, header_anchor

def detect_column_indices_fallback(matrix):
    col_scores = {i: {'floats': 0, 'text': 0, 'cap_ints': 0} for i in range(len(matrix[0]))}
    for row in matrix:
        for idx, cell in enumerate(row):
            if idx >= len(matrix[0]):
                continue
            cell_str = str(cell).strip()
            if not cell_str:
                continue
            col_scores[idx]['text'] += len(cell_str)
            if re.search(r'\b\d+\.\d{3,4}\b', cell_str):
                col_scores[idx]['floats'] += 1
            if '/' in cell_str or any(int(num) > 55 for num in re.findall(r'\b\d+\b', cell_str)):
                col_scores[idx]['cap_ints'] += 1
    vc_col = max(col_scores, key=lambda k: col_scores[k]['floats'])
    rem_cols = [k for k in col_scores if k != vc_col]
    st_col = max(rem_cols, key=lambda k: col_scores[k]['text'])
    cap_col = max([k for k in col_scores if k != vc_col and k != st_col], key=lambda k: col_scores[k]['cap_ints'])
    return st_col, cap_col, vc_col

def explode_raw_matrix(rows_list):
    normalized_rows = []
    for row in rows_list:
        if not row:
            continue
        cells_split = [str(cell).split('\n') if cell is not None else [""] for cell in row]
        max_splits = max(len(c) for c in cells_split)
        for c in cells_split:
            while len(c) < max_splits:
                c.append(c[-1] if len(c) == 1 else "")
        for i in range(max_splits):
            sub_row = [c[i].strip() for c in cells_split]
            normalized_rows.append(sub_row)
    return normalized_rows

# ============================================================
# 4. DATA INGESTION & HEALING
# ============================================================
def parse_capacity_cell(raw_cap, station_name):
    cap_str = "" if raw_cap is None else str(raw_cap).strip()
    if cap_str in ("", "-", "nan", "None"):
        return MOD_REFERENCE_CAPACITY.get(station_name, 0.0)
    if '/' in cap_str:
        cap_str = cap_str.split('/')[-1]
    cap_match = re.search(r'[\d.]+', cap_str)
    if cap_match:
        return float(cap_match.group())
    return MOD_REFERENCE_CAPACITY.get(station_name, 0.0)

def parse_rate_cell(raw_rate):
    if raw_rate is None:
        return None
    rate_str = str(raw_rate).strip()
    match = re.search(r'\d+\.\d+|\d+', rate_str)
    if not match:
        return None
    try:
        return float(match.group())
    except ValueError:
        return None

def normalize_name(name):
    clean = re.sub(r'[\s\-\(\)\.&_]', '', name.lower())
    for ref_key in MOD_REFERENCE_CAPACITY:
        ref_clean = re.sub(r'[\s\-\(\)\.&_]', '', ref_key.lower())
        if ref_clean == clean or (len(clean) > 4 and (ref_clean in clean or clean in ref_clean)):
            return ref_key
    return name

def enrich_dataframe(df):
    """Computes derived rankings, cumulative queues, owner types, and zones."""
    if df.empty:
        return df
    df = df.sort_values(by=['Total_VC', 'Capacity_MW'], ascending=[True, False]).reset_index(drop=True)
    df['MOD_Rank'] = df.index + 1
    df['Cumulative_MW'] = df['Capacity_MW'].cumsum()
    df['MW_Ahead_In_Queue'] = df['Cumulative_MW'] - df['Capacity_MW']
    df['Demand_Zone'] = pd.cut(
        df['Cumulative_MW'],
        bins=[0, 5000, 10000, 15000, 20000, 25000, 30000, float('inf')],
        labels=ZONE_LABELS
    )
    df['Owner_Group'] = df['Generating_Station'].apply(classify_owner)
    df['Step_Jump_Paise'] = (df['Total_VC'].diff().fillna(0.0) * 100).round(2)
    return df

def parse_and_heal_data(raw_rows, target_discom="MSEDCL", is_pdf_source=False):
    if not raw_rows:
        return pd.DataFrame()

    matrix = explode_raw_matrix(raw_rows) if is_pdf_source else [
        [("" if c is None else c) for c in row] for row in raw_rows
    ]

    station_col, capacity_col, rate_col, discom_col, header_row = detect_header_columns(matrix)
    if station_col is None or rate_col is None:
        station_col, capacity_col, rate_col = detect_column_indices_fallback(matrix)
        discom_col, header_row = None, 0

    processed_data = []
    current_section = target_discom
    section_locked = discom_col is not None

    for row_idx, row in enumerate(matrix[header_row + 1:], start=header_row + 1):
        if rate_col >= len(row) or station_col >= len(row):
            continue

        station_raw = str(row[station_col]).strip()
        row_text_all = " ".join(str(c) for c in row if c not in (None, "")).lower()

        if not section_locked and 'mod stack for' in row_text_all:
            m = re.search(r'mod stack for\s+([a-z0-9\-]+)', row_text_all)
            if m:
                current_section = m.group(1).upper()
            continue

        if not station_raw or station_raw.lower() in ("nan", "none") or len(station_raw) <= 2:
            continue
        if any(x in station_raw.upper() for x in ['TOTAL', 'GENERATING STATION', 'OWNER TYPE', 'DISCOM', 'NOTE', 'MOD STACK']):
            continue

        parsed_rate = parse_rate_cell(row[rate_col])
        if parsed_rate is None:
            continue

        if section_locked:
            discom_val = str(row[discom_col]).strip().upper() if discom_col < len(row) else ""
            if target_discom and discom_val and target_discom.upper() not in discom_val:
                continue
        else:
            if target_discom and target_discom.upper() not in current_section:
                continue

        final_name = normalize_name(station_raw)
        raw_cap = row[capacity_col] if capacity_col is not None and capacity_col < len(row) else None
        final_cap = parse_capacity_cell(raw_cap, final_name)

        processed_data.append({
            'Generating_Station': final_name,
            'Capacity_MW': final_cap,
            'Total_VC': parsed_rate,
        })

    df = pd.DataFrame(processed_data).drop_duplicates(subset=['Generating_Station', 'Total_VC'])
    if df.empty:
        return df
    return enrich_dataframe(df)

def get_available_discoms(raw_rows, is_pdf_source=False):
    matrix = explode_raw_matrix(raw_rows) if is_pdf_source else [
        [("" if c is None else c) for c in row] for row in raw_rows
    ]
    station_col, capacity_col, rate_col, discom_col, header_row = detect_header_columns(matrix)
    found = set()
    if discom_col is not None:
        for row in matrix[header_row + 1:]:
            if discom_col < len(row):
                v = str(row[discom_col]).strip().upper()
                if v and v not in ("NAN", "NONE"):
                    found.add(v)
    else:
        for row in matrix:
            row_text = " ".join(str(c) for c in row if c not in (None, "")).lower()
            m = re.search(r'mod stack for\s+([a-z0-9\-]+)', row_text)
            if m:
                found.add(m.group(1).upper())
    return sorted(found) if found else ["MSEDCL"]

# ============================================================
# 5. MULTI-TAB PDF REPORT ENGINES (TABS 1, 2, 3, AND 4)
# ============================================================
def _get_pdf_styles():
    return {
        'title': ParagraphStyle('TText', fontName='Helvetica-Bold', fontSize=15, textColor=colors.white, alignment=1),
        'subtitle': ParagraphStyle('SText', fontName='Helvetica', fontSize=9, textColor=colors.HexColor('#D9E1F2'), alignment=1),
        'sec_title': ParagraphStyle('SecT', fontName='Helvetica-Bold', fontSize=11, textColor=colors.HexColor('#1F4E78'), spaceBefore=8, spaceAfter=4),
        'body': ParagraphStyle('BStyle', fontName='Helvetica', fontSize=9.5, leading=13, textColor=colors.HexColor('#262626')),
        'kpi_title': ParagraphStyle('KTitle', fontName='Helvetica-Bold', fontSize=8, textColor=colors.HexColor('#595959'), alignment=1),
        'kpi_val': ParagraphStyle('KVal', fontName='Helvetica-Bold', fontSize=13, textColor=colors.HexColor('#1F4E78'), alignment=1),
        'footer': ParagraphStyle('FStyle', fontName='Helvetica-Oblique', fontSize=7.5, textColor=colors.HexColor('#718096'), alignment=1)
    }

def _build_pdf_banner(title_text, subtitle_text, width=540):
    styles = _get_pdf_styles()
    header_data = [
        [Paragraph(title_text, styles['title'])],
        [Paragraph(subtitle_text, styles['subtitle'])]
    ]
    header_table = Table(header_data, colWidths=[width])
    header_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#1F4E78')),
        ('TOPPADDING', (0, 0), (-1, -1), 9),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 9)
    ]))
    return header_table

# --- TAB 1 PDF: DEEP DIVE + +/- 5 NEIGHBORHOOD ---
def generate_pdf_report(plant_name, df, simulated_demand):
    plant_data = df[df['Generating_Station'] == plant_name].iloc[0]
    styles = _get_pdf_styles()
    buffer = io.BytesIO()

    doc = SimpleDocTemplate(buffer, pagesize=letter, rightMargin=36, leftMargin=36, topMargin=30, bottomMargin=30)
    story = []

    story.append(_build_pdf_banner(
        "MERIT ORDER DISPATCH (MOD) STRATEGIC BRIEF",
        f"Unit Deep-Dive & Neighborhood Proximity Analysis | Generated: {datetime.now().strftime('%d %b %Y %H:%M')}"
    ))
    story.append(Spacer(1, 10))

    kpi_cells = [
        [Paragraph("GRID MERIT RANK", styles['kpi_title']),
         Paragraph("VARIABLE COST RATE", styles['kpi_title']),
         Paragraph("NET METRIC CAPACITY", styles['kpi_title']),
         Paragraph("TECH MINIMUM (55% MTL)", styles['kpi_title'])],
        [Paragraph(f"#{int(plant_data['MOD_Rank'])} of {len(df)}", styles['kpi_val']),
         Paragraph(f"Rs. {plant_data['Total_VC']:.4f}/kWh", styles['kpi_val']),
         Paragraph(f"{plant_data['Capacity_MW']:.1f} MW", styles['kpi_val']),
         Paragraph(f"{plant_data['Capacity_MW'] * 0.55:.1f} MW", styles['kpi_val'])]
    ]
    kpi_table = Table(kpi_cells, colWidths=[135, 135, 135, 135])
    kpi_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#F2F5F9')),
        ('BOX', (0, 0), (-1, -1), 1, colors.HexColor('#D9E1F2')),
        ('INNERGRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#D9E1F2')),
        ('TOPPADDING', (0, 0), (-1, -1), 5),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 5)
    ]))
    story.append(kpi_table)
    story.append(Spacer(1, 10))

    status, status_color, status_desc = (
        "SAFE DESPATCH PROFILE", "#1B5E20",
        "The asset safely clears the lower-cost backlog queue and runs continuously under standard grid parameters."
    )
    if simulated_demand <= plant_data['MW_Ahead_In_Queue']:
        status, status_color, status_desc = (
            "CRITICAL RSD / CURTAILED BOUNDARY", "#B71C1C",
            "System loading fails to clear the economic baseline required for this asset. High risk of forced Reserve Shut Down (RSD)."
        )
    elif simulated_demand <= plant_data['Cumulative_MW']:
        status, status_color, status_desc = (
            "SYSTEM MARGINAL BALANCING NODE", "#E65100",
            "Asset sets the current clearing price threshold. Unit subject to cyclical regulatory load adjustments."
        )

    analysis_paragraph = Paragraph(
        f"<b>Operational Assessment Summary:</b> Target asset <b>{plant_name}</b> ({plant_data.get('Owner_Group', 'GenCo')}) "
        f"is evaluated under a system demand threshold of <b>{simulated_demand:,} MW</b>. "
        f"The unit maintains a <font color='{status_color}'><b>{status}</b></font> posture. {status_desc}",
        styles['body']
    )

    matrix_data = [
        ["Operational Parameter Attribute", "Value Mapping Metrics"],
        ["Lower-Cost Supply Backlog Queue", f"{plant_data['MW_Ahead_In_Queue']:,.1f} MW"],
        ["Cumulative Grid Anchor Clearance", f"{plant_data['Cumulative_MW']:,.1f} MW"],
        ["Margin vs. Simulated Demand", f"{simulated_demand - plant_data['Cumulative_MW']:+,.1f} MW"],
        ["Grid Loading Dispatch Category", str(plant_data['Demand_Zone']).split(':')[-1].strip()]
    ]
    matrix_table = Table(matrix_data, colWidths=[220, 320])
    matrix_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#4A5568')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 8.5),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#E2E8F0')),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#F7FAFC')])
    ]))

    story.append(KeepTogether([
        Paragraph("1. System Dispatch Assessment Matrix", styles['sec_title']),
        analysis_paragraph, Spacer(1, 5), matrix_table
    ]))
    story.append(Spacer(1, 8))

    # Supply Stack Step Chart
    x_steps = [0] + list(df['Cumulative_MW'])
    y_steps = list(df['Total_VC']) + [list(df['Total_VC'])[-1]]

    fig, ax = plt.subplots(figsize=(6.5, 2.1), facecolor='#F8FAFC')
    ax.set_facecolor('#FFFFFF')
    ax.step(x_steps, y_steps, where='post', color='#1F4E78', linewidth=1.8, label='MOD Supply Stack')
    ax.axvspan(plant_data['MW_Ahead_In_Queue'], plant_data['Cumulative_MW'], color='#E53E3E', alpha=0.4, label='Target Unit')
    ax.axvline(x=simulated_demand, color='#D69E2E', linestyle='--', linewidth=1.5, label=f'Demand ({simulated_demand:,} MW)')
    ax.set_title("Economic Merit Order Step Curve", fontsize=8.5, fontweight='bold')
    ax.set_xlabel("Cumulative System Supply Footprint (MW)", fontsize=7.5)
    ax.set_ylabel("Variable Charge (Rs./kWh)", fontsize=7.5)
    ax.grid(True, linestyle=':', alpha=0.5)
    ax.legend(fontsize=7, loc='upper left')
    plt.tight_layout()

    chart_buf = io.BytesIO()
    plt.savefig(chart_buf, format='png', dpi=250)
    chart_buf.seek(0)
    plt.close(fig)

    story.append(KeepTogether([
        Paragraph("2. Graphical Supply Stack Curve Analysis", styles['sec_title']),
        Spacer(1, 3), Image(chart_buf, width=540, height=175)
    ]))
    story.append(Spacer(1, 8))

    # +/- 5 Neighbouring Plants Table
    target_idx = int(plant_data['MOD_Rank']) - 1
    start_idx = max(0, target_idx - 5)
    end_idx = min(len(df), target_idx + 6)
    neigh_df = df.iloc[start_idx:end_idx].copy()

    neigh_rows = [["Pos", "Rank", "Generating Station", "Cap (MW)", "VC (Rs./kWh)", "Gap (Paise)", "Cum. MW"]]
    target_row_num = 1
    for idx_i, (_, r) in enumerate(neigh_df.iterrows(), start=1):
        rel = int(r['MOD_Rank']) - int(plant_data['MOD_Rank'])
        pos_str = "TARGET" if rel == 0 else (f"{rel:+d} Above" if rel < 0 else f"{rel:+d} Below")
        if rel == 0:
            target_row_num = idx_i
        gap_paise = (r['Total_VC'] - plant_data['Total_VC']) * 100.0
        neigh_rows.append([
            pos_str,
            f"#{int(r['MOD_Rank'])}",
            str(r['Generating_Station'])[:36],
            f"{r['Capacity_MW']:,.1f}",
            f"{r['Total_VC']:.4f}",
            f"{gap_paise:+.2f} p",
            f"{r['Cumulative_MW']:,.0f}"
        ])

    neigh_table = Table(neigh_rows, colWidths=[55, 38, 195, 58, 68, 64, 62])
    t_style = [
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#1F4E78')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 7.5),
        ('GRID', (0, 0), (-1, -1), 0.4, colors.HexColor('#CBD5E0')),
        ('ALIGN', (3, 1), (-1, -1), 'RIGHT'),
        ('TOPPADDING', (0, 0), (-1, -1), 3),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#F7FAFC')]),
        ('BACKGROUND', (0, target_row_num), (-1, target_row_num), colors.HexColor('#FFF5F5')),
        ('TEXTCOLOR', (0, target_row_num), (-1, target_row_num), colors.HexColor('#9B2C2C')),
        ('FONTNAME', (0, target_row_num), (-1, target_row_num), 'Helvetica-Bold'),
    ]
    neigh_table.setStyle(TableStyle(t_style))

    story.append(KeepTogether([
        Paragraph("3. Competitive Neighborhood Window (5 Units Above & 5 Units Below)", styles['sec_title']),
        Spacer(1, 3), neigh_table
    ]))

    doc.build(story)
    buffer.seek(0)
    return buffer.getvalue()

# --- TAB 2 PDF: FLASHCARD STATUS BRIEF ---
def generate_flashcards_pdf(card_df, full_df_len, simulated_demand, status_filter, name_filter):
    styles = _get_pdf_styles()
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter, rightMargin=36, leftMargin=36, topMargin=30, bottomMargin=30)
    story = []

    story.append(_build_pdf_banner(
        "MOD FLASHCARD DISPATCH STATUS BRIEF",
        f"Simulated Demand: {simulated_demand:,} MW | Filter: {status_filter} | Generated: {datetime.now().strftime('%d %b %Y %H:%M')}"
    ))
    story.append(Spacer(1, 10))

    safe_mw = card_df[card_df['Cumulative_MW'] < simulated_demand]['Capacity_MW'].sum()
    rsd_mw = card_df[card_df['MW_Ahead_In_Queue'] >= simulated_demand]['Capacity_MW'].sum()
    marg_mw = card_df['Capacity_MW'].sum() - safe_mw - rsd_mw

    kpi_cells = [
        [Paragraph("FILTERED STATIONS", styles['kpi_title']),
         Paragraph("SAFE DISPATCH MW", styles['kpi_title']),
         Paragraph("MARGINAL MW", styles['kpi_title']),
         Paragraph("CRITICAL / RSD MW", styles['kpi_title'])],
        [Paragraph(f"{len(card_df)} of {full_df_len}", styles['kpi_val']),
         Paragraph(f"{safe_mw:,.1f} MW", styles['kpi_val']),
         Paragraph(f"{marg_mw:,.1f} MW", styles['kpi_val']),
         Paragraph(f"{rsd_mw:,.1f} MW", styles['kpi_val'])]
    ]
    kpi_table = Table(kpi_cells, colWidths=[135, 135, 135, 135])
    kpi_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#F2F5F9')),
        ('BOX', (0, 0), (-1, -1), 1, colors.HexColor('#D9E1F2')),
        ('INNERGRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#D9E1F2')),
        ('TOPPADDING', (0, 0), (-1, -1), 6),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6)
    ]))
    story.append(kpi_table)
    story.append(Spacer(1, 12))

    rows = [["Rank", "Generating Station", "Portfolio", "Capacity (MW)", "VC (Rs./kWh)", "Cum. MW", "Dispatch Status"]]
    row_styles = [
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#1F4E78')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 8),
        ('GRID', (0, 0), (-1, -1), 0.4, colors.HexColor('#CBD5E0')),
        ('ALIGN', (3, 1), (5, -1), 'RIGHT'),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
    ]

    for idx, (_, r) in enumerate(card_df.iterrows(), start=1):
        label, color_hex, _ = get_dispatch_status(r, simulated_demand)
        rows.append([
            f"#{int(r['MOD_Rank'])}",
            str(r['Generating_Station'])[:34],
            str(r.get('Owner_Group', 'GenCo')),
            f"{r['Capacity_MW']:,.1f}",
            f"{r['Total_VC']:.4f}",
            f"{r['Cumulative_MW']:,.0f}",
            label
        ])
        row_styles.append(('TEXTCOLOR', (6, idx), (6, idx), colors.HexColor(color_hex)))
        row_styles.append(('FONTNAME', (6, idx), (6, idx), 'Helvetica-Bold'))

    t = Table(rows, colWidths=[38, 175, 75, 62, 65, 58, 67], repeatRows=1)
    row_styles.append(('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#F7FAFC')]))
    t.setStyle(TableStyle(row_styles))
    story.append(t)

    doc.build(story)
    buffer.seek(0)
    return buffer.getvalue()

# --- TAB 3 PDF: COMPLETE AUDITABLE LEDGER ---
def generate_ledger_pdf(df, discom_choice, simulated_demand):
    styles = _get_pdf_styles()
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=landscape(letter), rightMargin=28, leftMargin=28, topMargin=28, bottomMargin=28)
    story = []

    story.append(_build_pdf_banner(
        f"OFFICIAL AUDITABLE MOD STACK LEDGER ({discom_choice})",
        f"Complete Extracted Merit Order Verification | Total Capacity: {df['Capacity_MW'].sum():,.1f} MW | Demand: {simulated_demand:,} MW",
        width=736
    ))
    story.append(Spacer(1, 10))

    headers = ["Rank", "Generating Station", "Owner Group", "Cap (MW)", "Total VC (Rs./kWh)", "Step Jump (p)", "Queue Ahead (MW)", "Cum. MW", "Demand Zone"]
    rows = [headers]

    for _, r in df.iterrows():
        rows.append([
            f"#{int(r['MOD_Rank'])}",
            str(r['Generating_Station'])[:38],
            str(r.get('Owner_Group', '')),
            f"{r['Capacity_MW']:,.2f}",
            f"{r['Total_VC']:.4f}",
            f"{r.get('Step_Jump_Paise', 0.0):+.2f}",
            f"{r['MW_Ahead_In_Queue']:,.1f}",
            f"{r['Cumulative_MW']:,.1f}",
            str(r['Demand_Zone'])
        ])

    ledger_table = Table(rows, colWidths=[35, 185, 75, 55, 78, 58, 72, 62, 116], repeatRows=1)
    ledger_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#1F4E78')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 7.5),
        ('GRID', (0, 0), (-1, -1), 0.4, colors.HexColor('#CBD5E0')),
        ('ALIGN', (3, 1), (7, -1), 'RIGHT'),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#F8FAFC')]),
        ('TOPPADDING', (0, 0), (-1, -1), 3.5),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 3.5),
    ]))
    story.append(ledger_table)
    story.append(Spacer(1, 8))
    story.append(Paragraph(
        f"Audit Certification: Rates extracted directly from uploaded SLDC/MSEDCL filing without rate override. Generated on {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}.",
        styles['footer']
    ))

    doc.build(story)
    buffer.seek(0)
    return buffer.getvalue()

# --- TAB 4 PDF: MACRO SYSTEM LOADING ZONES REPORT ---
def generate_zones_pdf(df, zone_stats_df, owner_stats_df, simulated_demand):
    styles = _get_pdf_styles()
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter, rightMargin=36, leftMargin=36, topMargin=30, bottomMargin=30)
    story = []

    story.append(_build_pdf_banner(
        "MACRO SYSTEM LOADING ZONES & PORTFOLIO REPORT",
        f"5,000 MW Merit Interval Analysis & Ownership Stack Distribution | Simulated Demand: {simulated_demand:,} MW"
    ))
    story.append(Spacer(1, 10))

    # Matplotlib Bar Chart for Zones
    fig, ax = plt.subplots(figsize=(6.5, 2.3), facecolor='#F8FAFC')
    ax.set_facecolor('#FFFFFF')
    short_labels = [str(z).split(':')[0] + "\n" + str(z).split('(')[-1].replace(')', '') for z in zone_stats_df['Demand_Zone']]
    bars = ax.bar(short_labels, zone_stats_df['Total_MW'], color='#2B6CB0', edgecolor='#1A365D', width=0.55)
    for bar in bars:
        h = bar.get_height()
        ax.text(bar.get_x() + bar.get_width() / 2.0, h + 120, f"{h:,.0f}", ha='center', va='bottom', fontsize=7, fontweight='bold')
    ax.set_title("Aggregated Capacity Blocks per 5,000 MW Demand Interval", fontsize=9, fontweight='bold')
    ax.set_ylabel("Capacity (MW)", fontsize=8)
    ax.set_ylim(0, max(zone_stats_df['Total_MW'].max() * 1.18, 1000))
    ax.tick_params(axis='x', labelsize=6.8)
    ax.grid(axis='y', linestyle=':', alpha=0.5)
    plt.tight_layout()

    chart_buf = io.BytesIO()
    plt.savefig(chart_buf, format='png', dpi=250)
    chart_buf.seek(0)
    plt.close(fig)

    story.append(Paragraph("1. System Capacity Distribution Across 5,000 MW Demand Zones", styles['sec_title']))
    story.append(Image(chart_buf, width=540, height=190))
    story.append(Spacer(1, 8))

    z_rows = [["Demand Zone Block", "Units", "Block Cap (MW)", "Weighted Avg VC", "Min VC", "Max VC"]]
    for _, r in zone_stats_df.iterrows():
        z_rows.append([
            str(r['Demand_Zone']),
            f"{int(r['Station_Count'])}",
            f"{r['Total_MW']:,.1f} MW",
            f"Rs. {r['Weighted_Avg_VC']:.4f}",
            f"Rs. {r['Min_VC']:.4f}",
            f"Rs. {r['Max_VC']:.4f}"
        ])
    z_table = Table(z_rows, colWidths=[190, 45, 85, 80, 70, 70])
    z_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#1F4E78')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 8),
        ('GRID', (0, 0), (-1, -1), 0.4, colors.HexColor('#CBD5E0')),
        ('ALIGN', (1, 1), (-1, -1), 'RIGHT'),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#F7FAFC')]),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
    ]))
    story.append(Paragraph("2. Zone-Wise Tariff & Capacity Breakdown Matrix", styles['sec_title']))
    story.append(z_table)
    story.append(Spacer(1, 10))

    o_rows = [["Ownership Portfolio Group", "Stations", "Total Capacity (MW)", "Stack Share (%)", "Weighted Avg VC"]]
    for _, r in owner_stats_df.iterrows():
        o_rows.append([
            str(r['Owner_Group']),
            f"{int(r['Stations'])}",
            f"{r['Total_MW']:,.1f} MW",
            f"{r['Share_Pct']:.1f}%",
            f"Rs. {r['Weighted_Avg_VC']:.4f}/kWh"
        ])
    o_table = Table(o_rows, colWidths=[170, 65, 110, 95, 100])
    o_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#4A5568')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 8),
        ('GRID', (0, 0), (-1, -1), 0.4, colors.HexColor('#CBD5E0')),
        ('ALIGN', (1, 1), (-1, -1), 'RIGHT'),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#F7FAFC')]),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
    ]))
    story.append(Paragraph("3. Utility Ownership Portfolio Comparison (MAHAGENCO vs. ISGS vs. IPP)", styles['sec_title']))
    story.append(o_table)

    doc.build(story)
    buffer.seek(0)
    return buffer.getvalue()

# ============================================================
# 6. DATA HANDLING LAYER
# ============================================================
DATA_FILE = "saved_mod_stack.csv"
df = pd.DataFrame()

with st.sidebar:
    st.header("⚙️ Data Source Management")
    uploaded_file = st.file_uploader("Upload raw MOD Stack (PDF or Excel)", type=["pdf", "xlsx"])
    discom_choice = st.session_state.get("discom_choice", "MSEDCL")

if uploaded_file is not None:
    file_ext = uploaded_file.name.lower()
    is_pdf = file_ext.endswith('.pdf')

    if is_pdf:
        with pdfplumber.open(uploaded_file) as pdf:
            raw_rows = []
            for page in pdf.pages:
                tables = page.extract_tables()
                if tables:
                    for table in tables:
                        raw_rows.extend(table)
    else:
        raw_excel = pd.read_excel(uploaded_file, header=None)
        raw_rows = raw_excel.values.tolist()

    available_discoms = get_available_discoms(raw_rows, is_pdf_source=is_pdf)
    with st.sidebar:
        discom_choice = st.selectbox(
            "DISCOM / Section to isolate:",
            available_discoms,
            index=available_discoms.index("MSEDCL") if "MSEDCL" in available_discoms else 0
        )
        st.session_state["discom_choice"] = discom_choice

    df = parse_and_heal_data(raw_rows, target_discom=discom_choice, is_pdf_source=is_pdf)

    if not df.empty:
        df.to_csv(DATA_FILE, index=False)
        st.sidebar.success(f"✅ Isolated {discom_choice} Stack: {len(df)} stations verified, rates read directly from the file's rate column.")
    else:
        st.sidebar.error("⚠️ No rows parsed. Check that the file has a 'Generating Station' and a 'Variable Charge' header within the first 10 rows.")
elif os.path.exists(DATA_FILE):
    df = pd.read_csv(DATA_FILE)
    df = enrich_dataframe(df)
    df['Demand_Zone'] = pd.Categorical(df['Demand_Zone'], categories=ZONE_LABELS, ordered=True)
else:
    st.sidebar.info("📂 Upload a MOD stack file (PDF or Excel) to begin. No cached data found.")

# ============================================================
# 7. DASHBOARD WORKSPACE
# ============================================================
st.title("⚡ MOD Grid Strategy & Risk Dashboard")

if not df.empty:
    live_demand = get_live_demand()

    # Determine System Marginal Clearing Plant at current demand
    clearing_rows = df[df['Cumulative_MW'] >= (live_demand if live_demand else 20000)]
    clearing_vc = clearing_rows.iloc[0]['Total_VC'] if not clearing_rows.empty else df['Total_VC'].max()

    col_kpi1, col_kpi2, col_kpi3, col_kpi4 = st.columns(4)
    col_kpi1.metric("Total Isolated Capacity", f"{df['Capacity_MW'].sum():,.1f} MW")
    col_kpi2.metric("Cheapest Baseload VC", f"₹{df['Total_VC'].min():.4f}/kWh")
    col_kpi3.metric("Most Expensive Peak VC", f"₹{df['Total_VC'].max():.4f}/kWh")
    col_kpi4.metric("Isolated Plants", f"{len(df)}")

    st.markdown("---")

    st.subheader("Grid Load Simulation Controls")
    if live_demand:
        st.success(f"📡 Real-Time Grid Sync Connected: **{live_demand:,.0f} MW State Demand**")
        simulated_demand = st.slider(
            "Adjust State Demand (MW) for Operational Test Simulation:",
            min_value=1000, max_value=35000, value=live_demand, step=100
        )
    else:
        simulated_demand = st.slider(
            "Simulate Total State Grid Demand (MW):",
            min_value=1000, max_value=35000, value=20000, step=100
        )

    # Show Live System Marginal Clearing Node banner
    marginal_matches = df[(df['MW_Ahead_In_Queue'] < simulated_demand) & (df['Cumulative_MW'] >= simulated_demand)]
    if not marginal_matches.empty:
        m_row = marginal_matches.iloc[0]
        st.caption(
            f"🧭 **System Marginal Clearing Node at {simulated_demand:,} MW:** "
            f"**{m_row['Generating_Station']}** (Rank #{int(m_row['MOD_Rank'])} | "
            f"Clearing VC: **₹{m_row['Total_VC']:.4f}/kWh** | Queue Span: {m_row['MW_Ahead_In_Queue']:,.0f}–{m_row['Cumulative_MW']:,.0f} MW)"
        )

    st.markdown("---")

    tab1, tab2, tab3, tab4 = st.tabs([
        "🎯 Plant Deep Dive & Report Center",
        "🗂️ Merit Order Flashcards",
        "📋 Auditable Extracted Data Ledger",
        "📊 Macro System Loading Zones",
    ])

    # ============================================================
    # TAB 1: PLANT DEEP DIVE & NEIGHBORHOOD ANALYSIS
    # ============================================================
    with tab1:
        search_match = df.index[df['Generating_Station'].str.contains('Parali', case=False, na=False)].tolist()
        default_idx = int(search_match[0]) if search_match else 0

        top_col1, top_col2 = st.columns([2.2, 1.3])
        with top_col1:
            selected_plant = st.selectbox(
                "Select Target Unit for Deep-Dive Analysis:",
                df['Generating_Station'].unique(),
                index=default_idx
            )
        plant_data = df[df['Generating_Station'] == selected_plant].iloc[0]

        with top_col2:
            st.markdown("<div style='height: 28px;'></div>", unsafe_allow_html=True)
            pdf_bytes_tab1 = generate_pdf_report(selected_plant, df, simulated_demand)
            st.download_button(
                label=f"📥 Download Strategic PDF Report ({selected_plant[:22]})",
                data=pdf_bytes_tab1,
                file_name=f"MOD_Executive_Report_{selected_plant.replace(' ', '_')}.pdf",
                mime="application/pdf",
                use_container_width=True
            )

        # Primary KPI Row
        sc1, sc2, sc3, sc4, sc5 = st.columns(5)
        sc1.metric("Economic Merit Rank", f"#{int(plant_data['MOD_Rank'])} of {len(df)}")
        sc2.metric("Variable Charge (VC)", f"₹{plant_data['Total_VC']:.4f}/kWh")
        sc3.metric("Cheaper MW Ahead", f"{plant_data['MW_Ahead_In_Queue']:,.1f} MW")
        sc4.metric("55% Tech Min / Full Cap", f"{plant_data['Capacity_MW']*0.55:.0f} / {plant_data['Capacity_MW']:.0f} MW")
        sc5.metric("Grid Merit Block", str(plant_data['Demand_Zone']).split(' (')[0])

        # Dispatch Risk Banner
        if simulated_demand <= plant_data['MW_Ahead_In_Queue']:
            mw_deficit = plant_data['MW_Ahead_In_Queue'] - simulated_demand
            st.error(
                f"🚨 **CRITICAL RISK (UNSCHEDULED / RSD)**: System demand profile ({simulated_demand:,} MW) is below this unit's queue entry point. "
                f"Blocked out by **{mw_deficit:,.1f} MW** of cheaper supply. Expected Status: **Reserve Shut Down (RSD)**."
            )
        elif simulated_demand <= plant_data['Cumulative_MW']:
            dispatched_mw = simulated_demand - plant_data['MW_Ahead_In_Queue']
            tech_min_mw = plant_data['Capacity_MW'] * 0.55
            st.warning(
                f"⚠️ **MARGINAL DISPATCH BOUNDARY**: This plant is the active clearing node! "
                f"Uncovered Grid Need: **{dispatched_mw:,.1f} MW** (55% Technical Minimum is **{tech_min_mw:,.1f} MW**)."
            )
        else:
            mw_buffer = simulated_demand - plant_data['Cumulative_MW']
            st.success(
                f"✅ **SAFE DESPATCH OPERATION**: System demand clears this unit's full capacity with a safety buffer of "
                f"**+{mw_buffer:,.1f} MW** above its cumulative anchor ({plant_data['Cumulative_MW']:,.1f} MW)."
            )

        # Main Supply Stack Bar Chart
        bar_colors = ['#ff4b4b' if name == selected_plant else 'rgba(100, 110, 130, 0.35)' for name in df['Generating_Station']]
        fig = go.Figure()
        fig.add_trace(go.Bar(
            x=df['Cumulative_MW'] - (df['Capacity_MW'] / 2),
            y=df['Total_VC'],
            width=df['Capacity_MW'],
            marker_color=bar_colors,
            marker_line_width=0,
            text=df['Generating_Station'],
            hovertemplate="<b>%{text}</b><br>Variable Cost: ₹%{y:.4f}/kWh<br>Cumulative Queue: %{customdata:.0f} MW<extra></extra>",
            customdata=df['Cumulative_MW']
        ))
        fig.add_vline(x=simulated_demand, line_dash="solid", line_color="#D69E2E", annotation_text="Simulated Load Boundary", annotation_position="top left")
        fig.add_vline(x=plant_data['Cumulative_MW'], line_dash="dash", line_color="#E53E3E", annotation_text="Unit Target Anchor", annotation_position="bottom right")
        fig.update_layout(
            xaxis_title="Cumulative Supply Footprint Matrix (MW)",
            yaxis_title="Variable Charge Fee (₹/kWh)",
            template="plotly_dark",
            bargap=0,
            height=430
        )
        st.plotly_chart(fig, use_container_width=True)

        st.markdown("---")

        # ------------------------------------------------------------
        # NEW FEATURE 1: +/- 5 NEIGHBORHOOD PROXIMITY WINDOW
        # ------------------------------------------------------------
        st.subheader(f"🔭 Competitive Neighborhood Window: 5 Plants Above & 5 Plants Below ({selected_plant})")
        st.caption(
            "Direct comparison against the 5 immediately cheaper units ahead in the queue (▲ Above) "
            "and the 5 immediately costlier units behind in the queue (▼ Below)."
        )

        target_rank_idx = int(plant_data['MOD_Rank']) - 1
        start_idx = max(0, target_rank_idx - 5)
        end_idx = min(len(df), target_rank_idx + 6)
        neigh_df = df.iloc[start_idx:end_idx].copy()

        def _format_rel_pos(r):
            diff = int(r['MOD_Rank']) - int(plant_data['MOD_Rank'])
            if diff == 0:
                return "🎯 TARGET UNIT"
            elif diff < 0:
                return f"▲ {diff} (Cheaper Ahead)"
            else:
                return f"▼ +{diff} (Costlier Behind)"

        neigh_df['Queue_Position'] = neigh_df.apply(_format_rel_pos, axis=1)
        neigh_df['VC_Diff_Rs'] = (neigh_df['Total_VC'] - plant_data['Total_VC']).round(4)
        neigh_df['Spread_Paise'] = ((neigh_df['Total_VC'] - plant_data['Total_VC']) * 100).round(2)
        neigh_df['Status_At_Load'] = neigh_df.apply(lambda r: f"{get_dispatch_status(r, simulated_demand)[2]} {get_dispatch_status(r, simulated_demand)[0]}", axis=1)

        ncol1, ncol2 = st.columns([1.45, 1.05])
        with ncol1:
            display_neigh = neigh_df[[
                'Queue_Position', 'MOD_Rank', 'Generating_Station', 'Owner_Group',
                'Capacity_MW', 'Total_VC', 'Spread_Paise', 'Cumulative_MW', 'Status_At_Load'
            ]].rename(columns={
                'Queue_Position': 'Relative Position',
                'MOD_Rank': 'Rank',
                'Generating_Station': 'Station',
                'Owner_Group': 'Portfolio',
                'Capacity_MW': 'Cap (MW)',
                'Total_VC': 'VC (₹/kWh)',
                'Spread_Paise': 'Δ Paise/kWh',
                'Cumulative_MW': 'Cum. MW',
                'Status_At_Load': 'Dispatch Status'
            })

            def _highlight_target_row(row):
                if row['Relative Position'] == "🎯 TARGET UNIT":
                    return ['background-color: rgba(229, 62, 62, 0.25); font-weight: bold;'] * len(row)
                elif "Cheaper" in str(row['Relative Position']):
                    return ['background-color: rgba(56, 161, 105, 0.08);'] * len(row)
                return ['background-color: rgba(214, 158, 46, 0.08);'] * len(row)

            st.dataframe(
                display_neigh.style.apply(_highlight_target_row, axis=1).format({
                    'Cap (MW)': '{:,.1f}',
                    'VC (₹/kWh)': '₹{:.4f}',
                    'Δ Paise/kWh': '{:+.2f} p',
                    'Cum. MW': '{:,.0f}'
                }),
                use_container_width=True,
                hide_index=True,
                height=420
            )

        with ncol2:
            n_colors = [
                '#E53E3E' if r == plant_data['MOD_Rank']
                else ('#38A169' if r < plant_data['MOD_Rank'] else '#DD6B20')
                for r in neigh_df['MOD_Rank']
            ]
            fig_neigh = go.Figure(go.Bar(
                x=neigh_df['Total_VC'],
                y=[f"#{int(r['MOD_Rank'])} {r['Generating_Station'][:20]}" for _, r in neigh_df.iterrows()],
                orientation='h',
                marker_color=n_colors,
                text=[f"₹{v:.4f} ({p:+.1f}p)" for v, p in zip(neigh_df['Total_VC'], neigh_df['Spread_Paise'])],
                textposition='auto',
                hovertemplate="<b>%{y}</b><br>VC: ₹%{x:.4f}/kWh<extra></extra>"
            ))
            fig_neigh.update_layout(
                title="Neighborhood VC Ladder (₹/kWh & Paise Spread)",
                xaxis_title="Variable Charge (₹/kWh)",
                yaxis=dict(autorange="reversed"),
                template="plotly_dark",
                height=420,
                margin=dict(l=10, r=10, t=40, b=20)
            )
            st.plotly_chart(fig_neigh, use_container_width=True)

        st.markdown("---")

        # ------------------------------------------------------------
        # NEW FEATURE 2: RANK-JUMP TARGETS & INTERACTIVE WHAT-IF SIMULATOR
        # ------------------------------------------------------------
        st.subheader("🧪 Strategic VC Optimizer & Heat Rate Sensitivity Simulator")

        sim_col1, sim_col2 = st.columns([1.15, 1.35])

        with sim_col1:
            st.markdown("##### 🎯 Key Rank-Jump Thresholds")
            curr_rank = int(plant_data['MOD_Rank'])
            curr_vc = float(plant_data['Total_VC'])

            # 1. Next 1 Rank Up
            if curr_rank > 1:
                above_1 = df.iloc[curr_rank - 2]
                delta_1_paise = (curr_vc - above_1['Total_VC']) * 100 + 0.01
                st.info(
                    f"**Jump 1 Rank (to #{curr_rank - 1} — {above_1['Generating_Station'][:25]}):**\n"
                    f"Target VC: **₹{above_1['Total_VC'] - 0.0001:.4f}/kWh** "
                    f"(Reduce by **{delta_1_paise:.2f} paise/kWh** | Overcomes **{above_1['Capacity_MW']:,.0f} MW**)"
                )
            else:
                st.success("🏆 This unit is already #1 (Cheapest Baseload Plant in the stack).")

            # 2. Jump 5 Ranks Up
            if curr_rank > 5:
                above_5 = df.iloc[curr_rank - 6]
                delta_5_paise = (curr_vc - above_5['Total_VC']) * 100 + 0.01
                mw_5_gain = plant_data['MW_Ahead_In_Queue'] - above_5['MW_Ahead_In_Queue']
                st.info(
                    f"**Jump 5 Ranks (to #{curr_rank - 5} — {above_5['Generating_Station'][:25]}):**\n"
                    f"Target VC: **₹{above_5['Total_VC'] - 0.0001:.4f}/kWh** "
                    f"(Reduce by **{delta_5_paise:.2f} paise/kWh** | Leapfrogs **{mw_5_gain:,.0f} MW**)"
                )

            # 3. Clear Current Demand Boundary if in RSD
            if simulated_demand <= plant_data['MW_Ahead_In_Queue'] and not marginal_matches.empty:
                clear_node = marginal_matches.iloc[0]
                needed_paise = (curr_vc - clear_node['Total_VC']) * 100 + 0.01
                st.warning(
                    f"**Escape RSD at {simulated_demand:,} MW Demand (Beat #{int(clear_node['MOD_Rank'])} {clear_node['Generating_Station'][:20]}):**\n"
                    f"Target VC: **₹{clear_node['Total_VC'] - 0.0001:.4f}/kWh** "
                    f"(Requires **-{needed_paise:.2f} paise/kWh** reduction)"
                )

        with sim_col2:
            st.markdown("##### ⚙️ Interactive 'What-If' Tariff & Heat Rate Simulator")
            sim_mode = st.radio(
                "Simulation Input Mode:",
                ["Direct Paise/kWh Adjustment", "Thermodynamic Heat Rate (kcal/kWh) Improvement"],
                horizontal=True
            )

            if sim_mode == "Direct Paise/kWh Adjustment":
                paise_shift = st.slider(
                    "Simulate Variable Charge Change (Paise/kWh):",
                    min_value=-100.0, max_value=100.0, value=-10.0, step=0.5
                )
            else:
                hr_col1, hr_col2, hr_col3 = st.columns(3)
                hr_reduction = hr_col1.number_input("Heat Rate Reduction (kcal/kWh):", min_value=0.0, max_value=500.0, value=45.0, step=5.0)
                coal_cost_mt = hr_col2.number_input("Landed Coal Cost (₹/MT):", min_value=1000.0, max_value=10000.0, value=4500.0, step=100.0)
                coal_gcv = hr_col3.number_input("As-Fired GCV (kcal/kg):", min_value=2000.0, max_value=6000.0, value=3600.0, step=50.0)
                # Δ VC (₹/kWh) = (ΔHR * Coal_Cost_per_kg) / GCV
                vc_saving_rs = (hr_reduction * (coal_cost_mt / 1000.0)) / coal_gcv
                paise_shift = -round(vc_saving_rs * 100.0, 2)
                st.caption(f"🔥 Improving Heat Rate by **{hr_reduction:.0f} kcal/kWh** saves **{abs(paise_shift):.2f} paise/kWh** (₹{vc_saving_rs:.4f}/kWh).")

            simulated_vc = max(0.01, curr_vc + (paise_shift / 100.0))

            # Re-simulate stack with modified VC
            sim_df = df.copy()
            sim_df.loc[sim_df['Generating_Station'] == selected_plant, 'Total_VC'] = simulated_vc
            sim_df = enrich_dataframe(sim_df)
            new_row = sim_df[sim_df['Generating_Station'] == selected_plant].iloc[0]

            new_rank = int(new_row['MOD_Rank'])
            rank_delta = curr_rank - new_rank  # positive means improved (moved closer to #1)
            mw_leapfrogged = plant_data['MW_Ahead_In_Queue'] - new_row['MW_Ahead_In_Queue']
            new_status, _, new_icon = get_dispatch_status(new_row, simulated_demand)

            res_c1, res_c2, res_c3, res_c4 = st.columns(4)
            res_c1.metric("Simulated VC", f"₹{simulated_vc:.4f}", f"{paise_shift:+.2f} p/kWh", delta_color="inverse")
            res_c2.metric("Simulated MOD Rank", f"#{new_rank}", f"{rank_delta:+d} Ranks")
            res_c3.metric("Queue Ahead", f"{new_row['MW_Ahead_In_Queue']:,.0f} MW", f"{-mw_leapfrogged:+,.0f} MW", delta_color="inverse")
            res_c4.metric("Simulated Status", f"{new_icon} {new_status}")

    # ============================================================
    # TAB 2: MERIT ORDER FLASHCARDS + PDF DOWNLOAD
    # ============================================================
    with tab2:
        fc_head1, fc_head2 = st.columns([2.2, 1.2])
        with fc_head1:
            st.subheader("Merit Order Flashcards — Ranked Cheapest to Most Expensive")

        fc1, fc2, fc3 = st.columns([1.8, 1.1, 1.1])
        with fc1:
            name_filter = st.text_input("🔍 Filter by station name", "")
        with fc2:
            status_filter = st.selectbox("Filter by dispatch status", ["All", "Safe", "Marginal", "Critical / RSD"])
        with fc3:
            owner_filter = st.selectbox("Filter by portfolio owner", ["All", "MAHAGENCO", "Central / ISGS", "Private IPP"])

        card_df = df.copy()
        if name_filter:
            card_df = card_df[card_df['Generating_Station'].str.contains(name_filter, case=False, na=False)]
        if owner_filter != "All":
            card_df = card_df[card_df['Owner_Group'] == owner_filter]
        if status_filter != "All":
            card_df = card_df[card_df.apply(lambda r: get_dispatch_status(r, simulated_demand)[0] == status_filter, axis=1)]

        card_df = card_df.sort_values('MOD_Rank')

        with fc_head2:
            st.markdown("<div style='height: 12px;'></div>", unsafe_allow_html=True)
            pdf_bytes_tab2 = generate_flashcards_pdf(card_df, len(df), simulated_demand, status_filter, name_filter)
            st.download_button(
                label="📥 Download Flashcards Status Brief (PDF)",
                data=pdf_bytes_tab2,
                file_name=f"MOD_Flashcards_Brief_{simulated_demand}MW.pdf",
                mime="application/pdf",
                use_container_width=True
            )

        cols_per_row = 4
        rows_of_cards = [card_df.iloc[i:i + cols_per_row] for i in range(0, len(card_df), cols_per_row)]

        if card_df.empty:
            st.info("No plants match the current filters.")
        else:
            for chunk in rows_of_cards:
                cols = st.columns(cols_per_row)
                for col, (_, row) in zip(cols, chunk.iterrows()):
                    label, color, icon = get_dispatch_status(row, simulated_demand)
                    with col:
                        st.markdown(
                            f"""
                            <div style="border:1px solid {color}44; border-left:6px solid {color};
                                        border-radius:10px; padding:12px 14px; margin-bottom:14px;
                                        background:linear-gradient(135deg, {color}0D, transparent);">
                                <div style="display:flex; justify-content:space-between; font-size:11px; color:#888; font-weight:600;">
                                    <span>RANK #{int(row['MOD_Rank'])} of {len(df)}</span>
                                    <span>{row.get('Owner_Group', '')}</span>
                                </div>
                                <div style="font-size:15px; font-weight:700; margin:4px 0 8px 0; min-height:38px;">{row['Generating_Station']}</div>
                                <div style="font-size:13px;">💰 Rate: <b>₹{row['Total_VC']:.4f}/kWh</b></div>
                                <div style="font-size:13px;">⚡ Capacity: <b>{row['Capacity_MW']:,.1f} MW</b></div>
                                <div style="font-size:13px;">📊 Cumulative: {row['Cumulative_MW']:,.0f} MW</div>
                                <div style="font-size:13px; margin-top:6px; color:{color}; font-weight:700;">{icon} {label}</div>
                            </div>
                            """,
                            unsafe_allow_html=True
                        )

    # ============================================================
    # TAB 3: AUDITABLE EXTRACTED DATA LEDGER + PDF DOWNLOAD
    # ============================================================
    with tab3:
        led_col1, led_col2, led_col3 = st.columns([2.0, 1.1, 1.1])
        with led_col1:
            st.subheader("Absolute Extracted Data Audit Trail")
        with led_col2:
            pdf_bytes_tab3 = generate_ledger_pdf(df, discom_choice, simulated_demand)
            st.download_button(
                label="📥 Download Official Audit Ledger (PDF)",
                data=pdf_bytes_tab3,
                file_name=f"MOD_Auditable_Ledger_{discom_choice}.pdf",
                mime="application/pdf",
                use_container_width=True
            )
        with led_col3:
            csv_bytes = df.to_csv(index=False).encode('utf-8')
            st.download_button(
                label="📊 Export Raw Audit Table (CSV)",
                data=csv_bytes,
                file_name=f"MOD_Auditable_Ledger_{discom_choice}.csv",
                mime="text/csv",
                use_container_width=True
            )

        ledger_view = df.copy()
        ledger_view['Dispatch_Status'] = ledger_view.apply(
            lambda r: f"{get_dispatch_status(r, simulated_demand)[2]} {get_dispatch_status(r, simulated_demand)[0]}",
            axis=1
        )

        st.dataframe(
            ledger_view[[
                'MOD_Rank', 'Generating_Station', 'Owner_Group', 'Capacity_MW',
                'Total_VC', 'Step_Jump_Paise', 'MW_Ahead_In_Queue', 'Cumulative_MW',
                'Demand_Zone', 'Dispatch_Status'
            ]],
            use_container_width=True,
            hide_index=True,
            height=540
        )

    # ============================================================
    # TAB 4: MACRO SYSTEM LOADING ZONES + PDF DOWNLOAD
    # ============================================================
    with tab4:
        # Compute Zone & Portfolio Summary Tables
        zone_rows = []
        for z_label in ZONE_LABELS:
            sub = df[df['Demand_Zone'] == z_label]
            tot_mw = sub['Capacity_MW'].sum()
            w_vc = (sub['Capacity_MW'] * sub['Total_VC']).sum() / tot_mw if tot_mw > 0 else 0.0
            zone_rows.append({
                'Demand_Zone': z_label,
                'Station_Count': len(sub),
                'Total_MW': tot_mw,
                'Weighted_Avg_VC': w_vc,
                'Min_VC': sub['Total_VC'].min() if not sub.empty else 0.0,
                'Max_VC': sub['Total_VC'].max() if not sub.empty else 0.0
            })
        zone_stats_df = pd.DataFrame(zone_rows)

        owner_rows = []
        total_stack_mw = max(df['Capacity_MW'].sum(), 1.0)
        for o_group, grp in df.groupby('Owner_Group'):
            o_mw = grp['Capacity_MW'].sum()
            o_wvc = (grp['Capacity_MW'] * grp['Total_VC']).sum() / o_mw if o_mw > 0 else 0.0
            owner_rows.append({
                'Owner_Group': o_group,
                'Stations': len(grp),
                'Total_MW': o_mw,
                'Share_Pct': (o_mw / total_stack_mw) * 100.0,
                'Weighted_Avg_VC': o_wvc
            })
        owner_stats_df = pd.DataFrame(owner_rows).sort_values('Total_MW', ascending=False)

        z_head1, z_head2 = st.columns([2.2, 1.2])
        with z_head1:
            st.subheader("Macro System Loading Zones & Portfolio Intelligence")
        with z_head2:
            pdf_bytes_tab4 = generate_zones_pdf(df, zone_stats_df, owner_stats_df, simulated_demand)
            st.download_button(
                label="📥 Download Macro Zones Report (PDF)",
                data=pdf_bytes_tab4,
                file_name=f"MOD_Macro_Loading_Zones_{discom_choice}.pdf",
                mime="application/pdf",
                use_container_width=True
            )

        z_col1, z_col2 = st.columns([1.35, 1.0])
        with z_col1:
            fig_zones = px.bar(
                zone_stats_df,
                x='Demand_Zone',
                y='Total_MW',
                color='Demand_Zone',
                title="Aggregated Capacity Blocks per 5,000 MW Demand Interval",
                text_auto='.0f'
            )
            fig_zones.update_layout(
                template="plotly_dark",
                showlegend=False,
                xaxis_title="",
                yaxis_title="Total Block Capacity (MW)",
                height=420
            )
            st.plotly_chart(fig_zones, use_container_width=True)

        with z_col2:
            fig_owner = px.pie(
                owner_stats_df,
                names='Owner_Group',
                values='Total_MW',
                hole=0.45,
                title="System Capacity Share by Utility Portfolio (MW)"
            )
            fig_owner.update_layout(template="plotly_dark", height=420)
            st.plotly_chart(fig_owner, use_container_width=True)

        st.markdown("##### 📊 Zone-Wise Tariff & Capacity Breakdown")
        st.dataframe(
            zone_stats_df.style.format({
                'Total_MW': '{:,.1f} MW',
                'Weighted_Avg_VC': '₹{:.4f}/kWh',
                'Min_VC': '₹{:.4f}/kWh',
                'Max_VC': '₹{:.4f}/kWh'
            }),
            use_container_width=True,
            hide_index=True
        )

        st.markdown("##### 🏛️ Utility Portfolio Benchmarking (MAHAGENCO vs. Central ISGS vs. Private IPPs)")
        st.dataframe(
            owner_stats_df.style.format({
                'Total_MW': '{:,.1f} MW',
                'Share_Pct': '{:.1f}%',
                'Weighted_Avg_VC': '₹{:.4f}/kWh'
            }),
            use_container_width=True,
            hide_index=True
        )
else:
    st.info("👈 Upload a MOD Rate Stack (PDF or Excel) in the sidebar to build the dashboard.")
