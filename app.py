import io
import math
import re
from datetime import date, datetime
import pandas as pd
import streamlit as st
import time
import requests

# ---------------------- CSV MANIFEST INGESTION + DISTANCE CALC ----------------------
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
OSRM_URL      = "https://router.project-osrm.org/route/v1/driving"
NOMINATIM_HEADERS = {"User-Agent": "VAREK-CEVA-Calculator/1.0 (personal use)"}

def _nominatim_query(query):
    try:
        resp = requests.get(
            NOMINATIM_URL,
            params={"q": query, "format": "json", "limit": 1, "countrycodes": "ca"},
            headers=NOMINATIM_HEADERS, timeout=10,
        )
        resp.raise_for_status()
        results = resp.json()
        if results:
            return (float(results[0]["lat"]), float(results[0]["lon"]))
    except Exception:
        pass
    return None

def geocode_address(address1, city, state, zipcode):
    """
    Progressive fallback geocoding. Exact street addresses often fail on
    abbreviated street types ('68 ST SE') and in small northern communities
    with thin OpenStreetMap coverage. Falling back to city/postal level still
    gives a usable number: the zone bands are 50/150/300/400/500 km wide, so
    being a few km off almost never changes which zone a shipment lands in.
    Returns (coords, precision_label).
    """
    attempts = [
        (f"{address1}, {city}, {state}, {zipcode}, Canada", "exact"),
        (f"{city}, {state}, {zipcode}, Canada",             "city+postal"),
        (f"{zipcode}, Canada",                              "postal"),
        (f"{city}, {state}, Canada",                        "city"),
    ]
    for query, precision in attempts:
        time.sleep(1.0)  # Nominatim usage policy: ~1 req/sec
        coords = _nominatim_query(query)
        if coords:
            return coords, precision
    return None, "failed"

def geocode_cached(address1, city, state, zipcode, cache):
    key = f"{address1}|{city}|{state}|{zipcode}".upper().strip()
    if key in cache:
        return cache[key]
    result = geocode_address(address1, city, state, zipcode)
    cache[key] = result
    return result

def driving_distance_km(origin, dest):
    if not origin or not dest:
        return None
    try:
        lat1, lon1 = origin
        lat2, lon2 = dest
        url = f"{OSRM_URL}/{lon1},{lat1};{lon2},{lat2}"
        resp = requests.get(url, params={"overview": "false"}, timeout=8)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") == "Ok" and data.get("routes"):
            return round(data["routes"][0]["distance"] / 1000.0, 1)
    except Exception:
        pass
    return None

REMARKS_ACCESSORIAL_HINTS = {
    "2 Man Service":             ["2 MAN", "2-MAN", "TWO MAN"],
    "Tailgate (over 200 lbs)":   ["LIFTGATE", "TAILGATE", "TG REQ"],
    "Inside Delivery":           ["INSIDE DELIVERY", "ROOM OF CHOICE"],
    "White Glove (residential)": ["WHITE GLOVE", "WGD"],
    "Skid Handbomb (lumper)":    ["HANDBOMB", "LUMPER"],
}

def suggest_accessorials_from_remarks(remarks):
    remarks_upper = (remarks or "").upper()
    return {k: any(kw in remarks_upper for kw in kws) for k, kws in REMARKS_ACCESSORIAL_HINTS.items()}

