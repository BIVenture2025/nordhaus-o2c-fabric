# Phase 6 — Ontology: validated findings (2026-08-09)

**Item:** `Nordhaus_O2C_Model` (Ontology) — id `967babba-b8df-4c43-a298-770057906acd`

Created by **Generate ontology directly from the semantic model**, not via *New item → Ontology*.
That is the better route and the runbook has been corrected — it auto-provisions four items:

```
Nordhaus_O2C_Model            Ontology
Nordhaus_O2C_Model_graph_...  Graph model
Nordhaus_O2C_Model_lh_...     Lakehouse        (ontology's own store)
Nordhaus_O2C_Model_lh_...     SQL analytics endpoint
```

Same shape as the Phase 0 smoke test (`RetailSalesOntology` + `_graph` + `_lh`), which is a good
sign — it matches the configuration that passed the gate.

## What worked, and what it vindicates

### 1. Direct Lake **on OneLake** binds correctly

This was the single biggest open risk in the project — the plan flagged that Import mode produces
entity types with *no data bindings*, and I chose on-OneLake over on-SQL without being able to
verify it.

**It binds.** On `gld_fact_order_to_cash`, `ArAmountLocal` shows Type `Double` with Data source
`gld_fact_order_to_cash`. No nulls, no rejected model. The fallback plan (recreate as Direct Lake
on SQL) is **not needed**.

### 2. The Gold contract paid off

Zero `Decimal` columns and graph-safe column names meant every bound property came through with a
real type. The `enforce_gold()` gate in notebook 07 did the job it was written for.

### 3. All 12 semantic-model relationships were inherited automatically

The entity graph for `gld_fact_order_to_cash` already radiates to `gld_dim_customer`,
`gld_dim_material`, `gld_dim_plant`, `gld_dim_sales_org`, `gld_dim_division`, `gld_dim_order_type`
and more — **without any relationship being defined in the ontology UI.**

This is the most valuable finding of the phase: **the TMDL relationship work in Phase 5 paid off
twice.** Relationships defined once in the semantic model become the ontology's edges for free.
Anyone who skips model relationships and plans to "do it in the ontology" is choosing the harder
path.

### 4. Entity types are auto-created one-per-table

All 13 Gold tables became entity types, named after the table (`gld_dim_customer`, not
`Customer`). Renaming to business language is cosmetic polish, not a blocker.

## What is NOT done — the actual remaining work

### Entity type keys are not auto-defined

`Instances` tab reports:

> **Missing entity type key** — To view overview content, there must be an entity type key.

and the binding page warns:

> **Entity type key missing.** In order to save your binding(s), you will need to define the entity
> type key and map the appropriate columns to the property name(s) used for the entity type key.

So bindings exist but cannot be *saved/activated* until each entity type has a key. This is the
gate between "ontology configured" and "ontology queryable".

### Key mapping — and a Gold gap this exposed

| Entity type | Key column | Status |
|---|---|---|
| `gld_dim_customer` | `CustomerId` | OK (1 SCD2 version per customer today) |
| `gld_dim_material` | `MaterialId` | OK |
| `gld_dim_date` | `DateKey` | OK |
| `gld_dim_sales_org` | `SalesOrg` | OK |
| `gld_dim_plant` | `Plant` | OK |
| `gld_dim_division` | `Division` | OK |
| `gld_dim_channel` | `DistributionChannel` | OK |
| `gld_dim_order_type` | `OrderType` | OK |
| `gld_dim_rejection` | `RejectionReason` | OK |
| `gld_fact_order_to_cash` | **`OrderLineKey`** | OK — built as `SalesOrderId-SalesOrderItem` and proven unique by the Gold validation gate |
| `gld_fact_accounts_receivable` | — | **NO single-column key** |
| `gld_fact_sales_target` | — | **NO single-column key** |
| `gld_fact_document_flow` | — | **NO single-column key** |

I built `OrderLineKey` for the main fact and did not do the same for the other three. That was an
oversight in notebook 07, visible only now. Two ways forward:

* **If the ontology supports composite keys** — map the natural composites and change nothing:
  * AR: `CompanyCode` + `AccountingDocId` + `FiscalYear` + `LineItem`
  * Target: `YearMonth` + `SalesOrg` + `Division`
  * Doc flow: `PrecedingDocId` + `PrecedingItem` + `SubsequentDocId` + `SubsequentItem` + `SubsequentCategory`
