# Phase 2b — Dataflow Gen2: non-SAP reference data

**Purpose:** bring the reference data that does *not* come from SAP into the lakehouse, using the
low-code tool that fits it. This closes the last outstanding Phase 2 item and the **Dataflow Gen2**
component of the seven-part Fabric scope.

## Why Dataflow Gen2 here and nowhere else

The heavy transformation (document-flow resolution, SCD2, the accumulating snapshot) belongs in
Spark notebooks — it is complex, testable, and version-controlled. Dataflow Gen2 earns its place on
**small, slow-changing, externally-sourced reference data**, which is exactly what a finance or ops
user would maintain themselves. It also burns noticeably more CU than a notebook per row, so keeping
it off the hot path is a deliberate cost decision, not just taste.

## What it ingests

### 1. `FX_RATES` — 72 rows (24 months × 3 currencies)

Group currency is **EUR** (company code `DE10`). Nordhaus transacts in EUR, USD and SGD, so every
revenue figure in Gold needs translating or the exec scoreboard silently adds three currencies
together. This is load-bearing, not decoration.

| Column | Notes |
|---|---|
| `YEARMONTH` | `YYYYMM`, joins to the month grain of `DimDate` |
| `FROM_CURR` | EUR / USD / SGD |
| `TO_CURR` | always EUR (group currency) |
| `RATE` | 1 unit of `FROM_CURR` expressed in EUR; EUR→EUR is exactly 1.0 |

Rates drift slowly month over month with small noise rather than sitting flat — a flat rate makes
currency translation invisible in the analytics, which defeats the point of modelling it.

### 2. `SALES_TARGET` — 288 rows (24 months × 3 sales orgs × 4 divisions)

| Column | Notes |
|---|---|
| `YEARMONTH` | `YYYYMM` |
| `VKORG` | DE10 / US10 / SG10 |
| `SPART` | 01 Seating / 02 Tables & case goods / 03 Storage / 04 Outdoor |
| `TARGET_NET_EUR` | monthly net revenue target, already in group currency |
| `CURRENCY` | EUR |

Targets carry the **same Q4 seasonality as the generated demand** (Oct/Nov 1.75×, Dec 1.35×,
Feb 0.72×). If targets were flat while actuals were seasonal, every variance chart would show a
fake Q4 beat and a fake February miss — the analysis would be measuring the target's flatness
rather than business performance.

Annual target total ≈ **€54.5M**, which sits sensibly against generated actuals.

## Flow

```
Notebook 05  ->  Files/reference/*.csv  ->  Dataflow Gen2  ->  Lakehouse tables
(generates)      (landing)                  (type + clean)     ref_fx_rates
                                                               ref_sales_target
```

The file-drop-then-dataflow shape is the realistic pattern: in a real Nordhaus, finance would
maintain these in a spreadsheet or they would arrive from an FX provider, and a dataflow would pick
them up. Generating them inline inside the dataflow would be a demo shortcut that teaches nothing.

## Dataflow transformations (deliberately light)

1. Source: Lakehouse → `Files/reference/FX_RATES.csv` (and `SALES_TARGET.csv`)
2. Promote headers
3. Set types explicitly — `RATE` and `TARGET_NET_EUR` to **decimal**, `YEARMONTH`/`VKORG`/`SPART`
   to **text** (leading-zero divisions like `01` must not become integers)
4. Destination: Lakehouse table, **Replace** (these are full snapshots, not increments)

> `SPART` is the trap: `01`–`04` will be inferred as whole numbers and lose the leading zero,
> silently breaking every join to `TSPA` and `VBAK`. Force it to text.

## The M for each query

Paste into **Home → Advanced editor** (replace the generated `#"Navigation N"` chain wholesale —
the generated version is correct but unreadable, and the rename makes the intent legible). For
`SALES_TARGET`, swap the filename, the column count (`Columns = 5`) and the type list.

