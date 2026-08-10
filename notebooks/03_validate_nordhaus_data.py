#!/usr/bin/env python3
"""
Validation harness for the Nordhaus synthetic SAP extract.

Checks referential integrity, SAP format conventions, process-logic invariants,
and that the deliberately injected anomalies actually landed at the intended
rates. Run this after every generation, before loading into Fabric.

    python validate_nordhaus_data.py --dir ./dev

Exit code 0 = all checks pass, 1 = at least one FAIL.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

RESULTS: list[tuple[str, str, str]] = []   # (status, check, detail)


def check(name: str, passed: bool, detail: str = "", warn_only: bool = False) -> None:
    status = "PASS" if passed else ("WARN" if warn_only else "FAIL")
    RESULTS.append((status, name, detail))


def load(d: Path) -> dict[str, pd.DataFrame]:
    out = {}
    for f in sorted(d.glob("*.csv")):
        out[f.stem] = pd.read_csv(f, dtype=str, keep_default_na=False)
    return out


def keyset(df: pd.DataFrame, cols: list[str]) -> set:
    return set(map(tuple, df[cols].astype(str).values))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="./dev")
    args = ap.parse_args()
    d = Path(args.dir)
    t = load(d)
    manifest = json.loads((d / "_generation_manifest.json").read_text())
    anomalies = manifest["injected_anomalies"]

    # ---------------------------------------------------------------- keys ---
    pks = {
        "VBAK": ["MANDT", "VBELN"],
        "VBAP": ["MANDT", "VBELN", "POSNR"],
        "LIKP": ["MANDT", "VBELN"],
        "LIPS": ["MANDT", "VBELN", "POSNR"],
        "VBRK": ["MANDT", "VBELN"],
        "VBRP": ["MANDT", "VBELN", "POSNR"],
        "KNA1": ["MANDT", "KUNNR"],
        "MARA": ["MANDT", "MATNR"],
        "VBFA": ["MANDT", "VBELV", "POSNV", "VBELN", "POSNN", "VBTYP_N"],
    }
    for tab, cols in pks.items():
        dup = t[tab].duplicated(subset=cols).sum()
        check(f"PK unique: {tab}", dup == 0, f"{dup} duplicate keys")

    # ------------------------------------------------- referential integrity --
    vbap_keys = keyset(t["VBAP"], ["VBELN", "POSNR"])
    lips_keys = keyset(t["LIPS"], ["VBELN", "POSNR"])
    vbak_ids = set(t["VBAK"]["VBELN"])
    likp_ids = set(t["LIKP"]["VBELN"])
    vbrk_ids = set(t["VBRK"]["VBELN"])
    kunnr_ids = set(t["KNA1"]["KUNNR"])
    matnr_ids = set(t["MARA"]["MATNR"])

    orphan = keyset(t["LIPS"], ["VGBEL", "VGPOS"]) - vbap_keys
    check("LIPS.VGBEL/VGPOS -> VBAP", not orphan, f"{len(orphan)} orphans")

    orphan = keyset(t["VBRP"], ["AUBEL", "AUPOS"]) - vbap_keys
    check("VBRP.AUBEL/AUPOS -> VBAP", not orphan, f"{len(orphan)} orphans")

    vbrp_dlv = t["VBRP"][t["VBRP"]["VGBEL"] != ""]
    orphan = keyset(vbrp_dlv, ["VGBEL", "VGPOS"]) - lips_keys
    check("VBRP.VGBEL/VGPOS -> LIPS", not orphan, f"{len(orphan)} orphans")

    check("VBAK.KUNNR -> KNA1", set(t["VBAK"]["KUNNR"]) <= kunnr_ids)
    check("VBAP.MATNR -> MARA", set(t["VBAP"]["MATNR"]) <= matnr_ids)
    check("LIKP.KUNNR -> KNA1", set(t["LIKP"]["KUNNR"]) <= kunnr_ids)
    check("VBPA.KUNNR -> KNA1", set(t["VBPA"]["KUNNR"]) <= kunnr_ids)

    all_docs = vbak_ids | likp_ids | vbrk_ids
    miss_v = set(t["VBFA"]["VBELV"]) - all_docs
    miss_n = set(t["VBFA"]["VBELN"]) - all_docs
    check("VBFA.VBELV -> existing doc", not miss_v, f"{len(miss_v)} dangling")
    check("VBFA.VBELN -> existing doc", not miss_n, f"{len(miss_n)} dangling")

    bkpf_ids = set(t["BKPF"]["BELNR"])
    check("BSID.BELNR -> BKPF", set(t["BSID"]["BELNR"]) <= bkpf_ids)
    check("BSAD.BELNR -> BKPF", set(t["BSAD"]["BELNR"]) <= bkpf_ids)

    # ----------------------------------------------- SAP format conventions ---
    def dats_ok(s: pd.Series) -> bool:
        v = s.astype(str)
        return bool(v.str.match(r"^\d{8}$").all())

    for tab, col in [("VBAK", "ERDAT"), ("VBAK", "VDATU"), ("LIKP", "WADAT_IST"),
                     ("VBRK", "FKDAT"), ("BSID", "AUGDT"), ("BSAD", "AUGDT")]:
        check(f"DATS format: {tab}.{col}", dats_ok(t[tab][col]))

    check("BSID open items have AUGDT='00000000'",
          bool((t["BSID"]["AUGDT"] == "00000000").all()),
          "open AR must use the empty-date convention, not null")
    check("BSAD cleared items have real AUGDT",
          bool((t["BSAD"]["AUGDT"] != "00000000").all()))
    check("No AR item in both BSID and BSAD",
          not (set(t["BSID"]["BELNR"]) & set(t["BSAD"]["BELNR"])))

    check("VBELN keys are 10 chars, zero-padded",
          bool(t["VBAK"]["VBELN"].str.len().eq(10).all()))
    check("POSNR keys are 6 chars",
          bool(t["VBAP"]["POSNR"].str.len().eq(6).all()))

    # --------------------------------------------------- process invariants ---
    likp_gi = t["LIKP"].set_index("VBELN")["WADAT_IST"].to_dict()
    lips_j = t["LIPS"].assign(GI=t["LIPS"]["VBELN"].map(likp_gi))
    ord_dt = t["VBAK"].set_index("VBELN")["ERDAT"].to_dict()
    lips_j["ORD"] = lips_j["VGBEL"].map(ord_dt)
    bad = lips_j[(lips_j["GI"].notna()) & (lips_j["ORD"].notna()) &
                 (lips_j["GI"] < lips_j["ORD"])]
    check("Goods issue never precedes order date", len(bad) == 0,
          f"{len(bad)} violations")

    vbrk_dt = t["VBRK"].set_index("VBELN")["FKDAT"].to_dict()
    vbrp_j = t["VBRP"][t["VBRP"]["VGBEL"] != ""].copy()
    vbrp_j["FKDAT"] = vbrp_j["VBELN"].map(vbrk_dt)
    vbrp_j["GI"] = vbrp_j["VGBEL"].map(likp_gi)
    bad = vbrp_j[(vbrp_j["GI"].notna()) & (vbrp_j["FKDAT"] < vbrp_j["GI"])]
    check("Invoice never precedes goods issue", len(bad) == 0,
          f"{len(bad)} violations")

    bsad = t["BSAD"]
    bad = bsad[bsad["AUGDT"] < bsad["BUDAT"]]
    check("Clearing never precedes posting", len(bad) == 0, f"{len(bad)} violations")

    # delivered quantity must not exceed ordered quantity per order item
    ordered = t["VBAP"].assign(q=t["VBAP"]["KWMENG"].astype(float)) \
                       .groupby(["VBELN", "POSNR"])["q"].sum()
    delivered = t["LIPS"].assign(q=t["LIPS"]["LFIMG"].astype(float)) \
                         .groupby(["VGBEL", "VGPOS"])["q"].sum()
    delivered.index.names = ["VBELN", "POSNR"]
    joined = delivered.to_frame("dlv").join(ordered.to_frame("ord"), how="inner")
    over = joined[joined["dlv"] > joined["ord"] + 0.001]
    check("Delivered qty <= ordered qty", len(over) == 0,
          f"{len(over)} over-deliveries")

    # rejected lines must never be delivered
    rejected = keyset(t["VBAP"][t["VBAP"]["ABGRU"] != ""], ["VBELN", "POSNR"])
    delivered_keys = keyset(t["LIPS"], ["VGBEL", "VGPOS"])
    check("Rejected lines are never delivered",
          not (rejected & delivered_keys),
          f"{len(rejected & delivered_keys)} rejected-but-shipped")

    # --------------------------------- many-to-many (the ontology's reason) ---
    items_per_dlv = t["LIPS"].groupby("VBELN").size()
    multi_item = int((items_per_dlv > 1).sum())
    check("Deliveries consolidate multiple order items", multi_item > 0,
          f"{multi_item:,} multi-item deliveries "
          f"({multi_item / len(items_per_dlv):.1%})")

    dlv_per_inv = t["VBRP"][t["VBRP"]["VGBEL"] != ""].groupby("VBELN")["VGBEL"].nunique()
    multi_dlv = int((dlv_per_inv > 1).sum())
    check("Invoices consolidate multiple deliveries", multi_dlv > 0,
          f"{multi_dlv:,} multi-delivery invoices "
          f"({multi_dlv / max(1, len(dlv_per_inv)):.1%})")

    dlv_per_item = t["LIPS"].groupby(["VGBEL", "VGPOS"])["VBELN"].nunique()
    split_items = int((dlv_per_item > 1).sum())
    check("Order items split across deliveries", split_items > 0,
          f"{split_items:,} split items")

    # ------------------------------------------------ anomaly rate landing ---
    n_orders = len(t["VBAK"][t["VBAK"]["AUART"] != "RE"])
    blocked = int((t["VBAK"]["CMGST"] == "B").sum())
    rate = blocked / max(1, n_orders)
    check("Credit-block rate in 5-12% band", 0.05 <= rate <= 0.12, f"{rate:.1%}")

    ret_orders = int((t["VBAK"]["AUART"] == "RE").sum())
    check("Return orders present", ret_orders > 0, f"{ret_orders:,} returns")
    check("Credit memos match return orders",
          int((t["VBRK"]["FKART"] == "G2").sum()) == ret_orders,
          f"{int((t['VBRK']['FKART'] == 'G2').sum()):,} credit memos vs "
          f"{ret_orders:,} return orders")

    neg = t["VBRK"][t["VBRK"]["FKART"] == "G2"]["NETWR"].astype(float)
    check("Credit memos carry negative value", bool((neg < 0).all()))

    changed = t["CDHDR"]["OBJECTID"].nunique()
    check("Change documents present", changed > 0,
          f"{changed:,} orders with changes ({changed / max(1, n_orders):.1%})")
    check("CDPOS has a row per CDHDR", len(t["CDPOS"]) == len(t["CDHDR"]))

    # seasonality actually landed
    vbak = t["VBAK"].copy()
    vbak["mm"] = vbak["ERDAT"].str[4:6]
    q4 = vbak[vbak["mm"].isin(["10", "11"])].shape[0] / 2
    feb = vbak[vbak["mm"] == "02"].shape[0] / 2
    check("Q4 spike visible vs February", q4 > feb * 1.4,
          f"Oct-Nov avg {q4:.0f}/mo vs Feb {feb:.0f}/mo")

    # AR open share should look like a real ageing tail
    open_share = len(t["BSID"]) / max(1, len(t["BSID"]) + len(t["BSAD"]))
    check("AR open share plausible (2-20%)", 0.02 <= open_share <= 0.20,
          f"{open_share:.1%} open", warn_only=True)


    # ------------------------------------------- column width vs DDL widths ---
    # A value longer than its destination column fails the SQL bulk load with
    # SqlBulkCopyInvalidColumnLength - an error that names no column, so it is
    # painful to diagnose in Fabric. Catch it here instead, where we can name it.
    import re as _re
    ddl_path = Path(__file__).with_name("01_SAP_Source_DDL.sql")
    if ddl_path.exists():
        ddl = _re.sub(r"/\*.*?\*/", "", ddl_path.read_text(), flags=_re.S)
        widths = {}
        for m in _re.finditer(r"CREATE TABLE sap\.(\w+)\s*\((.*?)\);", ddl, _re.S):
            tbl, body = m.group(1), m.group(2)
            body = _re.sub(r"--[^\n]*", "", body)
            for col, typ, n in _re.findall(r"^\s*(\w+)\s+(CHAR|NVARCHAR)\((\d+)\)",
                                           body, _re.M):
                widths[(tbl, col)] = int(n)

        over = []
        for (tbl, col), maxlen in sorted(widths.items()):
            if tbl not in t or col not in t[tbl].columns:
                continue
            actual = int(t[tbl][col].astype(str).str.len().max() or 0)
            if actual > maxlen:
                over.append(f"{tbl}.{col} is {f'CHAR({maxlen})'} but data reaches {actual}")
        check("no value exceeds its DDL column width", not over,
            "; ".join(over) if over else f"{len(widths)} columns checked")
    else:
        check("DDL width check", True, "DDL not found next to script - skipped",
            warn_only=True)

    # ------------------------------------------------------------- report ---
    width = max(len(c) for _, c, _ in RESULTS) + 2
    print(f"\nNordhaus data validation  —  {d.resolve()}\n" + "=" * (width + 34))
    for status, name, detail in RESULTS:
        mark = {"PASS": "  ok  ", "WARN": " warn ", "FAIL": " FAIL "}[status]
        print(f"[{mark}] {name:<{width}} {detail}")

    fails = sum(1 for s, _, _ in RESULTS if s == "FAIL")
    warns = sum(1 for s, _, _ in RESULTS if s == "WARN")
    print("=" * (width + 34))
    print(f"{len(RESULTS)} checks — {len(RESULTS) - fails - warns} passed, "
          f"{warns} warnings, {fails} failed")
    print(f"injected anomalies: {json.dumps(anomalies)}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
