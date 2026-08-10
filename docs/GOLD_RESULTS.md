# Phase 4 — Gold layer results (2026-08-05, revised 2026-08-09)

13 Gold tables built from the 18 Silver tables.

> **Correction (2026-08-09).** This document originally said the notebook "ran clean". That was
> asserted, not verified — the SQL endpoint was used to confirm the tables existed and reconciled,
> and the notebook's own validation output was never read. Writes happen in section 8 and
> validation in section 9, so tables land whether or not the gate passes. Two checks were in fact
> **failing**, and would have gone unnoticed. Verifying an outcome by a different route is not the
> same as verifying the check that was supposed to catch it.

## Revision 2 (2026-08-09) — chronology checks, bridge keys, money rounding

Three changes after the ontology work exposed gaps:

1. **Money rounded to 2dp.** The forced `Decimal -> Double` cast produced IEEE-754 artefacts
   (`5189.200082400001`) visible in the ontology instance browser. `F.round(..., 2)` keeps the type
   `Double` and removes the noise.
2. **Document-flow bridge keys** — `PrecedingOrderLineKey` / `SubsequentOrderLineKey`, so the
   ontology can relate flow edges to `OrderLine` (relationships target the entity key).
3. **Chronology checks moved from line grain to document grain** (see below).

### Verified on disk after re-run

| check | result |
|---|---|
| flow edges total | 32,486 |
| bridge key populated (sales-order edges) | 15,932 |
| **bridge keys that fail to resolve to an order line** | **0** |
| **non-`C` rows carrying a bridge key** (guard working) | **0** |
| **money values not rounded to 2dp** | **0** |

15,932 also equals the Silver `delivery_item` count — one order→delivery edge per delivery item,
which is the expected relationship and a useful independent cross-check.

### The two failing chronology checks — diagnosis confirmed, magnitude wrong

The old checks compared `MAX(clearing)` vs `MAX(billing)` and `MAX(billing)` vs `MAX(goods issue)`
at **order-line** grain. Comparing two independent MAX aggregates cannot test a per-document
ordering, so they were invalid regardless of the counts.

Replaced with true **document-grain** invariants: a receivable is never cleared before it is
posted (AR item grain), and an invoice never predates the goods issue it bills (billing-item to
delivery grain). The line-grain counts are still printed as INFO.

**The counts, and what they actually are:** 1 line for cash-before-invoice, 9 for
invoice-before-goods-issue — 10 of 16,175, or 0.06%.

Every one of the 10 has `DeliveryCount = 2` and `IsSplitDelivery = true`:

* the 9 invoice-before-GI rows are split deliveries whose **second shipment fell after the last
  weekly billing run**, all with goods issue between 2026-06-23 and 2026-06-30;
* the single cash-before-invoice row (`4500005213-10`) has **two** billing documents — the first
  cleared 2026-06-25, the second billed 2026-06-26.

So the mechanism was diagnosed correctly. **The predicted magnitude was not.** I claimed these
were "guaranteed to occur" widely given 612 open AR items and ~12% split deliveries. In fact they
cluster entirely in the final week of the data window (data ends 2026-06-30), because the
coincidence requires a second delivery to land after the last billing run. They are **period-end
boundary effects**, not a general pattern — which means almost any other cut of this data would
have passed the old checks and hidden the fact that they were wrong.

Lesson: being right about a mechanism is not the same as being right about its scale, and the
second claim needs its own evidence.

## Row counts (verified via the SQL analytics endpoint, not the notebook's own output)

| table | rows | reconciles to |
|---|---:|---|
| `gld_dim_date` | 1,096 | 2024-01-01 → 2026-12-31 |
| `gld_dim_customer` (SCD2) | 320 | KNA1 |
| `gld_dim_material` (SCD2) | 160 | MARA |
| `gld_dim_sales_org` | 3 | DE10 / US10 / SG10 |
| `gld_dim_plant` | 3 | PL01 / VN01 / MX01 |
| `gld_fact_order_to_cash` | 16,175 | **= VBAP exactly — order-line grain preserved end to end** |
| `gld_fact_accounts_receivable` | 10,810 | = BSID 612 + BSAD 10,198 |
| `gld_fact_sales_target` | 288 | Dataflow Gen2 reference data |
| `gld_fact_document_flow` | 32,486 | VBFA |

## Gold contract — verified against the catalog, not the notebook

```sql
SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
WHERE TABLE_NAME LIKE 'gld%' AND DATA_TYPE IN ('decimal','numeric');   -- 0
SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
WHERE TABLE_NAME LIKE 'gld%' AND COLUMN_NAME NOT LIKE '[A-Za-z]%';     -- 0
```

**0 Decimal columns and 0 unsafe column names across 183 columns in 13 tables.** Both Fabric Graph
preconditions are met. This was checked by querying the catalog rather than trusting the
notebook's own gate — a gate that inspects the DataFrame it just built can pass while the artefact
on disk is wrong.

## Revision 3 (2026-08-09) — AR currency conversion

`gld_fact_accounts_receivable` now carries `AmountEUR` / `SignedAmountEUR`, converted at the rate
for the **posting month** (when the receivable came into existence), plus `FxRate` and
`FxRateMissing`. All three new validation checks passed: no row fell back to a default rate, EUR
receivables are unchanged, and non-EUR receivables actually moved.

### Open AR by company code — the reason the conversion exists