```m
let
    Source = Lakehouse.Contents([HierarchicalNavigation = null, EnableVorder = true, OutputMetadataRefresh = true]),
    Workspace = Source{[workspaceId = "."]}[Data],
    LH = Workspace{[lakehouseName = "OntologyDataLH"]}[Data],
    FilesRoot = LH{[Id = "Files", ItemKind = "Folder"]}[Data],
    RefFolder = FilesRoot{[Name = "reference"]}[Content],
    FileBin = RefFolder{[Name = "FX_RATES.csv"]}[Content],
    CsvRows = Csv.Document(FileBin, [Delimiter = ",", Columns = 4, QuoteStyle = QuoteStyle.None]),
    Headers = Table.PromoteHeaders(CsvRows, [PromoteAllScalars = true]),
    Typed = Table.TransformColumnTypes(Headers, {{"YEARMONTH", type text}, {"FROM_CURR", type text}, {"TO_CURR", type text}, {"RATE", type number}})
in
    Typed
```

`SALES_TARGET` type list — note `SPART` forced to text:

```m
    Typed = Table.TransformColumnTypes(Headers, {{"YEARMONTH", type text}, {"VKORG", type text}, {"SPART", type text}, {"TARGET_NET_EUR", type number}, {"CURRENCY", type text}})
```

## Status

- [x] Reference data designed and validated locally
- [x] Notebook `05_Generate_Reference_Data` created, lakehouse pinned as default, run clean —
      `fx rows 72 | target rows 288 | annual target EUR 54526993.14`
- [x] OneLake write independently verified — `Files/reference/{FX_RATES,SALES_TARGET}.csv` are
      visible from the Dataflow's Lakehouse navigator, i.e. from outside the notebook session.
      This is the check that the Phase 1 silent-write incident taught us to insist on.
- [x] Dataflow Gen2 `DF_Load_Reference_Data` created (`0c0c0d1d-20a2-4d7a-a3a8-dbe935627aca`),
      both CSVs bound as queries `FX_RATES csv` / `SALES_TARGET csv`, 73 rows / 4 cols confirmed
      on the FX preview
- [x] Advanced editor M applied to both queries — headers promoted, types pinned
- [x] Queries renamed to `ref_fx_rates` / `ref_sales_target`
- [x] Data destination → Lakehouse `OntologyDataLH`, container `dbo`, update method **Replace**,
      schema **Dynamic** (safe here because the M pins every type explicitly)
- [x] Published and run — `Last run 2026-08-05 10:54`
- [x] **Verified in the lakehouse:** `ref_fx_rates` 72 rows × 4 cols, `ref_sales_target`
      288 rows × 5 cols, `SPART` holding `01`–`04` as text. Both figures match the design exactly.

**PHASE 2b COMPLETE.** All seven required Fabric components are now instantiated except the
semantic model and ontology (Phases 5–6).

## Lessons for the engine (Fabric web UI automation)

1. **Notebook cells have a command mode.** Clicking a cell selects it; typing then fires Jupyter
   shortcuts instead of entering text — that is how a stray `TESTLINE` turned one code cell into
   five cells and two markdown cells. The reliable sequence is **click the cell → press Enter →
   type**. Verify with the status bar (`Selected Cell N of M`) before typing anything.
2. **Screenshots go stale during Fabric typing.** Several screenshots showed only the first chunk
   of a multi-chunk `type` batch; the rest had in fact landed. Re-screenshot before "fixing"
   apparently-missing text, or you will duplicate lines (this happened once and needed a repair).
3. **Never press Enter to dismiss a Monaco IntelliSense popup.** It accepts the highlighted
   suggestion — `Typed` silently became `Type.FunctionRequiredParameters`. Press Escape. And note
   Escape inside the Advanced editor raises *Discard query changes*, which Escape then cancels.
4. **Monaco's bracket auto-close is not a problem in practice** — 18 lines of Python and 12 lines
   of M typed character-for-character correct, including nested `{{"COL", type text}, ...}`.
   Auto-**indent** is the real hazard, so keep typed code flat and block-free.
5. **A wedged renderer looks like a broken tool.** Mouse clicks silently stopped landing for a
   whole session while keyboard input kept working. Reopening the item in a fresh tab restored
   clicks immediately with no loss — the dataflow draft and both queries survived. Reach for a
   fresh tab early rather than re-deriving coordinates.
6. **`Unidentified` in the lakehouse explorer is usually stale metadata, not a failed write.**
   Both new tables first appeared under `Tables > Unidentified`; a page refresh promoted them to
   real Delta tables with correct row counts. Refresh before diagnosing.
7. **Read the row count off the lakehouse, not the dataflow.** The dataflow reported success;
   the count that matters (72 / 288) came from the lakehouse table preview.