def parse_manifest_csv(csv_bytes):
    """Parse a CEVA manifest CSV export into the same waybill-dict shape the
    PDF batch parser produces, so the batch UI / review flags work unchanged.
    NOTE: uses Chargeable Weight (lbs) — confirm with CEVA this is the billed weight."""
    required = ["Booking Number", "Chargeable Weight (lbs)",
                "Shipper Address 1", "Shipper City", "Shipper State", "Shipper Zipcode",
                "Consignee Name", "Consignee Address 1", "Consignee City",
                "Consignee State", "Consignee Zipcode"]
    try:
        df = pd.read_csv(io.BytesIO(csv_bytes))
    except Exception:
        df = pd.read_excel(io.BytesIO(csv_bytes))
    missing = [c for c in required if c not in df.columns]
    if missing:
        return [{"error": f"CSV is missing expected column(s): {', '.join(missing)}"}]

    results = []
    for _, row in df.iterrows():
        remarks = str(row.get("Remarks", "") or "")
        weight = float(row.get("Chargeable Weight (lbs)", 0) or 0)
        cons_addr1 = str(row.get("Consignee Address 1", "")).strip()
        results.append({
            "dg_number":          str(row.get("Booking Number", "")).strip(),
            "ref_no":             str(row.get("Housebill", "")).strip(),
            "weight_lbs":         weight,
            "shipper_name":       str(row.get("Shipper Name", "")).strip(),
            "shipper_address1":   str(row.get("Shipper Address 1", "")).strip(),
            "shipper_city":       str(row.get("Shipper City", "")).strip(),
            "shipper_state":      str(row.get("Shipper State", "")).strip(),
            "shipper_zip":        str(row.get("Shipper Zipcode", "")).strip(),
            "consignee_name":     str(row.get("Consignee Name", "")).strip(),
            "consignee_address1": cons_addr1,
            "consignee_city":     str(row.get("Consignee City", "")).strip(),
            "consignee_state":    str(row.get("Consignee State", "")).strip(),
            "consignee_zip":      str(row.get("Consignee Zipcode", "")).strip(),
            "consignee_address":  f'{cons_addr1}, {row.get("Consignee City","")}, {row.get("Consignee State","")} {row.get("Consignee Zipcode","")}',
            "remarks":            remarks,
            "item_description":   str(row.get("Item Description", "")).strip(),
            "service_level":      str(row.get("Service Level", "")).strip(),
            "suggested_accessorials": suggest_accessorials_from_remarks(remarks),
            "needs_review": (weight <= 0 or not cons_addr1),
            "parse_notes": [],
        })
    return results


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
def extract_address_block(lines: list, start_label: str, stop_words: set) -> list:
    result_lines = []
    in_block     = False
    for line in lines:
        low = line.lower()
        if start_label in low:
            in_block = True
            continue
        if in_block:
            if any(sw in low for sw in stop_words):
                break
            if len(line) < 3:
                continue
            if re.match(r'^\d{10,}$', line):
                continue
            if line.endswith("..."):
                continue
            result_lines.append(line)
    return result_lines


def extract_consignee_from_page(page) -> list:
    try:
        w, h  = page.width, page.height
        crop  = page.crop((w * 0.35, h * 0.08, w * 0.65, h * 0.40))
        text  = crop.extract_text() or ""
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        stop  = {"house/ref", "pickup date", "service/de", "prepaid", "attn", "collect"}
        result     = []
        skip_label = True
        for line in lines:
            low = line.lower()
            if skip_label:
                if "consignataire" in low or "consignee" in low:
                    skip_label = False
                continue
            if any(sw in low for sw in stop):
                break
            if len(line) < 3 or re.match(r"^\d{7,}$", line):
                continue
            line = re.split(r"\s{3,}", line)[0].strip()
            line = re.sub(r"\.{2,}$", "", line).strip()
            if line in ("NOVA", "YOW", "YUL", "YYZ", "ECO", "APT", "THD", "WGD"):
                continue
            if len(line) < 3:
                continue
            if line:
                result.append(line)
        return result
    except Exception:
        return []


def extract_shipper_from_page(page) -> list:
    try:
        w, h  = page.width, page.height
        crop  = page.crop((0, h * 0.08, w * 0.35, h * 0.40))
        text  = crop.extract_text() or ""
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        stop  = {"house/ref", "pickup date", "service/de", "prepaid", "attn", "collect"}
        result     = []
        skip_label = True
        for line in lines:
            low = line.lower()
            if skip_label:
                if "expéditeur" in low or "shipper" in low:
                    skip_label = False
                continue
            if any(sw in low for sw in stop):
                break
            if len(line) < 3 or re.match(r"^\d{7,}$", line):
                continue
            line = re.split(r"\s{3,}", line)[0].strip()
            line = re.sub(r"\.{2,}$", "", line).strip()
            if line in ("NOVA", "YOW", "YUL", "YYZ", "ECO", "APT", "THD", "WGD"):
                continue
            if len(line) < 3:
                continue
            if line:
                result.append(line)
        return result
    except Exception:
        return []


