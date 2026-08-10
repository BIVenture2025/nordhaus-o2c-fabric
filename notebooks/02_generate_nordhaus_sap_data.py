#!/usr/bin/env python3
"""
Nordhaus Group - synthetic SAP S/4HANA Order-to-Cash source data generator
===========================================================================
Phase 1 of the Fabric E2E O2C build.

Produces CSV extracts shaped exactly like an SAP source system, ready to load
into a Microsoft Fabric SQL Database using 01_SAP_Source_DDL.sql.

Design principles
-----------------
1. SAP conventions preserved: MANDT client, CHAR(8) DATS dates with the
   '00000000' empty convention, leading-zero keys, LOEKZ flags, SPRAS.
2. Full referential integrity: every VBFA edge points at documents that exist.
3. Deliberate process pathology. Flat, clean data makes a boring dashboard.
   Every anomaly injected here is recorded in ANOMALY_LOG and written to
   _generation_manifest.json, so the analytics can later be validated against
   known ground truth rather than vibes.

Usage
-----
    python generate_nordhaus_sap_data.py --scale 0.05 --out ./dev     # fast dev run
    python generate_nordhaus_sap_data.py --scale 1.0  --out ./full    # full volume

Only stdlib + numpy/pandas. No network access required.
"""

from __future__ import annotations

import argparse
import json
import random
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

# =============================================================================
# CONFIGURATION
# =============================================================================

MANDT = "100"
SPRAS = "E"

PERIOD_START = date(2024, 7, 1)
PERIOD_END = date(2026, 6, 30)

BASE_CUSTOMERS = 3200
BASE_MATERIALS = 1600
BASE_ORDERS = 55000

# --- Injected pathology rates (validated in the test harness) ---------------
P_CREDIT_BLOCK_BASE = 0.075     # baseline; risky customers get much more
P_DELIVERY_SPLIT = 0.12         # order item shipped across >1 delivery
P_PARTIAL_DELIVERY = 0.05       # under-delivery vs ordered quantity
P_RETURN = 0.03                 # return + credit memo
P_REJECTION = 0.02              # line rejected, never delivered
P_OPEN_BACKLOG = 0.07           # ordered but not yet delivered at period end
P_ORDER_CHANGE = 0.22           # at least one change document
P_BILLING_BLOCK = 0.03

ANOMALY_LOG: dict[str, object] = {}

# --- Organisational structure ----------------------------------------------
COMPANY_CODES = [
    # BUKRS, name, city, country, currency
    ("DE10", "Nordhaus GmbH", "Hamburg", "DE", "EUR"),
    ("US10", "Nordhaus Inc", "Charlotte", "US", "USD"),
    ("SG10", "Nordhaus Asia Pte", "Singapore", "SG", "SGD"),
]

SALES_ORGS = [
    # VKORG, BUKRS, description, currency, region weight
    ("DE10", "DE10", "Nordhaus EMEA", "EUR", 0.45),
    ("US10", "US10", "Nordhaus Americas", "USD", 0.35),
    ("SG10", "SG10", "Nordhaus APAC", "SGD", 0.20),
]

CHANNELS = [
    # VTWEG, text, weight, avg lines/order, payment-term bias
    ("10", "Retail wholesale", 0.50, 3.2, "Z030"),
    ("20", "Contract project", 0.22, 5.5, "Z060"),
    ("30", "D2C e-commerce", 0.28, 1.6, "Z014"),
]

DIVISIONS = [
    ("01", "Seating"),
    ("02", "Tables & case goods"),
    ("03", "Storage"),
    ("04", "Outdoor"),
]

PLANTS = [
    # WERKS, name, country, city, sales org, MTS/MTO bias, lead-time days
    ("PL01", "Poznan Case Goods", "PL", "Poznan", "DE10", "MTS", (3, 12)),
    ("VN01", "Binh Duong Upholstery", "VN", "Binh Duong", "SG10", "MTO", (56, 98)),
    ("MX01", "Monterrey Assembly", "MX", "Monterrey", "US10", "MTS", (5, 18)),
]

ORDER_TYPES = [
    # AUART, VBTYP, text
    ("OR", "C", "Standard Order"),
    ("ZKB", "C", "Configurable MTO"),
    ("ZPRJ", "C", "Contract Project"),
    ("RE", "H", "Returns"),
    ("G2", "K", "Credit Memo Req"),
]

PAYMENT_TERMS = {"Z014": 14, "Z030": 30, "Z045": 45, "Z060": 60, "Z090": 90}

INCOTERMS = ["EXW", "FCA", "CIF", "DAP", "DDP"]

REJECTION_REASONS = [
    ("01", "Customer cancelled"),
    ("02", "Price too high"),
    ("03", "Delivery date unacceptable"),
    ("04", "Material discontinued"),
    ("05", "Duplicate order"),
]

CREDIT_BLOCK_REASONS = ["01", "02", "03"]  # exceeded limit / overdue / new customer

# NOTE: every code here must be <= 9 chars - MATKL is CHAR(9) in SAP (MARA/VBAP/VBRP).
# Longer values fail the SQL bulk load with SqlBulkCopyInvalidColumnLength, which reports
# no column name, so it is worth keeping this constraint visible at the definition.
MATERIAL_GROUPS = {
    "01": ["SEAT-SOFA", "SEAT-CHR", "SEAT-STL", "SEAT-RECL"],
    "02": ["TBL-DINE", "TBL-COFF", "TBL-DESK", "CASE-SIDE"],
    "03": ["STO-WARD", "STO-SHELF", "STO-CAB", "STO-DRAW"],
    "04": ["OUT-LNG", "OUT-DINE", "OUT-PARA", "OUT-STOR"],
}

