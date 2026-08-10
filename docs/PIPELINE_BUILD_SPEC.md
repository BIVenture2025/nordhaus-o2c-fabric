# Phase 2 — `PL_Load_SAP_Source` build spec

**Status:** ✅ **Step 1 COMPLETE and verified (2026-08-04).** `Copy_SAP_Table` runs green in 33s and
loaded `sap.T001` with 3 rows, confirmed by `SELECT COUNT(*)`. Step 2 (ForEach over 32 tables) is
the remaining work.

### Verified working configuration

| | Setting | Value |
|---|---|---|
| **Source** | Connection | `OntologyDataLH (Nordhaus-O2C-Dev)` |
| | Root folder | **Files** (not Tables) |
| | File path type | File path |
| | Directory | `sap_landing_parquet` |
| | File name | `T001.parquet` |
| | **File format** | **Parquet** ← see gotcha below |
| **Destination** | Connection | `FabricSql WaiSonYeong` |
| | SQL Database | `SAP_S4H_Source` |
| | Table option | **Use existing** (never Auto create) |
| | Table | `sap.T001` |

**Gotcha that cost a validation round:** File format defaults to **Binary**, and selecting Parquet
from the dropdown silently fails if the list scroll position shifts mid-click. The symptom is
validation error *"Destination must be binary when source is of binary format."* Confirm the field
reads `Parquet` before validating — and note the **Mapping tab only becomes enabled once Parquet is
actually set**, which is the reliable visual tell.


**Goal:** copy 32 Parquet files from `OntologyDataLH → Files/sap_landing_parquet` into the
matching `sap.*` tables in `SAP_S4H_Source`.

Build order matters: get **one** Copy activity working end to end first, then wrap it in a ForEach.
Every dead end in this project so far was caught by testing one case before scaling to 32.

---

## Why a pipeline and not T-SQL

`OPENROWSET` from OneLake into Fabric SQL Database does not work — four variants, all rejected,
conclusively `Msg 5371` (connector unsupported, independent of format). Full detail in
`../Phase1/README.md`. The Copy activity uses managed connectors instead of a URL prefix, which is
why it works where `OPENROWSET` can't. Data Pipeline is also a required component of the build, so
this is the tool doing its real job rather than a workaround.

---

## Step 1 — Single Copy activity (prove the pattern)

Add **Copy data → Add to canvas**. Name it `Copy_SAP_Table`.

**Source**
| Setting | Value |
|---|---|
| Connection | `OntologyDataLH` (Lakehouse) |
| Root folder | `Files` |
| File path | `sap_landing_parquet/T001.parquet` |
| File format | **Parquet** |

**Destination**
| Setting | Value |
|---|---|
| Connection | `SAP_S4H_Source` (SQL database) |
| Table | `sap` . `T001` |
| Table action | **Append** |

> Do **not** pick "Auto create table" — the tables already exist with enforced primary keys and
> deliberate SAP typing. Letting Copy invent the schema would discard both.

**Run it.** Expect 3 rows in `sap.T001`. Verify:

```sql
SELECT COUNT(*) FROM sap.T001;   -- expect 3
```

If this fails, fix it here — do not proceed to the ForEach.

---

## Step 2 — ✅ BUILT (2026-08-04). ForEach over 32 tables

`ForEach_SAP_Table` containing `Copy_SAP_Table`. Validated clean, running green.

### How to move a working Copy activity into a ForEach

Do **not** rebuild it — the config is fiddly and re-earning it wastes the verification you already
did. Cut and paste preserves everything:

1. Select the Copy activity on the main canvas → **Ctrl+X**
2. Click the ForEach's **pencil (edit)** icon → this opens the loop's own inner canvas
   (breadcrumb changes to `Main canvas > ForEach_SAP_Table`)
3. Click empty space there → **Ctrl+V**

Pasting on the *outer* canvas while the ForEach is merely selected drops it **beside** the loop,
not inside. The breadcrumb is the tell — you must actually be inside the ForEach's canvas.
All source/destination settings, including the connections, survive the move.

### Dynamic settings

| Field | Value |
|---|---|
| ForEach → Sequential | **checked** (easier to debug; volume is small) |
| ForEach → Items | `@createArray('T001','TVKO',…,'CDPOS')` — full 32-name array below |
| Source → File name | `@{concat(item(),'.parquet')}` |
| Destination → **Enter manually** | checked — this splits Table into schema + name boxes |
| Destination → schema / table | `sap` / `@{item()}` |
| Destination → Advanced → **Pre-copy script** | `TRUNCATE TABLE sap.@{item()}` |