def parse_waybill_page(text: str, page=None) -> dict:
    """Parse extracted text from one waybill page. Returns parsed fields dict.
    Pass page object for crop-based address extraction (more accurate)."""
    result = {
        "consignee_name":    "",
        "consignee_address": "",
        "shipper_name":      "",
        "shipper_address":   "",
        "is_pickup":         False,
        "routing_address":   "",
        "weight_lbs":        0.0,
        "ref_no":            "",
        "dg_number":         "",
        "parse_notes":       [],
    }

    lines = [l.strip() for l in text.splitlines() if l.strip()]

    result["is_pickup"] = bool(re.search(r'\(PICKUP\)', text, re.IGNORECASE))

    weight_match = re.search(r'(\d+\.?\d*)\s*(Lbs|Kgs|lbs|kgs|LBS|KGS)', text)
    if weight_match:
        val  = float(weight_match.group(1))
        unit = weight_match.group(2).lower()
        if "kg" in unit:
            val = round(val * 2.20462, 3)
            result["parse_notes"].append(f"Weight converted: {weight_match.group(1)} kg → {val} lbs")
        result["weight_lbs"] = round(val, 3)
    else:
        result["parse_notes"].append("weight_missing")

    dg_match = re.search(r'((?:DG|GD)\d{3}-\d{7})', text)
    if dg_match:
        result["dg_number"] = dg_match.group(1)

    ref_match = re.search(
        r'(?:House/Ref\s*#[:\s]+)([A-Z0-9\-]{4,})|'
        r'\b(NLS\d{4,}|AZN\d{4,}|DVB\w{4,}|DLF\w{4,}|DY4\w{4,}|CTM[TV]\d{4,}|VFB\d{4,}|VGB\d{4,}|VTO\d{4,}|WAW\d{4,}|ZH\d{4,})\b',
        text
    )
    if ref_match:
        val = (ref_match.group(1) or ref_match.group(2) or "").strip()
        if len(val) >= 4:
            result["ref_no"] = val

    if page is not None:
        consignee_lines = extract_consignee_from_page(page)
    else:
        stop_words = {
            "house/ref", "attn:", "pickup date", "service/de", "prepaid",
            "dangerous", "good desc", "billing party", "dimensions",
            "special inst", "references", "collect/port"
        }
        consignee_lines = extract_address_block(
            lines, "consignee / consignataire",
            stop_words | {"shipper", "expéditeur"},
        )

    if consignee_lines:
        result["consignee_name"]    = consignee_lines[0]
        result["consignee_address"] = ", ".join(consignee_lines[1:]) if len(consignee_lines) > 1 else ""
    else:
        result["parse_notes"].append("consignee_missing")

    if page is not None:
        shipper_lines = extract_shipper_from_page(page)
    else:
        stop_words = {
            "house/ref", "attn:", "pickup date", "service/de", "prepaid",
            "dangerous", "good desc", "billing party", "dimensions",
            "special inst", "references", "collect/port"
        }
        shipper_lines = extract_address_block(
            lines, "shipper / expéditeur",
            stop_words | {"consignee", "consignataire"},
        )

    if shipper_lines:
        result["shipper_name"]    = shipper_lines[0]
        result["shipper_address"] = ", ".join(shipper_lines[1:]) if len(shipper_lines) > 1 else ""
    else:
        result["parse_notes"].append("shipper_missing")

    if result["is_pickup"]:
        result["routing_address"] = f'{result["shipper_name"]} {result["shipper_address"]}'.strip()
    else:
        result["routing_address"] = f'{result["consignee_name"]} {result["consignee_address"]}'.strip()

    # NEW: overall confidence flag — anything downstream (UI, batch queue) can
    # check this instead of re-deriving "is this row trustworthy" logic itself.
    result["needs_review"] = bool(
        set(result["parse_notes"]) & {"weight_missing", "consignee_missing", "shipper_missing"}
        or not result["dg_number"]
    )

    return result