PRODUCT_WORDS = {
    "01": ["Aalborg", "Bergen", "Cirrus", "Drammen", "Elvind", "Fjord", "Gotland"],
    "02": ["Halden", "Ivar", "Jotun", "Kalmar", "Lund", "Malmo", "Narvik"],
    "03": ["Odense", "Prisma", "Quist", "Ronne", "Skagen", "Tromso", "Uppsala"],
    "04": ["Varde", "Wexio", "Ystad", "Zealand", "Aurora", "Borgen", "Caledon"],
}

MODIFIERS = ["", " II", " Compact", " Grande", " Lite", " Pro", " Classic", " Nordic"]


# =============================================================================
# HELPERS
# =============================================================================

def dats(d: date | None) -> str:
    """SAP DATS: CHAR(8) YYYYMMDD, empty = '00000000' (NOT null)."""
    return "00000000" if d is None else d.strftime("%Y%m%d")


def tims(rng: random.Random) -> str:
    """SAP TIMS: CHAR(6) HHMMSS, business hours."""
    return f"{rng.randint(7, 19):02d}{rng.randint(0, 59):02d}{rng.randint(0, 59):02d}"


def pad(value: int | str, width: int) -> str:
    """SAP leading-zero numeric key."""
    return str(value).rjust(width, "0")


def weighted_pick(rng: random.Random, items: list, weights: list[float]):
    return rng.choices(items, weights=weights, k=1)[0]


def seasonal_weight(d: date) -> float:
    """Q4 furniture demand spike + a February lull."""
    m = d.month
    if m in (10, 11):
        return 1.75
    if m == 12:
        return 1.35
    if m == 2:
        return 0.72
    if m in (7, 8):
        return 0.85
    return 1.0


def in_lny_shutdown(d: date) -> bool:
    """VN01 Lunar New Year production shutdown (approximate, ~2 weeks)."""
    return (d.month == 1 and d.day >= 24) or (d.month == 2 and d.day <= 10)


def business_days_after(d: date, n: int) -> date:
    """Add n calendar days, then nudge off weekends."""
    out = d + timedelta(days=int(n))
    while out.weekday() >= 5:
        out += timedelta(days=1)
    return out


# =============================================================================
# MASTER DATA
# =============================================================================

def build_org_tables() -> dict[str, pd.DataFrame]:
    t001 = pd.DataFrame(
        [(MANDT, b, n, c, ct, w, SPRAS) for b, n, c, ct, w in COMPANY_CODES],
        columns=["MANDT", "BUKRS", "BUTXT", "ORT01", "LAND1", "WAERS", "SPRAS"],
    )
    tvko = pd.DataFrame(
        [(MANDT, v, b, d, w) for v, b, d, w, _ in SALES_ORGS],
        columns=["MANDT", "VKORG", "BUKRS", "VKOOR", "WAERS"],
    )
    tvtw = pd.DataFrame(
        [(MANDT, c, t, SPRAS) for c, t, _, _, _ in CHANNELS],
        columns=["MANDT", "VTWEG", "VTEXT", "SPRAS"],
    )
    tspa = pd.DataFrame(
        [(MANDT, s, t, SPRAS) for s, t in DIVISIONS],
        columns=["MANDT", "SPART", "VTEXT", "SPRAS"],
    )
    t001w = pd.DataFrame(
        [(MANDT, w, n, c, city, vk) for w, n, c, city, vk, _, _ in PLANTS],
        columns=["MANDT", "WERKS", "NAME1", "LAND1", "ORT01", "VKORG"],
    )
    tvak = pd.DataFrame(
        [(MANDT, a, v) for a, v, _ in ORDER_TYPES],
        columns=["MANDT", "AUART", "VBTYP"],
    )
    tvakt = pd.DataFrame(
        [(MANDT, SPRAS, a, t) for a, _, t in ORDER_TYPES],
        columns=["MANDT", "SPRAS", "AUART", "BEZEI"],
    )
    tvaut = pd.DataFrame(
        [(MANDT, SPRAS, c, t) for c, t in REJECTION_REASONS],
        columns=["MANDT", "SPRAS", "ABGRU", "BEZEI"],
    )
    return {
        "T001": t001, "TVKO": tvko, "TVTW": tvtw, "TSPA": tspa, "T001W": t001w,
        "TVAK": tvak, "TVAKT": tvakt, "TVAUT": tvaut,
    }