* **If it requires a single column** — add three surrogate keys to notebook 07 and re-run Gold:
  ```python
  .withColumn("ArItemKey",     F.concat_ws("-", "CompanyCode", "AccountingDocId", "FiscalYear", "LineItem"))
  .withColumn("TargetKey",     F.concat_ws("-", "YearMonth", "SalesOrg", "Division"))
  .withColumn("DocFlowKey",    F.concat_ws("-", "PrecedingDocId", "PrecedingItem", "SubsequentDocId", "SubsequentItem", "SubsequentCategory"))
  ```
  Each is a `concat_ws` of the natural composite, so it stays deterministic and re-runnable, and a
  uniqueness assertion goes in the validation gate alongside the existing `OrderLineKey` check.

**ANSWERED 2026-08-09: composite keys ARE supported.** *Define entity type key* opens an
"Add or edit key" dialog with a **Property list** of checkboxes — multiple properties can be
selected, and the dialog describes a key as "a unique identification property **or sequence**".

So **path 1 applies: map the natural composites, change nothing in Gold.** The three
`concat_ws` surrogate keys are *not* needed. Notebook 07 stays as it is.

### Final key mapping (all 13)

| Entity type | Key properties |
|---|---|
| `gld_dim_customer` | `CustomerId` |
| `gld_dim_material` | `MaterialId` |
| `gld_dim_date` | `DateKey` |
| `gld_dim_sales_org` | `SalesOrg` |
| `gld_dim_plant` | `Plant` |
| `gld_dim_division` | `Division` |
| `gld_dim_channel` | `DistributionChannel` |
| `gld_dim_order_type` | `OrderType` |
| `gld_dim_rejection` | `RejectionReason` |
| `gld_fact_order_to_cash` | `OrderLineKey` |
| `gld_fact_accounts_receivable` | `CompanyCode` + `AccountingDocId` + `FiscalYear` + `LineItem` |
| `gld_fact_sales_target` | `YearMonth` + `SalesOrg` + `Division` |
| `gld_fact_document_flow` | `PrecedingDocId` + `PrecedingItem` + `SubsequentDocId` + `SubsequentItem` + `SubsequentCategory` |

Two cautions when picking from the property list:

1. **Measures appear in the same list** (`Order_Lines`, `Order_Lines_In_Scope`, `Avg_*`). They are
   unbound and must never be chosen as key properties.
2. **The SCD2 dimensions are keyed on the business key alone**, which is correct *today* because
   the Gold validation gate asserts exactly one current version per `CustomerId` / `MaterialId`.
   If a second version is ever created, this key silently breaks. At that point either add
   `ValidFrom` to the key or bind the entity to an `IsCurrent = true` filtered view. Recorded so
   the assumption is visible rather than buried.

### Timeseries section

The binding page also shows a **Timeseries data** block with a `Timestamp column *` marked
required. If Save is blocked by it, use the natural event date for the entity:
`gld_fact_order_to_cash` → `OrderCreatedDate`; `gld_fact_accounts_receivable` → `PostingDate`;
`gld_fact_document_flow` → `FlowCreatedDate`. Dimensions have no event date — if it demands one
there, say so, because that would be a genuine design conflict worth understanding.

### Measures arrive as unbound properties

`Avg_Delivery_Delay_Days`, `Avg_Goods_Issue_to_Invoice_Days`, `Avg_Order_to_Cash_Days` and friends
appear as properties with Data source **Unbound**. Expected — they are DAX measures, not columns,
so there is nothing to bind them to. Harmless noise. Leave them; do not try to bind them.

## Two standalone entity types — one is a defect, one is the real work

### `gld_dim_rejection` is standalone — my omission

I listed it as an entity type but never created the semantic-model relationship, so nothing was
inherited. The columns exist on both sides (`gld_fact_order_to_cash[RejectionReason]` →
`gld_dim_rejection[RejectionReason]`).

**Caveat worth knowing before adding it.** `RejectionReason` is `''` for ~98% of order lines —
only ~2% are rejected — and the dimension contains real reason codes only, not `''`. So the
relationship will leave the vast majority of fact rows unmatched and produce a blank member. That
is *correct* behaviour for a sparse degenerate dimension, not a fault, but it means the
relationship is only useful when filtered to `IsRejected = true`. Low value; add it for
completeness, not for insight.

TMDL to add in the semantic model (relationships flow through to the ontology):

```tmdl
createOrReplace
	relationship rel_o2c_rejection
		fromColumn: gld_fact_order_to_cash.RejectionReason
		toColumn: gld_dim_rejection.RejectionReason
```

### `gld_fact_document_flow` is standalone — expected, and it is the phase's real remaining work

It was deliberately left unrelated in Phase 5: its keys (`PrecedingDocId`, `SubsequentDocId`) are
SAP document numbers that may be an order, a delivery, an invoice or a credit memo depending on the
category column, so there is no single clean foreign key to any one dimension.