def decode_barcode_from_page(page) -> str:
    try:
        from pyzbar import pyzbar
    except ImportError:
        return ""
    for dpi in (200, 300):
        try:
            img      = page.to_image(resolution=dpi).original
            barcodes = pyzbar.decode(img)
            if barcodes:
                return barcodes[0].data.decode("utf-8", errors="ignore").strip()
        except Exception:
            continue
    return ""


def parse_waybill(pdf_bytes: bytes) -> dict:
    """Parse single waybill PDF — reads first page only."""
    try:
        import pdfplumber
    except ImportError:
        return {"error": "pdfplumber not installed — add to requirements.txt"}
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            page = pdf.pages[0]
            text = page.extract_text() or ""
            barcode_val = decode_barcode_from_page(page)
    except Exception as e:
        return {"error": f"Could not read PDF: {e}"}
    result = parse_waybill_page(text, page=page)
    if barcode_val:
        result["dg_number"]    = barcode_val
        result["barcode_read"] = True
    else:
        result["barcode_read"] = False
    result["needs_review"] = bool(
        set(result["parse_notes"]) & {"weight_missing", "consignee_missing", "shipper_missing"}
        or not result["dg_number"]
    )
    return result


def parse_waybill_batch(pdf_bytes: bytes) -> list:
    """
    Parse all waybills from a multi-page PDF.
    Each unique DG number (or ref#, as fallback) = one waybill. Deduplicates.
    """
    try:
        import pdfplumber
    except ImportError:
        return [{"error": "pdfplumber not installed — add to requirements.txt"}]
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            pages_text = [p.extract_text() or "" for p in pdf.pages]
    except Exception as e:
        return [{"error": f"Could not read PDF: {e}"}]

    seen_dg = set()
    results = []

    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf2:
            pages_objs = list(pdf2.pages)
    except Exception:
        pages_objs = [None] * len(pages_text)

    for i, text in enumerate(pages_text):
        # FIX: page_obj must be resolved BEFORE it's used to parse this page.
        # Previously this line came *after* parse_waybill_page(..., page=page_obj),
        # which meant every waybill in a batch was parsed against the page object
        # left over from the PREVIOUS loop iteration (a NameError on the very
        # first page, and an off-by-one page mismatch on every page after that).
        page_obj = pages_objs[i] if i < len(pages_objs) else None

        parsed = parse_waybill_page(text, page=page_obj)

        barcode_val = decode_barcode_from_page(page_obj) if page_obj else ""
        if barcode_val:
            parsed["dg_number"]    = barcode_val
            parsed["barcode_read"] = True
        else:
            parsed["barcode_read"] = False

        parsed["source_page"] = i + 1  # for traceability if something looks wrong

        key = parsed["dg_number"] or parsed["ref_no"]
        # Skip blank/template pages — no valid identifier (e.g. routing stickers)
        if not key or len(key) < 4:
            continue
        if key in seen_dg:
            continue
        seen_dg.add(key)

        parsed["needs_review"] = bool(
            set(parsed["parse_notes"]) & {"weight_missing", "consignee_missing", "shipper_missing"}
            or not parsed["dg_number"]
        )

        results.append(parsed)

    return results


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
    base       = max(MIN_CHARGE[zone], rate_per_lb * weight_lbs)
    ooa_charge = OOA_RATE[ooa_type] * ooa_km if is_ooa and ooa_km > 0 else 0.0
    acc        = sum(v for k, v in ACCESSORIALS.items() if flags.get(k, False))

    wait_charge = 0.0
    if wait_minutes > 30:
        increments  = math.ceil((wait_minutes - 30) / 15)
        wait_charge = (WAIT_RATE_HR / 4.0) * increments
        acc        += wait_charge

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
defaults = {
    "log":            [],
    "pdf_weight":     0.0,
    "pdf_consignee":  "",
    "pdf_ref":        "",
    "pdf_dg":         "",
    "pdf_parsed":     False,
    "last_file_id":   None,
    "fca_pct":        0.0,
    "batch_queue":    [],
    "batch_idx":      0,
    "batch_mode":     False,
    # NEW: CSV manifest ingestion path (separate queue from the PDF batch queue,
    # since a CSV row and a parsed PDF page carry slightly different fields)
    "csv_queue":      [],
    "csv_idx":        0,
    "csv_mode":       False,
    "geocode_cache":  {},
    "csv_last_file_id": None,
    "csv_original_df": None,   # NEW: raw manifest, kept as-is so we can output it augmented
    "csv_results_by_idx": {},  # NEW: row index -> computed charge fields
    "auto_distance_km": None,
}
for k, v in defaults.items():
    if k not in st.session_state:
        st.session_state[k] = v

