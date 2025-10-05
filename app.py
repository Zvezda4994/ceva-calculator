import math
import pandas as pd
import streamlit as st

st.set_page_config(page_title="CEVA / NovaXpress Tariff Calculator", page_icon="📦", layout="centered")

# ---------------------- TARIFF DATA ----------------------
ZONES = [(1, 0, 50), (2, 51, 150), (3, 151, 300), (4, 301, 400), (5, 401, 500)]
MIN_CHARGE = {1:30.00, 2:45.00, 3:60.00, 4:70.00, 5:80.00}
# rows = weight brackets, cols = zone1..zone5
RATES = {
    "0-500":     (500,  [0.064, 0.120, 0.167, 0.224, 0.261]),
    "501-1000":  (1000, [0.054, 0.083, 0.111, 0.158, 0.186]),
    "1001-2000": (2000, [0.045, 0.054, 0.064, 0.101, 0.130]),
    "2001-4000": (4000, [0.036, 0.045, 0.054, 0.064, 0.073]),
    "4001+":     (float("inf"), [0.022, 0.031, 0.040, 0.051, 0.059]),
}
OOA_RATE = {"FULL": 1.50, "BACKHAUL EMPTY": 0.80, "BACKHAUL FULL": 1.00}
ACCESSORIALS = {
    "2 Man Service": 20.0,
    "Tailgate (over 200 lbs)": 10.0,
    "Inside Delivery": 20.0,
    "White Glove (residential)": 20.0,
    "Skid Handbomb (lumper)": 40.0,
    "Direct Drive (flat)": 40.0,       # this one is INCLUDED in the fuelable subtotal
}
WAIT_RATE_HR = 45.0                    # billed in 15-min increments after first 30 min free
FUEL_DEFAULT = 0.15                    # 15%

# ---------------------- HELPERS ----------------------
def zone_from_km(km: float):
    if km <= 50: return 1
    if km <= 150: return 2
    if km <= 300: return 3
    if km <= 400: return 4
    if km <= 500: return 5
    return None

def bracket_and_rate(weight_lbs: float, zone: int):
    for name, (upper, zrates) in RATES.items():
        if weight_lbs <= upper:
            return name, zrates[zone-1]
    # should never hit because last bracket is inf
    return "4001+", RATES["4001+"][1][zone-1]

def ceil_div(a, b):  # ceil(a/b)
    return math.ceil(a / b)

def calculate(
    distance_km, weight_lbs, 
    is_ooa, ooa_type, ooa_km,
    flags, wait_minutes, extra_stops,
    apply_fuel, fuel_pct_override  # override as percent (e.g., 12) or None
):
    zone = zone_from_km(distance_km)
    if zone is None:
        return {"error": "Distance exceeds Zone 5 (500 km) supported by this tariff."}

    bracket, rate_per_lb = bracket_and_rate(weight_lbs, zone)
    base = max(MIN_CHARGE[zone], rate_per_lb * weight_lbs)

    # Out-of-area
    ooa_charge = 0.0
    if is_ooa and ooa_km > 0:
        ooa_charge = OOA_RATE[ooa_type] * ooa_km

    # Accessorials (non-fuel) – all flat items add here
    acc = 0.0
    for k, v in ACCESSORIALS.items():
        if flags.get(k, False):
            acc += v

    # Wait time: first 30 min free, then 15-min increments
    wait_charge = 0.0
    if wait_minutes > 30:
        increments = ceil_div(wait_minutes - 30, 15)
        wait_charge = (WAIT_RATE_HR / 4.0) * increments
        acc += wait_charge

    # Extra stops at base rate
    extra_amt = base * max(0, int(extra_stops))

    # Fuelable = Base + OOA + Direct Drive (flat if enabled) + Extra stops
    direct_drive_flat = ACCESSORIALS["Direct Drive (flat)"] if flags.get("Direct Drive (flat)", False) else 0.0
    fuelable = base + ooa_charge + direct_drive_flat + extra_amt

    # Fuel percent (decimal)
    fuel_pct = 0.0
    if apply_fuel:
        if fuel_pct_override is None:
            fuel_pct = FUEL_DEFAULT
        else:
            fuel_pct = max(0.0, float(fuel_pct_override) / 100.0)

    fuel_amt = fuelable * fuel_pct
    total = base + ooa_charge + acc + extra_amt + fuel_amt

    breakdown = {
        "Zone": zone,
        "Weight Bracket": bracket,
        "Rate per lb": rate_per_lb,
        "Base LTL": round(base, 2),
        "OOA charge": round(ooa_charge, 2),
        "Accessorials (non-fuel)": round(acc - wait_charge, 2),
        "Wait Time charge": round(wait_charge, 2),
        "Extra Stops amount": round(extra_amt, 2),
        "Fuelable Subtotal": round(fuelable, 2),
        "Fuel % used": fuel_pct,
        "Fuel amount": round(fuel_amt, 2),
        "Grand Total": round(total, 2),
    }
    return breakdown