def build_customers(rng: random.Random, n: int) -> tuple[pd.DataFrame, ...]:
    """KNA1 / KNVV / KNB1 / ADRC + an internal profile frame used downstream."""
    org_codes = [s[0] for s in SALES_ORGS]
    org_weights = [s[4] for s in SALES_ORGS]
    chan_codes = [c[0] for c in CHANNELS]
    chan_weights = [c[2] for c in CHANNELS]

    cities = {
        "DE10": [("Hamburg", "DE"), ("Munich", "DE"), ("Lyon", "FR"), ("Milan", "IT"),
                 ("Madrid", "ES"), ("Utrecht", "NL"), ("Malmo", "SE"), ("Krakow", "PL")],
        "US10": [("Charlotte", "US"), ("Austin", "US"), ("Denver", "US"),
                 ("Toronto", "CA"), ("Chicago", "US"), ("Seattle", "US")],
        "SG10": [("Singapore", "SG"), ("Sydney", "AU"), ("Tokyo", "JP"),
                 ("Seoul", "KR"), ("Auckland", "NZ"), ("Kuala Lumpur", "MY")],
    }
    prefixes = ["Nordic", "Urban", "Casa", "Habitat", "Maison", "Living", "Domus",
                "Atelier", "Studio", "Form", "Line", "Haus", "Interior", "Object"]
    suffixes = ["Furnishings", "Interiors", "Group", "Retail", "Living", "Design",
                "Concepts", "Collective", "Trading", "Partners", "Hospitality"]

    kna1, knvv, knb1, adrc, profiles = [], [], [], [], []

    for i in range(1, n + 1):
        kunnr = pad(100000 + i, 10)
        adrnr = pad(500000 + i, 10)
        vkorg = weighted_pick(rng, org_codes, org_weights)
        vtweg = weighted_pick(rng, chan_codes, chan_weights)
        bukrs = next(s[1] for s in SALES_ORGS if s[0] == vkorg)
        waers = next(s[3] for s in SALES_ORGS if s[0] == vkorg)
        city, land = rng.choice(cities[vkorg])
        name = f"{rng.choice(prefixes)} {rng.choice(suffixes)}"
        if rng.random() < 0.35:
            name += f" {rng.choice(['AB', 'GmbH', 'SARL', 'Ltd', 'Pte', 'BV', 'Inc'])}"

        # Payment terms biased by channel; contract projects run long
        zterm = next(c[4] for c in CHANNELS if c[0] == vtweg)
        if vtweg == "20" and rng.random() < 0.30:
            zterm = "Z090"

        # --- Injected: a structurally late-paying Contract cohort ------------
        late_payer = vtweg == "20" and rng.random() < 0.28
        # --- Injected: a credit-risky segment --------------------------------
        risk_class = "HIGH" if rng.random() < 0.09 else ("MED" if rng.random() < 0.25 else "LOW")

        created = PERIOD_START - timedelta(days=rng.randint(30, 2200))

        kna1.append((MANDT, kunnr, name, land, city, str(rng.randint(10000, 99999)),
                     "", "0001", rng.choice(["FURN", "RETL", "HOSP", "OFFC"]),
                     adrnr, dats(created), ""))
        adrc.append((MANDT, adrnr, name, city, str(rng.randint(10000, 99999)),
                     f"{rng.randint(1, 200)} {rng.choice(['Main', 'Park', 'Nord', 'Hafen'])} St",
                     land, ""))
        for spart, _ in DIVISIONS:
            knvv.append((MANDT, kunnr, vkorg, vtweg, spart,
                         rng.choice(["01", "02", "03"]),
                         f"{vkorg[:2]}{rng.randint(100, 999)}",
                         rng.choice(["01", "02"]), rng.choice(INCOTERMS),
                         zterm, rng.choice(["01", "02", "03"]), ""))
        knb1.append((MANDT, kunnr, bukrs, "0000140000", zterm, "TC", ""))

        profiles.append({
            "KUNNR": kunnr, "VKORG": vkorg, "VTWEG": vtweg, "BUKRS": bukrs,
            "WAERS": waers, "ZTERM": zterm, "LATE_PAYER": late_payer,
            "RISK": risk_class, "ADRNR": adrnr,
        })

    return (
        pd.DataFrame(kna1, columns=["MANDT", "KUNNR", "NAME1", "LAND1", "ORT01", "PSTLZ",
                                    "REGIO", "KTOKD", "BRSCH", "ADRNR", "ERDAT", "LOEVM"]),
        pd.DataFrame(knvv, columns=["MANDT", "KUNNR", "VKORG", "VTWEG", "SPART", "KDGRP",
                                    "BZIRK", "KONDA", "INCO1", "ZTERM", "VSBED", "LOEVM"]),
        pd.DataFrame(knb1, columns=["MANDT", "KUNNR", "BUKRS", "AKONT", "ZTERM",
                                    "ZWELS", "LOEVM"]),
        pd.DataFrame(adrc, columns=["CLIENT", "ADDRNUMBER", "NAME1", "CITY1", "POST_CODE1",
                                    "STREET", "COUNTRY", "REGION"]),
        pd.DataFrame(profiles),
    )