# ---------------------- UI — HEADER ----------------------
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

# ---------------------- UI — PDF UPLOAD ----------------------
st.subheader("📄 Upload Waybill")
st.caption("Single waybill or full day batch PDF. Weight, DG #, ref # and consignee will auto-fill.")

uploaded = st.file_uploader("Upload waybill PDF", type=["pdf"], label_visibility="collapsed")

if uploaded is not None:
    file_id = (uploaded.name, uploaded.size)
    if file_id != st.session_state.last_file_id:
        st.session_state.last_file_id = file_id
        pdf_bytes = uploaded.read()

        try:
            import pdfplumber
            with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                page_count = len(pdf.pages)
        except Exception:
            page_count = 1

        if page_count > 2:
            results = parse_waybill_batch(pdf_bytes)
            if results and "error" in results[0]:
                st.error(results[0]["error"])
            else:
                st.session_state.batch_queue = results
                st.session_state.batch_idx   = 0
                st.session_state.batch_mode  = True
                st.session_state.pdf_parsed  = False
                n_review = sum(1 for r in results if r.get("needs_review"))
                msg = f"📦 Found {len(results)} waybills. Working through them one by one."
                if n_review:
                    msg += f" ⚠️ {n_review} flagged for review (missing weight/DG#/address)."
                st.success(msg)
        else:
            parsed = parse_waybill(pdf_bytes)
            if "error" in parsed:
                st.error(parsed["error"])
            else:
                st.session_state.batch_mode    = False
                st.session_state.batch_queue   = []
                st.session_state.pdf_weight    = parsed["weight_lbs"]
                st.session_state.pdf_consignee = f'{parsed["consignee_name"]}  |  {parsed["consignee_address"]}'.strip(" |")
                st.session_state.pdf_ref       = parsed["ref_no"]
                st.session_state.pdf_dg        = parsed["dg_number"]
                st.session_state.pdf_parsed    = True

                if parsed.get("needs_review"):
                    st.warning("⚠️ Some fields couldn't be confidently read from this PDF — double-check weight, DG#, and consignee below before calculating.")
                for note in parsed["parse_notes"]:
                    if "kg" in note.lower():
                        st.info(f"ℹ️ {note}")

# Batch queue navigator
if st.session_state.batch_mode and st.session_state.batch_queue:
    queue = st.session_state.batch_queue
    idx   = st.session_state.batch_idx
    total = len(queue)

    if idx < total:
        current = queue[idx]
        label = f"📋 Waybill {idx + 1} of {total} — {current['dg_number'] or current['ref_no']}"
        if current.get("needs_review"):
            label += "  ⚠️ NEEDS REVIEW"
        st.info(label)

        st.session_state.pdf_weight    = current["weight_lbs"]
        st.session_state.pdf_consignee = f'{current["consignee_name"]}  |  {current["consignee_address"]}'.strip(" |")
        st.session_state.pdf_ref       = current["ref_no"]
        st.session_state.pdf_dg        = current["dg_number"]
        st.session_state.pdf_parsed    = True

        if current.get("needs_review"):
            st.warning("⚠️ This waybill had one or more fields that couldn't be confidently read — double-check weight, DG#, and consignee below before calculating.")
        if current.get("parse_notes") and any("kg" in n for n in current["parse_notes"]):
            for note in current["parse_notes"]:
                if "kg" in note:
                    st.info(f"ℹ️ {note}")
    else:
        st.success("✅ All waybills in this batch have been processed.")
        st.session_state.batch_mode = False