# ---------------------- UI ----------------------
st.title("📦 CEVA / NovaXpress Tariff Calculator")

st.subheader("Input")
col1, col2 = st.columns(2)

with col1:
    distance_km = st.number_input("Distance (km)", min_value=0.0, max_value=500.0, value=50.0, step=1.0)
    weight_lbs = st.number_input("Weight (lbs)", min_value=1.0, value=20.0, step=1.0)

with col2:
    is_ooa = st.selectbox("Is Out-of-Area?", ["No", "Yes"], index=0) == "Yes"
    ooa_type = st.selectbox("Out-of-Area Type", list(OOA_RATE.keys()), index=0, disabled=not is_ooa)
    ooa_km = st.number_input("Out-of-Area KM", min_value=0.0, value=0.0, step=1.0, disabled=not is_ooa)

st.markdown("---")
st.caption("Accessorials (toggle as needed)")
c1, c2 = st.columns(2)

with c1:
    two_man = st.toggle("2 Man Service", value=False)
    tailgate = st.toggle("Tailgate (over 200 lbs)", value=False)
    inside = st.toggle("Inside Delivery", value=False)

with c2:
    white_glove = st.toggle("White Glove (residential)", value=False)
    handbomb = st.toggle("Skid Handbomb (lumper)", value=False)
    direct_drive = st.toggle("Direct Drive (flat)", value=False)

wait_minutes = st.number_input("Wait Time (minutes)", min_value=0, value=0, step=1)
extra_stops = st.number_input("Extra Stops at Base Rate (count)", min_value=0, value=0, step=1)

st.markdown("---")
apply_fuel = st.selectbox("Apply Fuel Surcharge?", ["Yes", "No"], index=0) == "Yes"
use_default = st.selectbox("Fuel % Source", ["Default (15%)", "Override"], index=0, disabled=not apply_fuel)
fuel_override = None
if apply_fuel and use_default == "Override":
    fuel_override = st.number_input("Fuel Surcharge % (e.g., 12 for 12%)", min_value=0.0, value=15.0, step=0.5)

if st.button("Calculate", type="primary"):
    flags = {
        "2 Man Service": two_man,
        "Tailgate (over 200 lbs)": tailgate,
        "Inside Delivery": inside,
        "White Glove (residential)": white_glove,
        "Skid Handbomb (lumper)": handbomb,
        "Direct Drive (flat)": direct_drive,
    }
    res = calculate(
        distance_km, weight_lbs,
        is_ooa, ooa_type, ooa_km,
        flags, wait_minutes, extra_stops,
        apply_fuel, fuel_override if (apply_fuel and use_default=="Override") else None
    )
    if "error" in res:
        st.error(res["error"])
    else:
        st.subheader("Derived")
        left, right = st.columns(2)
        with left:
            st.metric("Zone", res["Zone"])
            st.metric("Weight Bracket", res["Weight Bracket"])
            st.metric("Rate per lb", f'{res["Rate per lb"]:.3f}')
            st.metric("Minimum Charge by Zone", f'${MIN_CHARGE[res["Zone"]]:,.2f}')
        with right:
            st.metric("Base LTL", f'${res["Base LTL"]:.2f}')
            st.metric("Fuel % used", f'{res["Fuel % used"]*100:.2f}%')
            st.metric("Fuel amount", f'${res["Fuel amount"]:.2f}')
            st.metric("Grand Total", f'${res["Grand Total"]:.2f}')

        st.write("---")
        st.subheader("Breakdown")
        df = pd.DataFrame(
            {
                "Component": [
                    "Base LTL",
                    "Out-of-Area charge",
                    "Accessorials (non-fuel)",
                    "Wait Time charge",
                    "Extra Stops amount",
                    "Fuel amount",
                ],
                "Amount ($)": [
                    res["Base LTL"],
                    res["OOA charge"],
                    res["Accessorials (non-fuel)"],
                    res["Wait Time charge"],
                    res["Extra Stops amount"],
                    res["Fuel amount"],
                ],
            }
        )
        st.dataframe(df, use_container_width=True)

