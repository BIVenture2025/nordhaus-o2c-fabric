# Project 23 — status scan, 2026-08-10

Plan (`Nordhaus_Fabric_Implementation_Plan.md`) versus what actually exists in
**Nordhaus-O2C-Dev**. Graph edges are out of scope as of this date — see the finding at the end.

---

## ⏳ The one time-boxed item: the trial expires in ~8 days

The Fabric banner reads **"Trials activated: 8 days left"**, so capacity ends around
**2026-08-17/18**. Everything below is optional. This is not.

Plan §Phase 7 already called for *"a full export before trial expiry"* and it has not been done.
When the trial lapses the workspace becomes inaccessible and every artefact — notebooks, dataflow,
pipeline, semantic model, ontology, Gold tables — goes with it. What survives today is only what is
in this folder: the notebooks, the specs, and the report definition.

**Minimum viable export, in priority order:**

1. **Screenshots of every report page** at full size — the portfolio evidence, and the only thing
   that survives with zero dependencies.
2. **Screenshots of the Fabric artefacts**: workspace item list, pipeline canvas, Dataflow Gen2
   query editor, ontology entity diagram, model relationship view, a notebook mid-run.
3. **The Dataflow Gen2 M code** — copy the Advanced Editor text into `Phase2/`. It exists nowhere
   on disk right now; `DATAFLOW_GEN2_SPEC.md` describes it but does not fully reproduce it.
4. **Gold tables to Parquet/CSV** — `_ENGINE`-side, so a future rebuild does not need the generator
   to run again. `gld_fact_order_to_cash` (16,175), `gld_fact_accounts_receivable` (10,810) and the
   six Phase 8 tables are the ones worth keeping.
5. **The `.pbix`** — already published; download a local copy too.
6. **Semantic model TMDL** — script the whole model out of TMDL view and save it. Cheapest possible
   insurance for 24 measures and 13 relationships.

---

## Component coverage — the original "all seven Fabric items" requirement

| # | Component | Status | Note |
|---|---|---|---|
| 1 | **Lakehouse** | ✅ Complete | `OntologyDataLH`, Bronze → Silver → Gold |
| 2 | **SQL Database** | ✅ Complete | `SAP_S4H_Source`, real SAP DDL, mirrored to OneLake |
| 3 | **Data Pipeline** | ⚠️ **Partial** | `PL_Load_SAP_Source` Step 1 only — one verified `Copy_SAP_Table`. The ForEach over 32 tables, the control table, watermarking, notebook/dataflow activities and refresh steps were never built |
| 4 | **Dataflow Gen2** | ✅ Complete | FX rates, sales targets; rescaled ×3.2 on 2026-08-09 |
| 5 | **Notebook** | ✅ Complete | 04 (generate), 06 (Silver), 07 (Gold), 08 (lineage) |
| 6 | **Semantic model** | ✅ Complete | Direct Lake on OneLake, 13 tables, 13 relationships, 24 measures |
| 7 | **Ontology** | ✅ Complete | 16 entity types with keys, bound live to Gold |
| — | *(stretch)* Data Agent | ❌ Not possible | Capacity-gated on this SKU, not a tenant toggle. Correctly abandoned |

**Component 3 is the only real gap against the stated brief.** The plan sold it as *"the SAP
extraction framework narrative every SAP shop recognises"* — that narrative currently rests on a
single Copy activity. `Phase2/PIPELINE_BUILD_SPEC.md` has the verified configuration and the
Parquet-format gotcha already written up, so Step 2 is a known quantity, not research.

---

## Scope delivered versus scope planned

| Area | Planned | Built | Gap |
|---|---|---|---|
| Report pages | 7 | **5** | Order Health & Backlog, Billing & Revenue Leakage, Process Mining / Cycle Time, Ontology Explorer |
| Measures | ~70 | **24** | Median/P90 cycle times, backlog ageing, credit-block metrics, price realisation, order-change volatility |
| Cycle-time decomposition | 5 stages | **3** | Order→Credit Release and Credit→ATP never surfaced |
| Change documents | CDHDR/CDPOS | Silver only | `slv_order_change` exists; never promoted to Gold, model or report |
| Returns / credit memos | In scope | Partial | `IsCreditMemo` flows to `gld_doc_invoice`; no returns measure, no report surface |
| Skill 0 mockup | Required by plan | **Skipped** | The plan said Phase 6 runs Skill 0 → Skill 1. It went straight to build |