But this table is **the whole reason this is a graph and not a star** — 32,486 edges carrying
order → delivery → invoice → credit memo. Leaving it unconnected means the ontology can answer
star-schema questions only.

**This one does need a Gold change.** Entity relationships target the entity *key*, and
`gld_fact_order_to_cash`'s key is `OrderLineKey` (`SalesOrderId-SalesOrderItem`).
`gld_fact_document_flow` has the two parts but not the concatenation, so add it in notebook 07:

```python
.withColumn("PrecedingOrderLineKey",
            F.when(F.col("PrecedingCategory") == "C",
                   F.concat_ws("-", "PrecedingDocId", "PrecedingItem")))
.withColumn("SubsequentOrderLineKey",
            F.when(F.col("SubsequentCategory") == "C",
                   F.concat_ws("-", "SubsequentDocId", "SubsequentItem")))
```

The `when` guard matters: only rows whose category is `C` (sales order) point at an order line, so
the key is deliberately NULL for delivery/invoice/credit-memo rows rather than being a
same-shaped string that would join to nothing and look like a data error.

Then relate `gld_fact_document_flow.PrecedingOrderLineKey` → `gld_fact_order_to_cash.OrderLineKey`.

**Unverified:** whether the ontology permits a relationship between two *fact* entity types. If it
insists on fact→dimension only, the fallback is promoting document flow to its own entity with
`Delivery` and `Invoice` entity types built from `gld_silver` sources — a bigger change worth
discussing before starting.

## Relationship naming — business language

Auto-generated names such as `gld_fact_accounts_receivable_has_gld_dim_customer` are accurate and
unreadable. An ontology's value is that a non-engineer can read the graph, so the relationship
should complete the sentence *"an OrderLine ___ a Customer"*.

| From → To | Suggested name | Reads as |
|---|---|---|
| order_to_cash → dim_customer | **soldTo** | OrderLine is sold to Customer |
| order_to_cash → dim_material | **isForProduct** | OrderLine is for Product |
| order_to_cash → dim_plant | **shipsFrom** | OrderLine ships from Plant |
| order_to_cash → dim_sales_org | **soldBy** | OrderLine is sold by SalesOrganisation |
| order_to_cash → dim_division | **inDivision** | OrderLine is in Division |
| order_to_cash → dim_channel | **soldThrough** | OrderLine is sold through Channel |
| order_to_cash → dim_order_type | **hasOrderType** | OrderLine has OrderType |
| order_to_cash → dim_date | **orderedOn** | OrderLine was ordered on Date |
| order_to_cash → dim_rejection | **rejectedFor** | OrderLine was rejected for Reason |
| accounts_receivable → dim_customer | **owedBy** | ReceivableItem is owed by Customer |
| accounts_receivable → dim_date | **postedOn** | ReceivableItem was posted on Date |
| sales_target → dim_sales_org | **targetsSalesOrg** | Target applies to SalesOrganisation |
| sales_target → dim_division | **targetsDivision** | Target applies to Division |
| document_flow → order_to_cash | **originatesFrom** | DocumentFlow originates from OrderLine |

Convention: lowerCamelCase verb phrase, direction from the *fact* side, no table prefixes. If the
UI shows a display label separately from the identifier, use "Sold To", "Is For Product" etc. for
the label.

Renaming entity types to business nouns (`Customer`, `Product`, `OrderLine`, `ReceivableItem`,
`Plant`, `SalesOrganisation`) is the matching polish and is cosmetic — safe to do at any time.

## PHASE 6 FUNCTIONALLY COMPLETE — verified 2026-08-09

| item | result |
|---|---|
| Entity types | 13, all with keys defined (composites where needed) |
| Data bindings | resolve — Direct Lake **on OneLake** confirmed correct |
| `gld_fact_order_to_cash` instances | **16,175** = VBAP exactly |
| `gld_fact_document_flow` instances | **32,486** |
| Numeric properties | real values, no nulls — Gold contract held end to end |
| Relationships | 12 inherited from the semantic model + `originatesFrom` |

### Fact-to-fact relationships ARE supported

The open question is answered: `gld_fact_document_flow --originatesFrom--> gld_fact_order_to_cash`
was accepted. No restructure needed, no promotion of Delivery/Invoice to their own entity types.

### The graph reads as business language

Document-flow instances render as, e.g.:

```
4500001315 / 110   SalesOrder (C)   ->   8000005272 / 10   Delivery (J)   qty 47   value 46,208.52
```