# ---------------------- UI — CSV MANIFEST UPLOAD ----------------------
st.markdown("---")
st.subheader("📊 Or Upload CEVA Manifest CSV")
st.caption("If CEVA emailed a manifest CSV alongside the waybill PDFs, use that instead — structured columns, no parsing guesswork, and it powers automatic distance calculation below.")

csv_uploaded = st.file_uploader("Upload manifest CSV", type=["csv", "xlsx"], label_visibility="collapsed", key="csv_uploader")

if csv_uploaded is not None:
    csv_file_id = (csv_uploaded.name, csv_uploaded.size)
    if csv_file_id != st.session_state.csv_last_file_id:
        st.session_state.csv_last_file_id = csv_file_id
        csv_bytes = csv_uploaded.read()
        csv_results = parse_manifest_csv(csv_bytes)
        if csv_results and "error" in csv_results[0]:
            st.error(csv_results[0]["error"])
        else:
            # keep the untouched original so the export mirrors their existing
            # manifest format exactly, just with charge columns appended
            try:
                st.session_state.csv_original_df = pd.read_csv(io.BytesIO(csv_bytes))
            except Exception:
                st.session_state.csv_original_df = pd.read_excel(io.BytesIO(csv_bytes))
            st.session_state.csv_results_by_idx = {}
            st.session_state.csv_queue = csv_results
            st.session_state.csv_idx   = 0
            st.session_state.csv_mode  = True
            st.session_state.pdf_parsed = False
            st.session_state.batch_mode = False
            n_review = sum(1 for r in csv_results if r.get("needs_review"))
            msg = f"📊 Loaded {len(csv_results)} waybills from manifest."
            if n_review:
                msg += f" ⚠️ {n_review} flagged for review (missing weight or address)."
            st.success(msg)