# ---------------------- TEST SCENARIOS ----------------------
with st.expander("Run example test scenarios"):
    tests = [
        ("Tiny Z1 (fuel default)", dict(distance_km=50, weight_lbs=20, is_ooa=False, ooa_type="FULL", ooa_km=0,
                                        flags={}, wait_minutes=0, extra_stops=0, apply_fuel=True, fuel_pct_override=None)),
        ("Tiny Z1 (fuel off)", dict(distance_km=50, weight_lbs=20, is_ooa=False, ooa_type="FULL", ooa_km=0,
                                    flags={}, wait_minutes=0, extra_stops=0, apply_fuel=False, fuel_pct_override=None)),
        ("1,600 lbs @120 km", dict(distance_km=120, weight_lbs=1600, is_ooa=False, ooa_type="FULL", ooa_km=0,
                                   flags={}, wait_minutes=0, extra_stops=0, apply_fuel=True, fuel_pct_override=None)),
        ("3,200 lbs @350 km + OOA FULL 60 + tailgate+white+wait50 + 1 extra",
         dict(distance_km=350, weight_lbs=3200, is_ooa=True, ooa_type="FULL", ooa_km=60,
              flags={"Tailgate (over 200 lbs)":True, "White Glove (residential)":True}, wait_minutes=50, extra_stops=1,
              apply_fuel=True, fuel_pct_override=None)),
        ("5,000 lbs @410 km + direct + 2-man + handbomb",
         dict(distance_km=410, weight_lbs=5000, is_ooa=False, ooa_type="FULL", ooa_km=0,
              flags={"Direct Drive (flat)":True, "2 Man Service":True, "Skid Handbomb (lumper)":True},
              wait_minutes=0, extra_stops=0, apply_fuel=True, fuel_pct_override=None)),
        ("800 lbs @480 km + OOA Backhaul Empty 120",
         dict(distance_km=480, weight_lbs=800, is_ooa=True, ooa_type="BACKHAUL EMPTY", ooa_km=120,
              flags={}, wait_minutes=0, extra_stops=0, apply_fuel=True, fuel_pct_override=None)),
        ("Fuel override 12%", dict(distance_km=50, weight_lbs=20, is_ooa=False, ooa_type="FULL", ooa_km=0,
                                   flags={}, wait_minutes=0, extra_stops=0, apply_fuel=True, fuel_pct_override=12)),
        ("Fuel override 0%", dict(distance_km=50, weight_lbs=20, is_ooa=False, ooa_type="FULL", ooa_km=0,
                                  flags={}, wait_minutes=0, extra_stops=0, apply_fuel=True, fuel_pct_override=0)),
    ]
    rows = []
    for name, kw in tests:
        res = calculate(**kw)
        res["Scenario"] = name
        rows.append(res)
    tdf = pd.DataFrame(rows)
    st.dataframe(tdf[
        ["Scenario","Zone","Weight Bracket","Rate per lb","Base LTL","OOA charge",
         "Accessorials (non-fuel)","Wait Time charge","Extra Stops amount",
         "Fuelable Subtotal","Fuel % used","Fuel amount","Grand Total"]
    ], use_container_width=True)