None of these are defects. They are scope that was deliberately or incidentally not reached, and
the report as delivered is coherent without them. Listing them matters because the plan is the
document a reviewer would read alongside it.

---

## Outstanding, by cost

### Cheap and worth doing

1. **Export everything** (see the trial section — do this first).
2. **Two AR measures in EUR.** `AmountEUR` has existed in Gold since revision 3 with no measure over
   it, which is why the Cash page is company-code-only:
   ```dax
   Open AR (EUR)    = CALCULATE ( SUM ( gld_fact_accounts_receivable[AmountEUR] ),
                                  gld_fact_accounts_receivable[IsOpen] = TRUE )
   Overdue AR (EUR) = CALCULATE ( SUM ( gld_fact_accounts_receivable[AmountEUR] ),
                                  gld_fact_accounts_receivable[IsOverdue] = TRUE )
   ```
   That unlocks a single group-currency AR total — 36.36m against the 38.64m naive sum.
3. **Business-friendly ontology names** — the entity types and relationships still carry
   `gld_fact_accounts_receivable_has_gld_dim_customer`-style names. Suggested names are in
   `Phase6/ONTOLOGY_FINDINGS.md`. Cosmetic, but it is the screen a reviewer looks at.
4. **Update the CLAUDE.md folder map.** It still describes Project 23 as *"through Phase 4 of 4+,
   no `.pbip` exists"*. There is now a five-page report, a semantic model and an ontology.

### Medium

5. **Data Pipeline Step 2** — the only genuine component gap. Half a day with the spec in hand.
6. **Surface the Phase 8 lineage tables.** Six Gold tables are built and validated but nothing
   consumes them: not in the semantic model, not in the report. Without the graph they are
   currently inert. A "Document Lineage" page over `gld_edge_*` would make them visible — 19,578
   complete order→cash paths is a genuinely interesting number that nothing currently shows.
7. **Change documents and returns.** `slv_order_change` is built and unused. An "Order Change
   Volatility" measure was in the plan and is the kind of metric SAP people notice.

### Optional

8. Remaining report pages and the measure catalogue toward ~70.
9. Project retrospective (see below).

---

## Finding: Fabric Graph edges do not persist

**2026-08-10.** Edges created through Graph → *Add edge* were saved, and were **gone after closing
and reopening the item from the workspace**. Repeatable enough that the graph work was abandoned.

This matters beyond cosmetics, because it is the second structural limitation found in the same
feature:

* Phase 6: ontology relationships do **not** materialise as graph edges. Edges are a separate
  construct built from a source table, and nothing warns you — the graph renders 13 disconnected
  nodes.
* Phase 8: edges that *are* created by hand do not survive a reopen.

Together these make the Graph surface unusable for a portfolio deliverable at its current preview
maturity. **The ontology itself is unaffected** — entity types, keys and live Gold bindings all work
and persist. Only the graph/edge layer is affected.

The Phase 8 work is not wasted: `gld_edge_order_to_delivery`, `gld_edge_delivery_to_invoice` and
`gld_edge_invoice_to_receivable` are validated Gold tables with zero orphan endpoints, and the
lineage they encode is queryable in SQL and DAX today, and re-bindable if the Graph feature
stabilises. What was lost is the traversal UI, not the model.

---

## Recommended close-out sequence

```
1. Export everything                  (trial deadline — do not defer)
2. Add the two AR EUR measures        (10 min, closes a documented gap)
3. Rename ontology entities           (30 min, reviewer-facing)
4. Update CLAUDE.md Project 23 row    (engine housekeeping)
5. Pipeline Step 2                    (only if the trial allows time)
6. Retrospective                      (before the context is cold)
```