if st.session_state.csv_queue:
    queue = st.session_state.csv_queue
    idx   = st.session_state.csv_idx
    total = len(queue)

    if idx < total:
        current = queue[idx]
        label = f"📊 Waybill {idx + 1} of {total} — {current['dg_number'] or current['ref_no']}"
        if current.get("needs_review"):
            label += "  ⚠️ NEEDS REVIEW"
        st.info(label)
        if current.get("needs_review"):
            st.warning("⚠️ Missing weight or consignee address on this row — double-check before calculating.")

        st.session_state.pdf_weight    = current["weight_lbs"]
        st.session_state.pdf_consignee = f'{current["consignee_name"]}  |  {current["consignee_address"]}'.strip(" |")
        st.session_state.pdf_ref       = current["ref_no"]
        st.session_state.pdf_dg        = current["dg_number"]
        st.session_state.pdf_parsed    = True

        st.caption(f"📦 {current.get('item_description','')}  ·  Service: {current.get('service_level','—')}")
        if current.get("remarks"):
            st.caption(f"📝 {current['remarks']}")

        col_a, col_b = st.columns([1, 1])
        with col_a:
            if st.button("📍 Auto-calculate distance", key=f"dist_{idx}"):
                with st.spinner("Geocoding addresses and routing..."):
                    origin, o_prec = geocode_cached(
                        current["shipper_address1"], current["shipper_city"],
                        current["shipper_state"], current["shipper_zip"],
                        st.session_state.geocode_cache,
                    )
                    dest, d_prec = geocode_cached(
                        current["consignee_address1"], current["consignee_city"],
                        current["consignee_state"], current["consignee_zip"],
                        st.session_state.geocode_cache,
                    )

                    if origin is None or dest is None:
                        st.session_state.auto_distance_km = None
                        which = []
                        if origin is None: which.append(f"shipper ({current['shipper_city']})")
                        if dest is None:   which.append(f"consignee ({current['consignee_city']})")
                        st.error(f"Couldn't geocode: {', '.join(which)}. Enter distance manually.")
                    else:
                        dist = driving_distance_km(origin, dest)
                        if dist is None:
                            st.session_state.auto_distance_km = None
                            st.error(
                                "Addresses found, but no driving route exists between them "
                                f"({current['shipper_city']} → {current['consignee_city']}). "
                                "Fly-in/remote destination, or this leg isn't driven end-to-end. Enter distance manually."
                            )
                        else:
                            st.session_state.auto_distance_km = dist
                            msg = f"Estimated driving distance: {dist} km"
                            if o_prec != "exact" or d_prec != "exact":
                                msg += f" — ⚠️ approximate (shipper: {o_prec}, consignee: {d_prec})"
                            st.success(msg + ". Always spot-check against the zone before billing.")
                            if dist > 500:
                                st.warning(
                                    f"⚠️ {dist} km exceeds the 500 km Zone 5 ceiling in this tariff — "
                                    "this shipment can't be priced with the current rate table. "
                                    "Check with CEVA which two points the billed distance is measured between."
                                )
        with col_b:
            suggested = [k for k, v in current.get("suggested_accessorials", {}).items() if v]
            if suggested:
                st.caption("💡 Remarks suggest: " + ", ".join(suggested) + " — verify before applying.")
    else:
        st.success("✅ All waybills in this manifest have been processed.")
        st.session_state.csv_mode = False

    # NEW: export the ORIGINAL manifest with two columns appended — this is
    # the format they actually asked for, not a separate log/report.
    if st.session_state.csv_original_df is not None:
        n_done = len(st.session_state.csv_results_by_idx)
        st.caption(f"{n_done} of {len(queue)} waybills calculated so far.")
        out_df = st.session_state.csv_original_df.copy()

        breakdown_columns = [
            "Distance (km)", "Zone", "Weight Bracket",
            "Base LTL ($)", "OOA Charge ($)", "Accessorials ($)",
            "Wait Time Charge ($)", "Fuel Amount ($)", "Grand Total ($)",
        ]
        for col in breakdown_columns:
            out_df[col] = out_df.index.map(
                lambda i, col=col: st.session_state.csv_results_by_idx.get(i, {}).get(col)
            )

        csv_buf = io.BytesIO()
        out_df.to_csv(csv_buf, index=False)
        csv_buf.seek(0)
        st.download_button(
            label="⬇️ Download manifest with totals",
            data=csv_buf,
            file_name=f"manifest_with_totals_{date.today().isoformat()}.csv",
            mime="text/csv",
        )
        if n_done < len(queue):
            st.caption("Rows not yet calculated will be blank in the download — you can download partway through and again at the end.")

if st.session_state.pdf_parsed:
    cols = st.columns(4)
    cols[0].metric("Weight", f'{st.session_state.pdf_weight:.3f} lbs')
    cols[1].metric("DG #",   st.session_state.pdf_dg  or "—")
    cols[2].metric("Ref #",  st.session_state.pdf_ref or "—")
    cols[3].metric("Consignee", st.session_state.pdf_consignee.split("|")[0].strip() or "—")
    if st.session_state.pdf_consignee:
        parts = st.session_state.pdf_consignee.split("|")
        if len(parts) > 1:
            st.caption(f"📍 {parts[1].strip()}")


# ---------------------- UI — SHIPMENT DETAILS ----------------------
st.markdown("---")
st.subheader("Shipment Details")

default_weight = st.session_state.pdf_weight if st.session_state.pdf_parsed else 0.0
default_ref    = st.session_state.pdf_ref    if st.session_state.pdf_parsed else ""
default_dg     = st.session_state.pdf_dg     if st.session_state.pdf_parsed else ""

col1, col2 = st.columns(2)

with col1:
    default_distance = st.session_state.auto_distance_km if st.session_state.auto_distance_km is not None else 0.0
    distance_km = st.number_input("Distance (km)", min_value=0.0, max_value=500.0, value=default_distance, step=1.0)
    if st.session_state.auto_distance_km is not None:
        st.caption("📍 Auto-calculated — double-check against the zone map before billing.")
    weight_lbs  = st.number_input("Weight (lbs)",  min_value=0.0, value=default_weight, step=0.1)