The `_cat_map` translation written back in **Silver** (`C -> SalesOrder`, `J -> Delivery`,
`M -> Invoice`, ...) is what makes the graph readable. Worth noting for the engine: the decision to
translate SAP category codes into words in Silver — rather than leaving it to the report layer —
paid off two layers later in a component that did not exist when the decision was made.

Order `4500004544` item 110 appears twice, feeding deliveries `8000011385` and `8000011386` — a
split delivery visible directly as two graph edges.

## MAJOR FINDING — ontology relationships are NOT graph edges

The Graph model item (`Nordhaus_O2C_Model_graph_...`) loads fine and is **not** gated by the
Copilot capacity wall. It reports `Data load completed`, exposes a **Query** menu
(*Query now* / *Create queryset*), and lists **Nodes (13)** — one per entity type.

**There is no Edges section.** Scrolling the Components panel shows nodes only. The 13
relationships defined in the ontology — including `originatesFrom` — did not propagate.

`Add edge` opens a **Create an edge** dialog requiring:

| field | meaning |
|---|---|
| Edge label | the relationship name |
| **Source table** | a table physically containing both keys |
| Origin node + Origin key | |
| Target node + Target key | |

So an edge is *materialised from a table*, not inherited from a semantic relationship. **These are
two independent layers.** Ontology relationships drive entity browsing; graph edges are built
separately in the Graph model.

Worth contrasting with the earlier finding: semantic-model relationships **did** flow into the
ontology automatically. It is easy to assume the same propagation continues one layer further. It
does not, and nothing in the UI warns you — the graph simply renders 13 disconnected nodes.

### Edge definitions to create (star edges, all single-key)

All nine use `OrderLineKey`, so the composite-key question does not arise:

| Edge label | Source table | Origin node / key | Target node / key |
|---|---|---|---|
| `soldTo` | `gld_fact_order_to_cash` | same / `OrderLineKey` | `gld_dim_customer` / `SoldToId` |
| `isForProduct` | `gld_fact_order_to_cash` | same / `OrderLineKey` | `gld_dim_material` / `MaterialId` |
| `shipsFrom` | `gld_fact_order_to_cash` | same / `OrderLineKey` | `gld_dim_plant` / `Plant` |
| `soldBy` | `gld_fact_order_to_cash` | same / `OrderLineKey` | `gld_dim_sales_org` / `SalesOrg` |
| `orderedOn` | `gld_fact_order_to_cash` | same / `OrderLineKey` | `gld_dim_date` / `OrderCreatedDateKey` |
| `inDivision` | `gld_fact_order_to_cash` | same / `OrderLineKey` | `gld_dim_division` / `Division` |
| `soldThrough` | `gld_fact_order_to_cash` | same / `OrderLineKey` | `gld_dim_channel` / `DistributionChannel` |
| `hasOrderType` | `gld_fact_order_to_cash` | same / `OrderLineKey` | `gld_dim_order_type` / `OrderType` |
| `rejectedFor` | `gld_fact_order_to_cash` | same / `OrderLineKey` | `gld_dim_rejection` / `RejectionReason` |

**Untested:** whether Origin/Target key accepts multiple columns. `gld_fact_accounts_receivable`,
`gld_fact_sales_target` and `gld_fact_document_flow` all have composite entity keys. The *ontology*
accepted composites; the *graph* may not. If it does not, the three `concat_ws` surrogate keys
proposed earlier — and dismissed as unnecessary — become necessary after all, for this layer only.

### Document lineage needs a structural change, not just an edge

The prize was order -> delivery -> invoice traversal. That cannot be built from the current nodes:
the only document node is `gld_fact_order_to_cash`. There is no `Delivery` or `Invoice` node, so
`gld_fact_document_flow` has nowhere to point.

The correct shape is for document flow to be the **edge source**, not a node — with `Delivery` and
`Invoice` promoted to Gold tables (from `slv_delivery_item` / `slv_billing_item`) and made nodes.
That is a Gold change plus new entity types, not a quick fix. Recorded as the honest "what I would
do next" rather than attempted under time pressure.

What the nine star edges *do* give is real: traverse from any Customer, Product or Plant to every
connected order line and back out to any other dimension, without pre-declaring the join path.

## Corrections to the runbook

* Step 6.1 was wrong. **Generate the ontology from the semantic model**, not from *New item*.
* Step 6.3 (manually create entity types) is unnecessary — they are generated.
* Step 6.4 (manually create relationships) is unnecessary for the star relationships — they are
  inherited. Still open for the `gld_fact_document_flow` bridge traversal.
* New step: **define entity type keys**, which is the real gate.