| Company code | Local ccy | AR items | Open items | Open AR (local) | Open AR (EUR) |
|---|---|---:|---:|---:|---:|
| DE10 | EUR | 4,560 | 267 | 16,414,875 | **16,414,875** |
| SG10 | SGD | 2,266 | 123 | 8,703,623 | 6,390,260 |
| US10 | USD | 3,984 | 222 | 13,524,581 | 13,552,114 |
| **TOTAL** | *(meaningless)* | 10,810 | 612 | **38,643,079** | **36,357,249** |

DE10 is identical in both columns, as it must be — it is already the group currency. The TOTAL row
reports its local currency as "USD" purely because `MAX()` picked one alphabetically, which is an
accidental but honest illustration that the column has no meaning at that level.

Both totals reconcile to the sum of their parts exactly, and AR items (10,810) and open items
(612) match the earlier Silver figures.

### My 9% estimate was wrong — the real figure is 6.3%

Naive total 38,643,079 against true 36,357,249 — an overstatement of **2,285,830, or 6.3%**, not
the ~9% I predicted.

The reason is worth keeping. I estimated using the **base** FX rates (USD 0.92, SGD 0.685), but
the generator drifts rates upward over the 24 months (+0.0035/month USD, +0.0022/month SGD). By
the end of the window USD→EUR has reached roughly parity and SGD→EUR about 0.734. **Open** AR is
concentrated at period end, precisely where the rates are highest and the conversion effect is
therefore smallest.

Lesson: estimating the impact of a currency conversion from base rates is unreliable when the
rates drift and the balance is time-concentrated. Direction and order of magnitude were right;
the number was not, and only measurement settled it.

### Minor cosmetic issue

`ORDER BY 1` places the ROLLUP `TOTAL` row alphabetically between SG10 and US10. Harmless, but a
sort key (`GROUPING(CompanyCode)`) would put it last where it belongs.

## Headline O2C metrics (non-rejected lines)

| Sales org | Order lines | Ordered €m | In-full | On-time vs requested | On-time vs confirmed | OTIF |
|---|---:|---:|---:|---:|---:|---:|
| **ALL** | 15,874 | 402.49 | 83.7% | 61.3% | 5.4% | **58.2%** |
| DE10 | 6,703 | 181.23 | 83.7% | 61.9% | 5.6% | 58.7% |
| US10 | 5,782 | 148.42 | 84.1% | 61.2% | 5.2% | 58.0% |
| SG10 | 3,389 | 72.85 | 83.3% | 60.3% | 5.5% | 57.6% |

## Correction: the on-time baseline works the *opposite* way round here

The 5.4% looked like a defect, so it was checked rather than reported. It is not a defect, and the
thing it corrects is the project's own commentary.

From the generator (`02_generate_nordhaus_sap_data.py`, lines 484-485):

```python
requested = business_days_after(od, lead + rng.randint(-2, 9))
confirmed = business_days_after(credit_release, lead)
```

There is exactly one schedule line per item (`ETENR = "0001"`), so the Silver "first schedule line"
selection is correct. `requested` carries an extra 0-9 day buffer that `confirmed` does not, so the
confirmed date lands **earlier** than the customer request and is the *stricter* yardstick.

Measured over the 14,004 lines with both dates populated:

| milestone | mean days from order |
|---|---:|
| confirmed | 20.4 |
| goods issue | 26.4 |
| requested | 34.5 |

| | lines |
|---|---:|
| confirmed **earlier** than requested | 12,504 (89%) |
| confirmed equals requested | 744 |
| confirmed **later** than requested | 756 |

Goods issue sits *between* the two milestones, which is exactly why on-time reads 61% against
requested and 5% against confirmed.

One caveat on the mechanism: the 0-9 business-day buffer accounts for roughly 5 calendar days, not
the 14 observed. The direction is certain and measured; the full decomposition of the remaining
gap has not been traced through the generator and should not be asserted without doing so.

Earlier notes in this project (and the Phase 1 DDL comment) asserted that measuring against the
confirmed date **flatters** OTIF. That is true of real SAP estates, where confirmation is pushed
*out* past the customer request. It is false for this dataset. The wording in
`06_Bronze_to_Silver.ipynb` has been corrected; the underlying lesson is unchanged and if anything
stronger — the baseline choice moves OTIF from 61.3% to 5.4%, so reporting both is what keeps the
modelling choice visible instead of buried.

## Two new Fabric traps

### 1. "Attach a lakehouse" can silently attach the **SQL analytics endpoint** instead

The notebook failed on the first run with:

```
UnsupportedOperationException
No default context found, please attach a lakehouse before running spark sql queries
with partial namespaces.
```

The Explorer *did* show `OntologyDataLH` attached. Hovering it revealed **`Type: Warehouse`** — the
OneLake catalog lists the Lakehouse and its SQL analytics endpoint under the *same name*, and the
endpoint had been picked. Its context menu offers only Refresh/Remove, with no "pin as default",
which is the tell.

Fix: remove it, re-add choosing the row with the **lakehouse** icon (lighter blue), then accept the
*Stop current session* prompt so the default can be set. The pin glyph next to the name is the
confirmation that it worked.

This is the third distinct guise of the default-lakehouse trap in this project — after the silent
local-disk write in Phase 1 and the mirrored-DB gating in Phase 3. **Always confirm the pin glyph
before running anything.**

### 2. A long-running notebook survives the browser closing

Chrome was closed mid-run; on reopening, the session reconnected in under a second and the run had
continued server-side. Do not restart a run just because the tab went away — reattach and check.
