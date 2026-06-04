import io
import math
import re
from datetime import date, datetime
import pandas as pd
import streamlit as st

st.set_page_config(page_title="CEVA / NovaXpress Tariff Calculator", page_icon="📦", layout="centered")

# ---------------------- TARIFF DATES ----------------------
TARIFF_EFFECTIVE = "2026-04-06"
TARIFF_EXPIRY    = "2026-12-31"

# ---------------------- TARIFF DATA (Effective 6-Apr-26) ----------------------
MIN_CHARGE = {1: 30.00, 2: 45.00, 3: 60.00, 4: 70.00, 5: 80.00}

RATES = {
    "0-500":     (500,          [0.064, 0.120, 0.167, 0.224, 0.261]),
    "501-1000":  (1000,         [0.054, 0.083, 0.111, 0.158, 0.186]),
    "1001-2000": (2000,         [0.045, 0.054, 0.064, 0.101, 0.130]),
    "2001-4000": (4000,         [0.036, 0.045, 0.054, 0.064, 0.073]),
    "4001+":     (float("inf"), [0.022, 0.031, 0.040, 0.051, 0.059]),
}

OOA_RATE = {"FULL": 1.50, "BACKHAUL EMPTY": 0.80, "BACKHAUL FULL": 1.00}

ACCESSORIALS = {
    "2 Man Service":             25.0,
    "Tailgate (over 200 lbs)":   15.0,
    "Inside Delivery":           25.0,
    "White Glove (residential)": 80.0,
    "Skid Handbomb (lumper)":    40.0,
    "Direct Drive (flat)":       40.0,
}

WAIT_RATE_HR = 60.0

# ---------------------- PDF PARSER ----------------------
def parse_waybill(pdf_bytes: bytes) -> dict:
    """
    Extract consignee address and weight from a CEVA waybill PDF.
    Returns dict with keys: consignee_name, consignee_address, weight_lbs, ref_no, parse_notes
    """
    try:
        import pdfplumber
    except ImportError:
        return {"error": "pdfplumber not installed. Add it to requirements.txt."}

    result = {
        "consignee_name":    "",
        "consignee_address": "",
        "weight_lbs":        0.0,
        "ref_no":            "",
        "parse_notes":       [],
    }

    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            # Use first page only — waybill is always page 1 (customer copy)
            page = pdf.pages[0]
            text = page.extract_text() or ""
    except Exception as e:
        return {"error": f"Could not read PDF: {e}"}

    lines = [l.strip() for l in text.splitlines() if l.strip()]

    # --- Weight ---
    # Patterns: "112.000 Lbs", "10.000 Kgs", "66.138 Lbs"
    weight_match = re.search(r'(\d+\.?\d*)\s*(Lbs|Kgs|lbs|kgs|LBS|KGS)', text)
    if weight_match:
        val  = float(weight_match.group(1))
        unit = weight_match.group(2).lower()
        if "kg" in unit:
            val = val * 2.20462
            result["parse_notes"].append(f"Weight converted from kg: {weight_match.group(1)} kg → {val:.3f} lbs")
        result["weight_lbs"] = round(val, 3)
    else:
        result["parse_notes"].append("Weight not found — enter manually.")

    # --- House/Ref # ---
    ref_match = re.search(r'(?:House/Ref\s*#[:\s]+|NLS|AZN|DVB|DLF|DY4|CTM[TV]|VFB|VGB|VTO|WAW|ZH)([A-Z0-9\-]+)', text)
    if ref_match:
        result["ref_no"] = ref_match.group(1).strip()

    # --- Consignee block ---
    # Strategy: find "Consignee / Consignataire" label, then grab the lines that follow
    # The consignee block ends when we hit "House/Ref" or "Attn:" or a known field label
    consignee_lines = []
    in_consignee = False
    stop_words = {"house/ref", "attn:", "pickup date", "service/de", "prepaid", "dangerous", "good desc", "billing party", "dimensions", "special inst", "references"}

    for i, line in enumerate(lines):
        low = line.lower()
        if "consignee / consignataire" in low or "consignee/consignataire" in low:
            in_consignee = True
            continue
        if in_consignee:
            if any(sw in low for sw in stop_words):
                break
            # Skip lines that are clearly shipper-side labels
            if "shipper" in low or "expéditeur" in low:
                break
            # Skip very short noise lines and known non-address tokens
            if len(line) < 3:
                continue
            if re.match(r'^\d{10,}$', line):  # bare account numbers
                continue
            consignee_lines.append(line)

    if consignee_lines:
        result["consignee_name"]    = consignee_lines[0]
        result["consignee_address"] = ", ".join(consignee_lines[1:]) if len(consignee_lines) > 1 else ""
    else:
        result["parse_notes"].append("Consignee address not found — enter manually.")

    return result