with col2:
    ref_number = st.text_input("Reference / Job #",  value=default_ref, placeholder="e.g. NLS1268763")
    dg_number  = st.text_input("DG # (barcode ref)", value=default_dg,  placeholder="e.g. DG104-1615184")
    is_ooa     = st.selectbox("Is Out-of-Area?", ["No", "Yes"], index=0) == "Yes"
    ooa_type   = st.selectbox("Out-of-Area Type", list(OOA_RATE.keys()), index=0, disabled=not is_ooa)
    ooa_km     = st.number_input("Out-of-Area KM", min_value=0.0, value=0.0, step=1.0, disabled=not is_ooa)

if st.session_state.pdf_consignee:
    st.caption(f"🚚 Consignee: {st.session_state.pdf_consignee}")

if distance_km == 0:
    st.warning("⚠️ Distance is 0 km — enter the delivery distance before calculating.")
if weight_lbs == 0:
    st.warning("⚠️ Weight is 0 lbs — enter the shipment weight before calculating.")

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
fuel_pct_input = st.number_input(
    "Fuel Surcharge % (e.g. 12 for 12%)",
    min_value=0.0,
    value=st.session_state.fca_pct,
    step=0.5
)
st.session_state.fca_pct = fuel_pct_input

if st.button("Calculate", type="primary"):
    if distance_km == 0:
        st.error("Enter the distance (km) before calculating.")
        st.stop()
    if weight_lbs == 0:
        st.error("Enter the shipment weight before calculating.")
        st.stop()

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

        st.session_state.log.append({
            "Timestamp":            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "DG #":                 dg_number,
            "Ref #":                ref_number,
            "Consignee":            st.session_state.pdf_consignee,
            "Distance (km)":        distance_km,
            "Weight (lbs)":         weight_lbs,
            "Zone":                 res["Zone"],
            "Weight Bracket":       res["Weight Bracket"],
            "Rate per lb":          res["Rate per lb"],
            "OOA Type":             ooa_type if is_ooa else "N/A",
            "OOA KM":               ooa_km if is_ooa else 0,
            "2 Man":                effective_two_man,
            "Tailgate":             effective_tailgate,
            "Inside Delivery":      flags["Inside Delivery"],
            "White Glove":          white_glove,
            "Handbomb":             handbomb,
            "Direct Drive":         direct_drive,
            "Wait Time (min)":      wait_minutes,
            "Fuel % (FCA)":         f'{fuel_pct_input:.1f}%',
            "Base LTL ($)":         res["Base LTL"],
            "OOA Charge ($)":       res["OOA charge"],
            "Accessorials ($)":     res["Accessorials (non-fuel)"],
            "Wait Time Charge ($)": res["Wait Time charge"],
            "Fuel Amount ($)":      res["Fuel amount"],
            "Grand Total ($)":      res["Grand Total"],
        })

        if st.session_state.csv_mode:
            # capture against the CURRENT row before we advance the index
            st.session_state.csv_results_by_idx[st.session_state.csv_idx] = {
                "Distance (km)":          distance_km,
                "Zone":                   res["Zone"],
                "Weight Bracket":         res["Weight Bracket"],
                "Base LTL ($)":           res["Base LTL"],
                "OOA Charge ($)":         res["OOA charge"],
                "Accessorials ($)":       res["Accessorials (non-fuel)"],
                "Wait Time Charge ($)":   res["Wait Time charge"],
                "Fuel Amount ($)":        res["Fuel amount"],
                "Grand Total ($)":        res["Grand Total"],
            }

        if st.session_state.batch_mode:
            st.session_state.batch_idx += 1
        if st.session_state.csv_mode:
            st.session_state.csv_idx += 1

        st.session_state.pdf_parsed      = False
        st.session_state.pdf_weight      = 0.0
        st.session_state.pdf_consignee   = ""
        st.session_state.pdf_ref         = ""
        st.session_state.pdf_dg          = ""
        st.session_state.auto_distance_km = None

        st.rerun()

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