**The `@@` trap:** typing `@{item()}` straight into a settings box makes Fabric write `@@{item()}` —
`@@` is its escape for a *literal* `@`, so the expression never evaluates. Fix it in the **Pipeline
expression builder** (click the field, then edit there) rather than the inline box, where a single
`@` is preserved correctly.

**Pre-copy script is what makes the pipeline re-runnable.** Without it, a second run appends
duplicate rows and dies on the primary keys. `TRUNCATE` per iteration is cleaner than table-level
Overwrite because it leaves the table definition — and therefore the PKs and OneLake mirroring —
untouched.



Add a **ForEach** activity, move the Copy inside it.

**ForEach → Settings → Items** (Sequential ON — simpler to debug, volume is small):

```
@createArray('T001','TVKO','TVTW','TSPA','T001W','TVAK','TVAKT','TVAUT','KNA1','KNVV','KNB1','ADRC','MARA','MARC','MVKE','MAKT','VBAK','VBAP','VBEP','VBKD','VBPA','LIKP','LIPS','VBRK','VBRP','PRCD_ELEMENTS','BKPF','BSID','BSAD','VBFA','CDHDR','CDPOS')
```

Then make the Copy activity dynamic — switch both fields to expression mode:

| Field | Expression |
|---|---|
| Source file path | `@concat('sap_landing_parquet/', item(), '.parquet')` |
| Destination table name | `@item()` |

Destination schema stays the literal `sap`.

---

## ✅ PHASE 2 COMPLETE (2026-08-05)

Re-ran the notebook with corrected `MATKL` codes, then re-ran `PL_Load_SAP_Source`.

| Check | Expected | Actual |
|---|---|---|
| Tables loaded | 32 | **32** |
| Total rows | 226,631 | **226,631** |
| Pipeline failures | 0 | **0** |

**OneLake mirroring confirmed live** — `Replication → Monitor replication` shows non-zero
`Rows replicated` on every table, including the two that previously failed:

| Table | Rows replicated |
|---|---|
| `sap.BKPF` | 21,008 |
| `sap.VBRP` | 16,175 ← previously failed |
| `sap.VBKD` | 11,000 |
| `sap.BSAD` | 10,198 |
| `sap.MVKE` | 2,880 |
| `sap.MARA` | 160 ← previously failed |

That is the moment Bronze actually exists in OneLake, and the payoff for insisting on enforced
primary keys back in Phase 0 — no PK would have meant no mirror, silently.

**The SAP source system is now complete and re-runnable end to end.** Next: Phase 3 (Silver),
starting from the mirrored Bronze tables.

---

## Historical record — the failed first run (2026-08-04)

## ⚠ RUN RESULT (2026-08-04): 29 of 32 loaded, pipeline reported Failed

| | |
|---|---|
| Pipeline run ID | `c1509d96-5dea-44d7-9ddc-efac66aa1ecd` |
| Duration | 15m 24s (sequential, ~27s per table) |
| Pipeline status | **Failed** |
| Tables loaded | **29 of 32** |
| Rows loaded | **194,121** of an expected 226,631 |
| Shortfall | **32,510 rows** |

The mechanism works — 29 tables loaded cleanly, iterating in array order, with `TRUNCATE` +
reload behaving correctly. Three iterations failed.

### ✅ ROOT CAUSE FOUND AND FIXED (2026-08-05)

The three empty tables were **MARA, VBAP, VBRP** — *not* VBFA. The row arithmetic pointed at
VBFA and the row arithmetic was wrong; the actual list came from querying the database.

**Error:** `SqlBulkCopyInvalidColumnLength` — *"SQL Bulk Copy failed due to receive an invalid
column length from the bcp client."*

**Cause:** `MATKL` is `CHAR(9)` in SAP, and it appears in exactly three tables — MARA, VBAP, VBRP.
The generator invented three material-group codes that were **10 characters**:
`SEAT-CHAIR`, `SEAT-STOOL`, `OUT-LOUNGE`. The DDL was right; the generator was wrong.

**Fix:** codes shortened to `SEAT-CHR`, `SEAT-STL`, `OUT-LNG` in both
`02_generate_nordhaus_sap_data.py` and `04_…ipynb`, with the `CHAR(9)` constraint noted at the
definition site.