# ---------------------- CALC HELPERS ----------------------
def zone_from_km(km: float):
    if km <= 50:  return 1
    if km <= 150: return 2
    if km <= 300: return 3
    if km <= 400: return 4
    if km <= 500: return 5
    return None

def bracket_and_rate(weight_lbs: float, zone: int):
    for name, (upper, zrates) in RATES.items():
        if weight_lbs <= upper:
            return name, zrates[zone - 1]
    return "4001+", RATES["4001+"][1][zone - 1]

def calculate(distance_km, weight_lbs, is_ooa, ooa_type, ooa_km, flags, wait_minutes, fuel_pct):
    zone = zone_from_km(distance_km)
    if zone is None:
        return {"error": "Distance exceeds Zone 5 (500 km) supported by this tariff."}

    bracket, rate_per_lb = bracket_and_rate(weight_lbs, zone)
    base = max(MIN_CHARGE[zone], rate_per_lb * weight_lbs)
    ooa_charge = OOA_RATE[ooa_type] * ooa_km if is_ooa and ooa_km > 0 else 0.0
    acc = sum(v for k, v in ACCESSORIALS.items() if flags.get(k, False))

    wait_charge = 0.0
    if wait_minutes > 30:
        increments = math.ceil((wait_minutes - 30) / 15)
        wait_charge = (WAIT_RATE_HR / 4.0) * increments
        acc += wait_charge

    dd_flat  = ACCESSORIALS["Direct Drive (flat)"] if flags.get("Direct Drive (flat)", False) else 0.0
    fuelable = base + ooa_charge + dd_flat
    fuel_amt = fuelable * fuel_pct
    total    = base + ooa_charge + acc + fuel_amt

    return {
        "Zone":                    zone,
        "Weight Bracket":          bracket,
        "Rate per lb":             rate_per_lb,
        "Base LTL":                round(base, 2),
        "OOA charge":              round(ooa_charge, 2),
        "Accessorials (non-fuel)": round(acc - wait_charge, 2),
        "Wait Time charge":        round(wait_charge, 2),
        "Fuelable Subtotal":       round(fuelable, 2),
        "Fuel % used":             fuel_pct,
        "Fuel amount":             round(fuel_amt, 2),
        "Grand Total":             round(total, 2),
    }

# ---------------------- SESSION STATE ----------------------
if "log"            not in st.session_state: st.session_state.log            = []
if "pdf_weight"     not in st.session_state: st.session_state.pdf_weight     = 0.0
if "pdf_consignee"  not in st.session_state: st.session_state.pdf_consignee  = ""
if "pdf_ref"        not in st.session_state: st.session_state.pdf_ref        = ""
if "pdf_parsed"     not in st.session_state: st.session_state.pdf_parsed     = False

# ---------------------- UI ----------------------
st.title("📦 CEVA / NovaXpress Tariff Calculator")

today     = date.today()
eff       = date.fromisoformat(TARIFF_EFFECTIVE)
exp       = date.fromisoformat(TARIFF_EXPIRY)
days_left = (exp - today).days

if today > exp:
    st.error(f"⚠️ This tariff expired on {exp.strftime('%b %d, %Y')}. Rates may no longer be valid — update before use.")
elif days_left <= 30:
    st.warning(f"⚠️ This tariff expires in {days_left} day(s) on {exp.strftime('%b %d, %Y')}. Confirm rates are still current.")