def build_materials(rng: random.Random, n: int) -> tuple[pd.DataFrame, ...]:
    mara, marc, mvke, makt, profiles = [], [], [], [], []
    div_codes = [d[0] for d in DIVISIONS]

    for i in range(1, n + 1):
        matnr = pad(f"FG{200000 + i}", 18)
        spart = rng.choice(div_codes)
        matkl = rng.choice(MATERIAL_GROUPS[spart])
        word = rng.choice(PRODUCT_WORDS[spart])
        desc = f"{word}{rng.choice(MODIFIERS)} {matkl.split('-')[1].title()}"

        # Outdoor and glass-topped tables are fragile -> higher return rate later
        fragile = spart == "04" or (spart == "02" and rng.random() < 0.30)

        # Upholstered seating is MTO out of VN01; the rest is MTS
        if spart == "01" and rng.random() < 0.62:
            werks, strgr, beskz = "VN01", "20", "E"      # make-to-order
        else:
            werks = rng.choice(["PL01", "MX01"])
            strgr, beskz = "10", "F"                      # make-to-stock

        lead_lo, lead_hi = next(p[6] for p in PLANTS if p[0] == werks)
        plifz = rng.randint(lead_lo, lead_hi)

        weight = round(rng.uniform(4, 85), 2)
        volume = round(weight * rng.uniform(0.02, 0.09), 3)
        cost = round(rng.uniform(45, 900), 2)
        list_price = round(cost * rng.uniform(1.9, 3.4), 2)

        mara.append((MANDT, matnr, "FERT", matkl, "EA", weight * 1.08, weight, "KG",
                     volume, "M3", f"{spart}{matkl[:4]}", dats(PERIOD_START), ""))
        marc.append((MANDT, matnr, werks, "PD", beskz, plifz, rng.randint(1, 3), strgr, ""))
        for vkorg, _, _, _, _ in SALES_ORGS:
            for vtweg, _, _, _, _ in CHANNELS:
                mvke.append((MANDT, matnr, vkorg, vtweg, "NORM", "01",
                             f"{spart}{matkl[:4]}", ""))
        makt.append((MANDT, matnr, SPRAS, desc))

        profiles.append({
            "MATNR": matnr, "SPART": spart, "MATKL": matkl, "WERKS": werks,
            "STRGR": strgr, "PLIFZ": plifz, "COST": cost, "LIST": list_price,
            "WEIGHT": weight, "FRAGILE": fragile, "DESC": desc,
        })

    return (
        pd.DataFrame(mara, columns=["MANDT", "MATNR", "MTART", "MATKL", "MEINS", "BRGEW",
                                    "NTGEW", "GEWEI", "VOLUM", "VOLEH", "PRDHA",
                                    "ERSDA", "LVORM"]),
        pd.DataFrame(marc, columns=["MANDT", "MATNR", "WERKS", "DISMM", "BESKZ", "PLIFZ",
                                    "WEBAZ", "STRGR", "LVORM"]),
        pd.DataFrame(mvke, columns=["MANDT", "MATNR", "VKORG", "VTWEG", "MTPOS",
                                    "KONDM", "PRODH", "LVORM"]),
        pd.DataFrame(makt, columns=["MANDT", "MATNR", "SPRAS", "MAKTX"]),
        pd.DataFrame(profiles),
    )


# =============================================================================
# TRANSACTIONAL FLOW
# =============================================================================

def build_order_dates(rng: random.Random, n_orders: int) -> list[date]:
    """Order dates weighted by seasonality across the period."""
    days = (PERIOD_END - PERIOD_START).days
    all_days = [PERIOD_START + timedelta(days=i) for i in range(days + 1)]
    weights = []
    for d in all_days:
        w = seasonal_weight(d)
        if d.weekday() >= 5:
            w *= 0.15                      # weekend order entry is rare
        weights.append(w)
    return rng.choices(all_days, weights=weights, k=n_orders)