**Systemic fix:** `03_validate_nordhaus_data.py` now parses the DDL and asserts that no generated
value exceeds its destination column width — 310 columns checked. Verified by regression: reverting
one code makes the check fail with
`MARA.MATKL is CHAR(9) but data reaches 10; VBAP.MATKL …; VBRP.MATKL …`.

That is the real win. Fabric's error names **no column and no table**, which is why this cost a
full 15-minute run to localise. The validator now names both, before anything reaches Fabric.

**Why enforced primary keys and correct column widths were worth keeping:** a permissive schema
(or `Auto create table`) would have silently truncated `SEAT-CHAIR` to `SEAT-CHA` and loaded
"successfully". The strict schema turned silent data corruption into a loud failure.

### To complete the load

1. Re-run the notebook (`04_…ipynb`) to regenerate data with the corrected codes.
2. Re-run `PL_Load_SAP_Source`. The pre-copy `TRUNCATE` makes this safe — it repairs the 29
   already-loaded tables rather than duplicating them.
3. Expect **32 tables / 226,631 rows**.

---

### Original diagnostic notes (kept for the record)

The shortfall of 32,510 rows is suspiciously close to `VBFA`'s 32,486 — the single largest table.
That points at a size/timeout issue rather than a schema problem, but **confirm before acting**:

```sql
-- which three tables are empty?
SELECT t.name AS empty_table
FROM   sys.tables t
WHERE  t.schema_id = SCHEMA_ID('sap')
AND    NOT EXISTS (SELECT 1 FROM sys.partitions p
                   WHERE p.object_id = t.object_id
                   AND p.index_id IN (0,1) AND p.rows > 0)
ORDER BY t.name;
```

Then open the pipeline's **View run history → the failed iteration → Output** to read the actual
error. Do not guess from the row arithmetic alone — the count only *suggests* VBFA.

### Likely fixes, in order of cheapness

1. **Write batch timeout / size** — Destination → Advanced has both, currently blank (defaults).
   A large table can exceed the default write batch timeout. Set `Write batch timeout` to e.g.
   `00:30:00`.
2. **Bulk insert table lock = Yes** for the large tables — faster, fewer lock escalations.
3. If the error is a **PK violation**, the pre-copy `TRUNCATE` didn't run for that iteration —
   check the expression resolved correctly for that table name.

Re-running is safe and cheap: the pre-copy `TRUNCATE` makes every iteration idempotent, so a
re-run repairs partial state rather than compounding it. That property is worth more here than
getting it right first time.

## Step 3 — Make it re-runnable

Table action **Append** will duplicate rows on a second run and violate the primary keys. Either:

- set destination **Table action → Overwrite**, or
- add a **Script** activity before the ForEach running
  `EXEC sp_MSforeachtable` equivalent — simplest is a literal 32-line
  `TRUNCATE TABLE sap.<T>;` script.

Overwrite is fewer moving parts. Confirm it preserves the existing table definition rather than
recreating it — if it drops and recreates, use the truncate script instead, because losing the
primary keys silently disables OneLake mirroring.

---

## Step 4 — Verify the load

```sql
SELECT COUNT(*) AS tables_with_rows FROM (
  SELECT t.name FROM sys.tables t
  JOIN sys.partitions p ON p.object_id = t.object_id AND p.index_id IN (0,1)
  WHERE t.schema_id = SCHEMA_ID('sap')
  GROUP BY t.name HAVING SUM(p.rows) > 0
) x;   -- expect 32
```

Expected total ≈ **226,631 rows** at `SCALE = 0.10`. Per-table counts are in
`Files/sap_landing/_generation_manifest.json`.

Then check **SQL Database → Replication → Monitor replication**: `Rows replicated` should move off
zero. That is the moment Bronze actually exists in OneLake, and the real proof the primary-key
decision was correct.

---

## Known trap

`BSID` / `BSAD` carry `''` (not NULL) in primary-key columns `UMSKS`, `UMSKZ`, and `AUGBL`.
Parquet was chosen partly to preserve this — every column is written as a string, so blanks survive.
If those two tables fail on a NOT NULL violation, the cause is a null-coercion setting on the
source, not the data.

---

## After this works

1. Add a **Notebook activity** for Bronze → Silver, chained after the ForEach.
2. **Do not** chain a semantic-model refresh directly onto a Spark write — the SQL analytics
   endpoint sync lag caused a false "source tables do not exist" failure in Phase 0. Insert a wait
   or a retry.
3. Add failure alerting on the pipeline.