else:
    st.info(f"📋 Tariff effective {eff.strftime('%b %d, %Y')} · Expires {exp.strftime('%b %d, %Y')} ({days_left} days remaining)")

# ---------------------- PDF UPLOAD ----------------------
st.subheader("📄 Upload Waybill (optional)")
st.caption("Upload a CEVA waybill PDF to auto-fill weight and consignee details.")

uploaded = st.file_uploader("Upload waybill PDF", type=["pdf"], label_visibility="collapsed")

if uploaded is not None:
    parsed = parse_waybill(uploaded.read())
    if "error" in parsed:
        st.error(parsed["error"])
    else:
        st.session_state.pdf_weight    = parsed["weight_lbs"]
        st.session_state.pdf_consignee = f'{parsed["consignee_name"]}  |  {parsed["consignee_address"]}'.strip(" |")
        st.session_state.pdf_ref       = parsed["ref_no"]
        st.session_state.pdf_parsed    = True

        cols = st.columns(3)
        cols[0].metric("Weight extracted", f'{parsed["weight_lbs"]:.3f} lbs')
        cols[1].metric("Ref #", parsed["ref_no"] or "—")
        cols[2].metric("Consignee", parsed["consignee_name"] or "—")

        if parsed["consignee_address"]:
            st.caption(f"📍 {parsed['consignee_address']}")
        if parsed["parse_notes"]:
            for note in parsed["parse_notes"]:
                st.warning(f"⚠️ {note}")

# ---------------------- SHIPMENT DETAILS ----------------------
st.markdown("---")
st.subheader("Shipment Details")

# Pre-fill weight from PDF if available, otherwise 0
default_weight = st.session_state.pdf_weight if st.session_state.pdf_parsed else 0.0
default_ref    = st.session_state.pdf_ref    if st.session_state.pdf_parsed else ""

col1, col2 = st.columns(2)

with col1:
    distance_km = st.number_input("Distance (km)", min_value=0.0, max_value=500.0, value=0.0, step=1.0)
    weight_lbs  = st.number_input("Weight (lbs)",  min_value=0.0, value=default_weight, step=1.0)

with col2:
    ref_number = st.text_input("Reference / Job #", value=default_ref, placeholder="e.g. NLS1268763")
    is_ooa   = st.selectbox("Is Out-of-Area?", ["No", "Yes"], index=0) == "Yes"
    ooa_type = st.selectbox("Out-of-Area Type", list(OOA_RATE.keys()), index=0, disabled=not is_ooa)
    ooa_km   = st.number_input("Out-of-Area KM", min_value=0.0, value=0.0, step=1.0, disabled=not is_ooa)

# Show parsed consignee as read-only context if available
if st.session_state.pdf_consignee:
    st.caption(f"🚚 Consignee: {st.session_state.pdf_consignee}")

st.markdown("---")
st.caption("Accessorials — toggle as needed")
c1, c2 = st.columns(2)

with c1:
    two_man      = st.toggle("2 Man Service ($25)",                                       value=(weight_lbs > 70))
    tailgate     = st.toggle("Tailgate over 200 lbs ($15)",                               value=(weight_lbs > 200))
    inside       = st.toggle("Inside Delivery ($25)",                                     value=False)

with c2:
    white_glove  = st.toggle("White Glove residential ($80 — includes 2-man/TG/Inside)", value=False)
    handbomb     = st.toggle("Skid Handbomb / lumper ($40)",                              value=False)
    direct_drive = st.toggle("Direct Drive flat ($40)",                                   value=False)

wait_minutes = st.number_input(
    "Wait Time (minutes — first 30 free, then $60/hr billed every 15 min)",
    min_value=0, value=0, step=1
)

st.markdown("---")
st.subheader("Fuel Surcharge (FCA)")
st.caption("Enter the current FCA rate provided by CEVA. Set to 0 if not applicable.")
fuel_pct_input = st.number_input("Fuel Surcharge % (e.g. 12 for 12%)", min_value=0.0, value=0.0, step=0.5)