def generate_transactions(rng: random.Random, cust: pd.DataFrame,
                          mat: pd.DataFrame, n_orders: int) -> dict[str, pd.DataFrame]:
    """The core O2C chain. Returns every transactional table."""
    cust_recs = cust.to_dict("records")
    mat_by_div: dict[str, list] = {d: [] for d, _ in DIVISIONS}
    for m in mat.to_dict("records"):
        mat_by_div[m["SPART"]].append(m)

    vbak, vbap, vbep, vbkd, vbpa = [], [], [], [], []
    likp, lips = [], []
    vbrk, vbrp, prcd = [], [], []
    bkpf, bsid, bsad = [], [], []
    vbfa = []
    cdhdr, cdpos = [], []

    order_dates = sorted(build_order_dates(rng, n_orders))

    seq = {"order": 0, "delivery": 0, "billing": 0, "acct": 0, "change": 0}
    counters = {k: 0 for k in
                ["credit_blocked", "split", "partial", "returned", "rejected",
                 "open_backlog", "changed", "late_paid", "billing_blocked"]}

    for od in order_dates:
        seq["order"] += 1
        vbeln = pad(4500000000 + seq["order"], 10)
        c = rng.choice(cust_recs)
        vkorg, vtweg, waers = c["VKORG"], c["VTWEG"], c["WAERS"]
        chan = next(ch for ch in CHANNELS if ch[0] == vtweg)

        # ---- order type -----------------------------------------------------
        if vtweg == "20":
            auart = "ZPRJ"
        elif rng.random() < 0.28:
            auart = "ZKB"
        else:
            auart = "OR"

        n_lines = max(1, int(rng.gauss(chan[3], chan[3] * 0.45)))
        n_lines = min(n_lines, 12)

        # ---- credit check ---------------------------------------------------
        risk_mult = {"HIGH": 4.5, "MED": 1.6, "LOW": 0.55}[c["RISK"]]
        credit_blocked = rng.random() < min(0.65, P_CREDIT_BLOCK_BASE * risk_mult)
        if credit_blocked:
            block_days = int(abs(rng.gauss(6, 5))) + 1
            counters["credit_blocked"] += 1
            cmgst = "B"
        else:
            block_days = 0
            cmgst = "A"
        credit_release = business_days_after(od, block_days)

        billing_blocked = rng.random() < P_BILLING_BLOCK
        if billing_blocked:
            counters["billing_blocked"] += 1

        # ---- pick materials -------------------------------------------------
        spart = rng.choice([d[0] for d in DIVISIONS])
        pool = mat_by_div[spart]
        lines = [rng.choice(pool) for _ in range(n_lines)]

        header_net = 0.0
        max_req_date = od
        any_delivered = False
        any_open = False
        shipments: list[dict] = []

        # ~15% of orders ship somewhere other than the sold-to party
        ship_to = c["KUNNR"]
        if rng.random() < 0.15:
            ship_to = rng.choice(cust_recs)["KUNNR"]

        for li, m in enumerate(lines, start=1):
            posnr = pad(li * 10, 6)
            qty = float(max(1, int(abs(rng.gauss(8, 9)))))
            if vtweg == "30":
                qty = float(rng.randint(1, 4))       # D2C buys small
            elif vtweg == "20":
                qty = float(rng.randint(10, 120))    # projects buy big

            list_val = round(m["LIST"] * qty, 2)
            # Contract channel discounts hardest -> the revenue-leakage finding
            disc_pct = {"10": rng.uniform(0.05, 0.18),
                        "20": rng.uniform(0.15, 0.38),
                        "30": rng.uniform(0.00, 0.10)}[vtweg]
            net_price = round(m["LIST"] * (1 - disc_pct), 2)
            net_val = round(net_price * qty, 2)
            cost_val = round(m["COST"] * qty, 2)
            header_net += net_val

            # ---- dates ------------------------------------------------------
            lead = m["PLIFZ"]
            if m["WERKS"] == "VN01" and in_lny_shutdown(od):
                lead += rng.randint(10, 21)          # LNY shutdown
            requested = business_days_after(od, lead + rng.randint(-2, 9))
            confirmed = business_days_after(credit_release, lead)
            max_req_date = max(max_req_date, requested)

            rejected = rng.random() < P_REJECTION
            abgru = rng.choice([r[0] for r in REJECTION_REASONS]) if rejected else ""
            if rejected:
                counters["rejected"] += 1

            vbap.append((MANDT, vbeln, posnr, m["MATNR"], m["DESC"][:40], "TAN",
                         m["WERKS"], "0001", qty, "EA", net_val, waers, net_price,
                         list_val, cost_val, m["MATKL"], abgru,
                         "A" if rejected else "", "A" if rejected else ""))
            vbep.append((MANDT, vbeln, posnr, "0001", dats(confirmed), qty,
                         0.0 if rejected else qty, qty, "CP"))
            prcd_key = pad(seq["order"], 10)
            prcd.append((MANDT, prcd_key, posnr, "010", "01", "PR00",
                         m["LIST"], list_val, waers, "C"))
            prcd.append((MANDT, prcd_key, posnr, "100", "01", "ZDIS",
                         round(-disc_pct * 100, 2), round(net_val - list_val, 2),
                         waers, "A"))

            if rejected:
                continue

            # ---- open backlog ------------------------------------------------
            gi_target = business_days_after(confirmed, rng.randint(0, 6))
            if gi_target > PERIOD_END or rng.random() < P_OPEN_BACKLOG:
                counters["open_backlog"] += 1
                any_open = True
                continue

            # ---- plan shipments (emitted after the line loop so several items
            #      can share one delivery, and several deliveries can share one
            #      invoice -- the many-to-many the ontology exists to model)
            split = rng.random() < (P_DELIVERY_SPLIT * (1.8 if m["STRGR"] == "20" else 1.0))
            partial = rng.random() < P_PARTIAL_DELIVERY
            if split:
                counters["split"] += 1
                portions = [round(qty * 0.6, 3), round(qty * 0.4, 3)]
            else:
                portions = [qty]
            if partial:
                counters["partial"] += 1
                portions[-1] = round(portions[-1] * rng.uniform(0.55, 0.9), 3)

            for pi, portion in enumerate(portions):
                slip_base = 1.9 if gi_target.month in (10, 11, 12) else 0.4
                slip = max(0, int(rng.gauss(slip_base, 3.2)))
                gi_date = business_days_after(gi_target + timedelta(days=pi * 6), slip)
                if gi_date > PERIOD_END:
                    counters["open_backlog"] += 1
                    any_open = True
                    continue
                shipments.append({"posnr": posnr, "mat": m, "qty": portion,
                                  "gi": gi_date, "price": net_price, "prcd": prcd_key})

        # ---- emit deliveries: shipments leaving on the same day travel together
        #      on ONE delivery document (multi-item LIKP/LIPS)
        by_gi: dict = {}
        for s in shipments:
            by_gi.setdefault(s["gi"], []).append(s)

        deliveries = []
        for gi_date, group in sorted(by_gi.items()):
            any_delivered = True
            seq["delivery"] += 1
            dlv = pad(8000000000 + seq["delivery"], 10)
            pick_date = business_days_after(gi_date, -1)
            tot_wt = sum(s["mat"]["WEIGHT"] * s["qty"] for s in group)
            tot_qty = sum(s["qty"] for s in group)
            likp.append((MANDT, dlv, dats(gi_date - timedelta(days=1)), tims(rng),
                         "LF", "J", vkorg, ship_to, c["KUNNR"],
                         dats(gi_date), dats(gi_date), dats(gi_date),
                         dats(pick_date), dats(pick_date), round(tot_wt, 3), "KG",
                         max(1, int(tot_qty / 4)), "01",
                         f"R{rng.randint(1, 9):05d}", "C", "C"))
            items = []
            for di, s in enumerate(group, start=1):
                dpos = pad(di * 10, 6)
                dval = round(s["price"] * s["qty"], 2)
                lips.append((MANDT, dlv, dpos, s["mat"]["MATNR"], s["mat"]["WERKS"],
                             "0001", s["qty"], "EA", "EA", vbeln, s["posnr"],
                             f"B{rng.randint(100000, 999999)}", dval, waers))
                vbfa.append((MANDT, vbeln, s["posnr"], dlv, dpos, "J", "C",
                             s["qty"], "EA", dval, waers, dats(gi_date), "+"))
                items.append((dpos, s, dval))
            deliveries.append({"dlv": dlv, "gi": gi_date, "items": items})

        # ---- emit invoices: SAP's billing due list runs weekly, so deliveries
        #      falling in the same run consolidate onto ONE invoice
        by_bill: dict = {}
        for d in deliveries:
            lag = rng.randint(0, 3) + (rng.randint(5, 20) if billing_blocked else 0)
            raw = business_days_after(d["gi"], lag)
            run = raw + timedelta(days=(4 - raw.weekday()) % 7)   # next Friday
            by_bill.setdefault(run, []).append(d)

        for fkdat, group in sorted(by_bill.items()):
            if fkdat > PERIOD_END:
                continue
            seq["billing"] += 1
            bill = pad(9000000000 + seq["billing"], 10)
            seq["acct"] += 1
            belnr = pad(1900000000 + seq["acct"], 10)
            gjahr = str(fkdat.year)
            bill_net, bi = 0.0, 0
            for d in group:
                for dpos, s, dval in d["items"]:
                    bi += 1
                    bpos = pad(bi * 10, 6)
                    bill_net += dval
                    vbrp.append((MANDT, bill, bpos, s["mat"]["MATNR"],
                                 s["mat"]["DESC"][:40], s["mat"]["WERKS"], s["qty"],
                                 "EA", dval, round(s["mat"]["COST"] * s["qty"], 2),
                                 round(s["mat"]["LIST"] * s["qty"], 2),
                                 s["mat"]["MATKL"], vbeln, s["posnr"], d["dlv"],
                                 dpos, s["prcd"]))
                    vbfa.append((MANDT, d["dlv"], dpos, bill, bpos, "M", "J",
                                 s["qty"], "EA", dval, waers, dats(fkdat), "+"))
            bill_net = round(bill_net, 2)
            tax = round(bill_net * 0.19, 2)
            vbrk.append((MANDT, bill, "F2", "M", dats(fkdat), dats(fkdat),
                         c["BUKRS"], vkorg, c["KUNNR"], c["KUNNR"], bill_net,
                         tax, waers, 1.0, c["ZTERM"], dats(fkdat), "C", "", belnr))
            bkpf.append((MANDT, c["BUKRS"], belnr, gjahr, "RV", dats(fkdat),
                         dats(fkdat), dats(fkdat), waers, 1.0, bill))

            # ---- AR / cash application -------------------------------------
            terms_days = PAYMENT_TERMS[c["ZTERM"]]
            due = fkdat + timedelta(days=terms_days)
            if c["LATE_PAYER"]:
                delay = int(abs(rng.gauss(26, 18)))
                counters["late_paid"] += 1
            else:
                delay = int(rng.gauss(2, 9))
            pay_date = due + timedelta(days=max(-8, delay))
            ar_row = [MANDT, c["BUKRS"], c["KUNNR"], "", "", "", "",
                      bill, gjahr, belnr, "001", dats(fkdat), dats(fkdat),
                      waers, bill, "RV", "S", bill_net + tax, bill_net + tax,
                      dats(fkdat), terms_days, c["ZTERM"], bill]
            if pay_date <= PERIOD_END:
                seq["acct"] += 1
                clr = pad(1900000000 + seq["acct"], 10)
                ar_row[5], ar_row[6] = dats(pay_date), clr
                bsad.append(tuple(ar_row))
                bkpf.append((MANDT, c["BUKRS"], clr, str(pay_date.year), "DZ",
                             dats(pay_date), dats(pay_date), dats(pay_date),
                             waers, 1.0, clr))
            else:
                ar_row[5], ar_row[6] = "00000000", ""    # open item convention
                bsid.append(tuple(ar_row))

            # ---- returns + credit memo -------------------------------------
            first_s = group[0]["items"][0][1]
            if rng.random() < (P_RETURN * (2.1 if first_s["mat"]["FRAGILE"] else 1.0)):
                ret_date = business_days_after(fkdat, rng.randint(5, 45))
                if ret_date <= PERIOD_END:
                    counters["returned"] += 1
                    mm = first_s["mat"]
                    seq["order"] += 1
                    ret = pad(4500000000 + seq["order"], 10)
                    ret_qty = round(first_s["qty"] * rng.choice([0.25, 0.5, 1.0]), 3)
                    ret_val = round(first_s["price"] * ret_qty, 2)
                    vbak.append((MANDT, ret, dats(ret_date), tims(rng), "SYNTH",
                                 dats(ret_date), "RE", "H", vkorg, vtweg, spart,
                                 "001", "0001", c["KUNNR"], f"RET-{bill[-6:]}",
                                 dats(ret_date), ret_val, waers, dats(ret_date),
                                 "", "", "A", "C", "C", "C"))
                    vbap.append((MANDT, ret, "000010", mm["MATNR"], mm["DESC"][:40],
                                 "REN", mm["WERKS"], "0001", ret_qty, "EA",
                                 ret_val, waers, first_s["price"],
                                 round(mm["LIST"] * ret_qty, 2),
                                 round(mm["COST"] * ret_qty, 2), mm["MATKL"],
                                 "", "C", "C"))
                    vbfa.append((MANDT, bill, "000010", ret, "000010", "H", "M",
                                 ret_qty, "EA", ret_val, waers, dats(ret_date), "-"))
                    seq["billing"] += 1
                    cm = pad(9000000000 + seq["billing"], 10)
                    vbrk.append((MANDT, cm, "G2", "O", dats(ret_date), dats(ret_date),
                                 c["BUKRS"], vkorg, c["KUNNR"], c["KUNNR"],
                                 -ret_val, round(-ret_val * 0.19, 2), waers, 1.0,
                                 c["ZTERM"], dats(ret_date), "C", "", ""))
                    vbrp.append((MANDT, cm, "000010", mm["MATNR"], mm["DESC"][:40],
                                 mm["WERKS"], -ret_qty, "EA", -ret_val,
                                 round(-mm["COST"] * ret_qty, 2),
                                 round(-mm["LIST"] * ret_qty, 2), mm["MATKL"],
                                 ret, "000010", "", "", first_s["prcd"]))
                    vbfa.append((MANDT, ret, "000010", cm, "000010", "O", "H",
                                 ret_qty, "EA", -ret_val, waers, dats(ret_date), "-"))

        # ---- order header ------------------------------------------------------
        lfstk = "C" if any_delivered and not any_open else ("B" if any_delivered else "A")
        vbak.append((MANDT, vbeln, dats(od), tims(rng), f"USR{rng.randint(1, 40):03d}",
                     dats(od), auart, "C", vkorg, vtweg, spart,
                     f"{rng.randint(1, 9):03d}", f"{vkorg[:2]}{rng.randint(1, 9):02d}",
                     c["KUNNR"], f"PO-{rng.randint(100000, 999999)}", dats(od),
                     round(header_net, 2), waers, dats(max_req_date),
                     rng.choice(CREDIT_BLOCK_REASONS) if credit_blocked else "",
                     "08" if billing_blocked else "", cmgst,
                     "C" if lfstk == "C" else "B", lfstk, lfstk))
        vbkd.append((MANDT, vbeln, "000000", c["ZTERM"], rng.choice(INCOTERMS),
                     "Delivered", f"PO-{rng.randint(100000, 999999)}", 1.0))
        for parvw in ["AG", "WE", "RE", "RG"]:
            # ~15% of orders ship somewhere other than the sold-to party
            partner = c["KUNNR"]
            if parvw == "WE" and rng.random() < 0.15:
                partner = rng.choice(cust_recs)["KUNNR"]
            vbpa.append((MANDT, vbeln, "000000", parvw, partner, c["ADRNR"]))

        # ---- change documents ---------------------------------------------------
        if rng.random() < P_ORDER_CHANGE:
            counters["changed"] += 1
            for _ in range(rng.choices([1, 2, 3], weights=[0.6, 0.3, 0.1], k=1)[0]):
                seq["change"] += 1
                chg = pad(seq["change"], 10)
                cdate = business_days_after(od, rng.randint(1, 30))
                if cdate > PERIOD_END:
                    continue
                field = rng.choices(["VDATU", "KWMENG", "NETPR"],
                                    weights=[0.55, 0.30, 0.15], k=1)[0]
                cdhdr.append((MANDT, "VERKBELEG", vbeln, chg,
                              f"USR{rng.randint(1, 40):03d}", dats(cdate), tims(rng),
                              "VA02", "U"))
                if field == "VDATU":
                    old, new = max_req_date, max_req_date + timedelta(days=rng.randint(3, 28))
                    tab, key, ov, nv = "VBAK", vbeln, dats(old), dats(new)
                else:
                    tab, key = "VBAP", f"{vbeln}000010"
                    base = round(rng.uniform(5, 60), 2)
                    ov, nv = str(base), str(round(base * rng.uniform(0.7, 1.4), 2))
                cdpos.append((MANDT, "VERKBELEG", vbeln, chg, tab, key, field,
                              "U", ov, nv))

    ANOMALY_LOG.update(counters)

    cols = {
        "VBAK": ["MANDT", "VBELN", "ERDAT", "ERZET", "ERNAM", "AUDAT", "AUART", "VBTYP",
                 "VKORG", "VTWEG", "SPART", "VKGRP", "VKBUR", "KUNNR", "BSTNK", "BSTDK",
                 "NETWR", "WAERK", "VDATU", "LIFSK", "FAKSK", "CMGST", "GBSTK", "LFSTK",
                 "FKSTK"],
        "VBAP": ["MANDT", "VBELN", "POSNR", "MATNR", "ARKTX", "PSTYV", "WERKS", "LGORT",
                 "KWMENG", "VRKME", "NETWR", "WAERK", "NETPR", "KZWI1", "WAVWR", "MATKL",
                 "ABGRU", "LFSTA", "FKSTA"],
        "VBEP": ["MANDT", "VBELN", "POSNR", "ETENR", "EDATU", "WMENG", "BMENG", "LMENG",
                 "ETTYP"],
        "VBKD": ["MANDT", "VBELN", "POSNR", "ZTERM", "INCO1", "INCO2", "BSTKD", "KURSK"],
        "VBPA": ["MANDT", "VBELN", "POSNR", "PARVW", "KUNNR", "ADRNR"],
        "LIKP": ["MANDT", "VBELN", "ERDAT", "ERZET", "LFART", "VBTYP", "VKORG", "KUNNR",
                 "KUNAG", "LFDAT", "WADAT", "WADAT_IST", "KODAT", "LDDAT", "BTGEW",
                 "GEWEI", "ANZPK", "VSBED", "ROUTE", "WBSTK", "LVSTK"],
        "LIPS": ["MANDT", "VBELN", "POSNR", "MATNR", "WERKS", "LGORT", "LFIMG", "MEINS",
                 "VRKME", "VGBEL", "VGPOS", "CHARG", "NETWR", "WAERK"],
        "VBRK": ["MANDT", "VBELN", "FKART", "VBTYP", "FKDAT", "ERDAT", "BUKRS", "VKORG",
                 "KUNRG", "KUNAG", "NETWR", "MWSBK", "WAERK", "KURRF", "ZTERM", "VALDT",
                 "RFBSK", "FKSTO", "BELNR"],
        "VBRP": ["MANDT", "VBELN", "POSNR", "MATNR", "ARKTX", "WERKS", "FKIMG", "VRKME",
                 "NETWR", "WAVWR", "KZWI1", "MATKL", "AUBEL", "AUPOS", "VGBEL", "VGPOS",
                 "KNUMV"],
        "PRCD_ELEMENTS": ["CLIENT", "KNUMV", "KPOSN", "STUNR", "ZAEHK", "KSCHL", "KBETR",
                          "KWERT", "WAERS", "KRECH"],
        "BKPF": ["MANDT", "BUKRS", "BELNR", "GJAHR", "BLART", "BLDAT", "BUDAT", "CPUDT",
                 "WAERS", "KURSF", "AWKEY"],
        "VBFA": ["MANDT", "VBELV", "POSNV", "VBELN", "POSNN", "VBTYP_N", "VBTYP_V",
                 "RFMNG", "MEINS", "RFWRT", "WAERS", "ERDAT", "PLMIN"],
        "CDHDR": ["MANDANT", "OBJECTCLAS", "OBJECTID", "CHANGENR", "USERNAME", "UDATE",
                  "UTIME", "TCODE", "CHANGE_IND"],
        "CDPOS": ["MANDANT", "OBJECTCLAS", "OBJECTID", "CHANGENR", "TABNAME", "TABKEY",
                  "FNAME", "CHNGIND", "VALUE_OLD", "VALUE_NEW"],
    }
    ar_cols = ["MANDT", "BUKRS", "KUNNR", "UMSKS", "UMSKZ", "AUGDT", "AUGBL", "ZUONR",
               "GJAHR", "BELNR", "BUZEI", "BUDAT", "BLDAT", "WAERS", "XBLNR", "BLART",
               "SHKZG", "DMBTR", "WRBTR", "ZFBDT", "ZBD1T", "ZTERM", "REBZG"]

    return {
        "VBAK": pd.DataFrame(vbak, columns=cols["VBAK"]),
        "VBAP": pd.DataFrame(vbap, columns=cols["VBAP"]),
        "VBEP": pd.DataFrame(vbep, columns=cols["VBEP"]),
        "VBKD": pd.DataFrame(vbkd, columns=cols["VBKD"]),
        "VBPA": pd.DataFrame(vbpa, columns=cols["VBPA"]),
        "LIKP": pd.DataFrame(likp, columns=cols["LIKP"]),
        "LIPS": pd.DataFrame(lips, columns=cols["LIPS"]),
        "VBRK": pd.DataFrame(vbrk, columns=cols["VBRK"]),
        "VBRP": pd.DataFrame(vbrp, columns=cols["VBRP"]),
        "PRCD_ELEMENTS": pd.DataFrame(prcd, columns=cols["PRCD_ELEMENTS"]),
        "BKPF": pd.DataFrame(bkpf, columns=cols["BKPF"]),
        "BSID": pd.DataFrame(bsid, columns=ar_cols),
        "BSAD": pd.DataFrame(bsad, columns=ar_cols),
        "VBFA": pd.DataFrame(vbfa, columns=cols["VBFA"]),
        "CDHDR": pd.DataFrame(cdhdr, columns=cols["CDHDR"]),
        "CDPOS": pd.DataFrame(cdpos, columns=cols["CDPOS"]),
    }