if st.button("Calculate", type="primary"):
    effective_two_man  = False if white_glove else two_man
    effective_tailgate = False if white_glove else tailgate

    flags = {
        "2 Man Service":             effective_two_man,
        "Tailgate (over 200 lbs)":   effective_tailgate,
        "Inside Delivery":           False if white_glove else inside,
        "White Glove (residential)": white_glove,
        "Skid Handbomb (lumper)":    handbomb,
        "Direct Drive (flat)":       direct_drive,
    }

    res = calculate(
        distance_km, weight_lbs,
        is_ooa, ooa_type, ooa_km,
        flags, wait_minutes,
        fuel_pct=fuel_pct_input / 100.0,
    )

    if "error" in res:
        st.error(res["error"])
    else:
        st.subheader("Derived")
        left, right = st.columns(2)
        with left:
            st.metric("Zone",                   res["Zone"])
            st.metric("Weight Bracket",         res["Weight Bracket"])
            st.metric("Rate per lb",            f'${res["Rate per lb"]:.3f}')
            st.metric("Minimum Charge by Zone", f'${MIN_CHARGE[res["Zone"]]:,.2f}')
        with right:
            st.metric("Base LTL",               f'${res["Base LTL"]:.2f}')
            st.metric("Fuel % (FCA) used",      f'{res["Fuel % used"] * 100:.2f}%')
            st.metric("Fuel amount",            f'${res["Fuel amount"]:.2f}')
            st.metric("Grand Total",            f'${res["Grand Total"]:.2f}')

        st.write("---")
        st.subheader("Breakdown")
        df = pd.DataFrame({
            "Component": [
                "Base LTL",
                "Out-of-Area charge",
                "Accessorials (non-fuel)",
                "Wait Time charge",
                "Fuel amount (FCA)",
            ],
            "Amount ($)": [
                res["Base LTL"],
                res["OOA charge"],
                res["Accessorials (non-fuel)"],
                res["Wait Time charge"],
                res["Fuel amount"],
            ],
        })
        st.dataframe(df, use_container_width=True)
        st.success(f"**Grand Total: ${res['Grand Total']:,.2f}**")

        # Log entry
        st.session_state.log.append({
            "Timestamp":              datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "Ref #":                  ref_number,
            "Consignee":              st.session_state.pdf_consignee,
            "Distance (km)":          distance_km,
            "Weight (lbs)":           weight_lbs,
            "Zone":                   res["Zone"],
            "Weight Bracket":         res["Weight Bracket"],
            "Rate per lb":            res["Rate per lb"],
            "OOA Type":               ooa_type if is_ooa else "N/A",
            "OOA KM":                 ooa_km if is_ooa else 0,
            "2 Man":                  effective_two_man,
            "Tailgate":               effective_tailgate,
            "Inside Delivery":        flags["Inside Delivery"],
            "White Glove":            white_glove,
            "Handbomb":               handbomb,
            "Direct Drive":           direct_drive,
            "Wait Time (min)":        wait_minutes,
            "Fuel % (FCA)":           f'{fuel_pct_input:.1f}%',
            "Base LTL ($)":           res["Base LTL"],
            "OOA Charge ($)":         res["OOA charge"],
            "Accessorials ($)":       res["Accessorials (non-fuel)"],
            "Wait Time Charge ($)":   res["Wait Time charge"],
            "Fuel Amount ($)":        res["Fuel amount"],
            "Grand Total ($)":        res["Grand Total"],
        })

        # Reset PDF state so next upload is fresh
        st.session_state.pdf_parsed    = False
        st.session_state.pdf_weight    = 0.0
        st.session_state.pdf_consignee = ""
        st.session_state.pdf_ref       = ""

# ---------------------- EXPORT ----------------------
if st.session_state.log:
    st.markdown("---")
    st.subheader("Session Log")
    log_df = pd.DataFrame(st.session_state.log)
    st.dataframe(log_df, use_container_width=True)

    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        log_df.to_excel(writer, index=False, sheet_name="Calculations")
    buf.seek(0)

    st.download_button(
        label="⬇️ Download as Excel",
        data=buf,
        file_name=f"nova_xpress_calculations_{date.today().isoformat()}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

    if st.button("Clear log"):
        st.session_state.log = []
        st.rerun()