# =============================================================================
# MAIN
# =============================================================================

def main() -> None:
    ap = argparse.ArgumentParser(description="Generate synthetic SAP O2C data")
    ap.add_argument("--scale", type=float, default=1.0,
                    help="volume multiplier (0.05 for a fast dev run)")
    ap.add_argument("--out", type=str, default="./out", help="output directory")
    ap.add_argument("--seed", type=int, default=20260803)
    args = ap.parse_args()

    rng = random.Random(args.seed)
    np.random.seed(args.seed)

    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)

    n_cust = max(50, int(BASE_CUSTOMERS * args.scale))
    n_mat = max(40, int(BASE_MATERIALS * args.scale))
    n_ord = max(200, int(BASE_ORDERS * args.scale))

    print(f"Nordhaus SAP generator | scale={args.scale} seed={args.seed}")
    print(f"  customers={n_cust:,}  materials={n_mat:,}  orders={n_ord:,}")

    tables: dict[str, pd.DataFrame] = {}
    tables.update(build_org_tables())

    kna1, knvv, knb1, adrc, cust_prof = build_customers(rng, n_cust)
    tables.update({"KNA1": kna1, "KNVV": knvv, "KNB1": knb1, "ADRC": adrc})

    mara, marc, mvke, makt, mat_prof = build_materials(rng, n_mat)
    tables.update({"MARA": mara, "MARC": marc, "MVKE": mvke, "MAKT": makt})

    print("  generating transactional flow...")
    tables.update(generate_transactions(rng, cust_prof, mat_prof, n_ord))

    manifest = {"seed": args.seed, "scale": args.scale,
                "period": [str(PERIOD_START), str(PERIOD_END)],
                "row_counts": {}, "injected_anomalies": ANOMALY_LOG}

    for name, df in sorted(tables.items()):
        path = outdir / f"{name}.csv"
        df.to_csv(path, index=False, encoding="utf-8")
        manifest["row_counts"][name] = len(df)
        print(f"    {name:<16} {len(df):>10,} rows")

    (outdir / "_generation_manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8")

    total = sum(manifest["row_counts"].values())
    print(f"\n  TOTAL {total:,} rows across {len(tables)} tables -> {outdir}")
    print("  injected anomalies:", json.dumps(ANOMALY_LOG))


if __name__ == "__main__":
    main()
