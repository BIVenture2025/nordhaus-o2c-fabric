/* ============================================================================
   Nordhaus Group — SAP S/4HANA Order-to-Cash source system
   Target: Microsoft Fabric SQL Database
   Phase 1 of the Fabric E2E O2C build

   SAP conventions deliberately preserved (these are the point, not accidents):
     MANDT      client field, first column of every application table ('100')
     DATS dates CHAR(8) 'YYYYMMDD', empty = '00000000' NOT null
                -> Silver must parse and convert '00000000' to NULL
     TIMS times CHAR(6) 'HHMMSS'
     LOEKZ      deletion indicator, 'X' or ''
     SPRAS      language key ('E')
     CURR/QUAN  DECIMAL amounts and quantities
                -> NOTE: Decimal is fine HERE. It is banned in GOLD because
                   Fabric Graph does not support it (see plan section 2).
     Trailing blanks and leading-zero numeric keys (VBELN '0000012345')
                -> another genuine Silver cleanup task

   Naming is intentionally cryptic. That is what the Bronze layer looks like
   in a real SAP shop, and reproducing it is the credibility signal.

   ---------------------------------------------------------------------------
   ENGINE TARGET: Fabric **SQL Database** (Azure SQL engine) - NOT Fabric Warehouse.
   Constraints here are REAL and ENFORCED. Do not add `NOT ENFORCED` or force
   `NONCLUSTERED` - those are Fabric *Warehouse* rules and are rejected here with
   `Msg 40514 ... 'NOT ENFORCED' is not supported in this version of SQL Server`.

   Primary keys are deliberately kept (not dropped) because Fabric SQL Database
   mirrors to OneLake automatically only for *eligible* tables, and eligibility
   requires a primary key. No PK = no mirror = no Bronze.
   All 32 PKs verified unique against generated data before shipping.

   LOAD WARNING - empty strings in key columns:
   BSID/BSAD carry `UMSKS`, `UMSKZ` = '' and (for open items) `AUGBL` = '' as part
   of the primary key. That is authentic SAP, and legal here because '' is not NULL.
   But a Data Pipeline Copy activity that treats empty strings as NULL will violate
   NOT NULL and fail the load. In the Copy activity, set the source CSV option
   "treat empty string as null" to FALSE.
   ---------------------------------------------------------------------------
   ============================================================================ */

/* ---------------------------------------------------------------------------
   IDEMPOTENT RESET - the whole script is safe to re-run any number of times.

   Creates the `sap` schema only if missing, then drops every existing sap.*
   table so the CREATE statements below always start from clean. No foreign keys
   are defined, so drop order is irrelevant.

   This matters beyond convenience: the trial capacity is finite and the entire
   environment must be rebuildable from source on demand. A DDL script that only
   works on an empty database is not a rebuild script.

   `CREATE SCHEMA` must be the first statement in its batch, hence the EXEC()
   wrapper rather than a bare guarded statement.
   --------------------------------------------------------------------------- */

IF SCHEMA_ID('sap') IS NULL
    EXEC('CREATE SCHEMA sap');
GO

DECLARE @drop NVARCHAR(MAX) = N'';
SELECT  @drop += N'DROP TABLE IF EXISTS sap.' + QUOTENAME(name) + N';' + CHAR(10)
FROM    sys.tables
WHERE   schema_id = SCHEMA_ID('sap');

IF @drop <> N'' EXEC sp_executesql @drop;
GO

/* ============================================================================
   1. ORGANISATIONAL / CONFIGURATION TABLES
   ============================================================================ */

-- Company codes
CREATE TABLE sap.T001 (
    MANDT   CHAR(3)      NOT NULL,
    BUKRS   CHAR(4)      NOT NULL,   -- Company code
    BUTXT   NVARCHAR(25) NULL,       -- Company name
    ORT01   NVARCHAR(25) NULL,       -- City
    LAND1   CHAR(3)      NULL,       -- Country key
    WAERS   CHAR(5)      NULL,       -- Local currency
    SPRAS   CHAR(1)      NULL,
    CONSTRAINT PK_T001 PRIMARY KEY (MANDT, BUKRS));

-- Sales organisations
CREATE TABLE sap.TVKO (
    MANDT   CHAR(3)      NOT NULL,
    VKORG   CHAR(4)      NOT NULL,   -- Sales organisation
    BUKRS   CHAR(4)      NULL,       -- Company code
    VKOOR   NVARCHAR(30) NULL,       -- Description
    WAERS   CHAR(5)      NULL,
    CONSTRAINT PK_TVKO PRIMARY KEY (MANDT, VKORG));

-- Distribution channels
CREATE TABLE sap.TVTW (
    MANDT   CHAR(3)      NOT NULL,
    VTWEG   CHAR(2)      NOT NULL,   -- Distribution channel
    VTEXT   NVARCHAR(20) NULL,
    SPRAS   CHAR(1)      NULL,
    CONSTRAINT PK_TVTW PRIMARY KEY (MANDT, VTWEG));

-- Divisions
CREATE TABLE sap.TSPA (
    MANDT   CHAR(3)      NOT NULL,
    SPART   CHAR(2)      NOT NULL,   -- Division
    VTEXT   NVARCHAR(20) NULL,
    SPRAS   CHAR(1)      NULL,
    CONSTRAINT PK_TSPA PRIMARY KEY (MANDT, SPART));

-- Plants
CREATE TABLE sap.T001W (
    MANDT   CHAR(3)      NOT NULL,
    WERKS   CHAR(4)      NOT NULL,   -- Plant
    NAME1   NVARCHAR(30) NULL,
    LAND1   CHAR(3)      NULL,
    ORT01   NVARCHAR(25) NULL,
    VKORG   CHAR(4)      NULL,
    CONSTRAINT PK_T001W PRIMARY KEY (MANDT, WERKS));

-- Sales document types
CREATE TABLE sap.TVAK (
    MANDT   CHAR(3)      NOT NULL,
    AUART   CHAR(4)      NOT NULL,   -- Sales document type
    VBTYP   CHAR(1)      NULL,       -- SD document category
    CONSTRAINT PK_TVAK PRIMARY KEY (MANDT, AUART));

-- Sales document type texts
CREATE TABLE sap.TVAKT (
    MANDT   CHAR(3)      NOT NULL,
    SPRAS   CHAR(1)      NOT NULL,
    AUART   CHAR(4)      NOT NULL,
    BEZEI   NVARCHAR(20) NULL,
    CONSTRAINT PK_TVAKT PRIMARY KEY (MANDT, SPRAS, AUART));

-- Rejection reason texts
CREATE TABLE sap.TVAUT (
    MANDT   CHAR(3)      NOT NULL,
    SPRAS   CHAR(1)      NOT NULL,
    ABGRU   CHAR(2)      NOT NULL,   -- Reason for rejection
    BEZEI   NVARCHAR(40) NULL,
    CONSTRAINT PK_TVAUT PRIMARY KEY (MANDT, SPRAS, ABGRU));

/* ============================================================================
   2. CUSTOMER MASTER
   ============================================================================ */

-- Customer master: general data
CREATE TABLE sap.KNA1 (
    MANDT   CHAR(3)      NOT NULL,
    KUNNR   CHAR(10)     NOT NULL,   -- Customer number (leading zeros)
    NAME1   NVARCHAR(35) NULL,
    LAND1   CHAR(3)      NULL,
    ORT01   NVARCHAR(35) NULL,
    PSTLZ   CHAR(10)     NULL,
    REGIO   CHAR(3)      NULL,
    KTOKD   CHAR(4)      NULL,       -- Account group
    BRSCH   CHAR(4)      NULL,       -- Industry key
    ADRNR   CHAR(10)     NULL,       -- Address number -> ADRC
    ERDAT   CHAR(8)      NULL,       -- Created on (DATS)
    LOEVM   CHAR(1)      NULL,       -- Deletion flag
    CONSTRAINT PK_KNA1 PRIMARY KEY (MANDT, KUNNR));

-- Customer master: sales area data
CREATE TABLE sap.KNVV (
    MANDT   CHAR(3)      NOT NULL,
    KUNNR   CHAR(10)     NOT NULL,
    VKORG   CHAR(4)      NOT NULL,
    VTWEG   CHAR(2)      NOT NULL,
    SPART   CHAR(2)      NOT NULL,
    KDGRP   CHAR(2)      NULL,       -- Customer group
    BZIRK   CHAR(6)      NULL,       -- Sales district
    KONDA   CHAR(2)      NULL,       -- Price group
    INCO1   CHAR(3)      NULL,       -- Incoterms
    ZTERM   CHAR(4)      NULL,       -- Payment terms
    VSBED   CHAR(2)      NULL,       -- Shipping conditions
    LOEVM   CHAR(1)      NULL,
    CONSTRAINT PK_KNVV PRIMARY KEY
        (MANDT, KUNNR, VKORG, VTWEG, SPART));

-- Customer master: company code data
CREATE TABLE sap.KNB1 (
    MANDT   CHAR(3)      NOT NULL,
    KUNNR   CHAR(10)     NOT NULL,
    BUKRS   CHAR(4)      NOT NULL,
    AKONT   CHAR(10)     NULL,       -- Reconciliation account
    ZTERM   CHAR(4)      NULL,
    ZWELS   CHAR(10)     NULL,       -- Payment methods
    LOEVM   CHAR(1)      NULL,
    CONSTRAINT PK_KNB1 PRIMARY KEY (MANDT, KUNNR, BUKRS));

-- Addresses
CREATE TABLE sap.ADRC (
    CLIENT      CHAR(3)      NOT NULL,
    ADDRNUMBER  CHAR(10)     NOT NULL,
    NAME1       NVARCHAR(40) NULL,
    CITY1       NVARCHAR(40) NULL,
    POST_CODE1  CHAR(10)     NULL,
    STREET      NVARCHAR(60) NULL,
    COUNTRY     CHAR(3)      NULL,
    REGION      CHAR(3)      NULL,
    CONSTRAINT PK_ADRC PRIMARY KEY (CLIENT, ADDRNUMBER));

/* ============================================================================
   3. MATERIAL MASTER
   ============================================================================ */

CREATE TABLE sap.MARA (
    MANDT   CHAR(3)       NOT NULL,
    MATNR   CHAR(18)      NOT NULL,  -- Material number
    MTART   CHAR(4)       NULL,      -- Material type (FERT/HALB)
    MATKL   CHAR(9)       NULL,      -- Material group
    MEINS   CHAR(3)       NULL,      -- Base unit of measure
    BRGEW   DECIMAL(13,3) NULL,      -- Gross weight
    NTGEW   DECIMAL(13,3) NULL,      -- Net weight
    GEWEI   CHAR(3)       NULL,      -- Weight unit
    VOLUM   DECIMAL(13,3) NULL,      -- Volume
    VOLEH   CHAR(3)       NULL,
    PRDHA   CHAR(18)      NULL,      -- Product hierarchy
    ERSDA   CHAR(8)       NULL,
    LVORM   CHAR(1)       NULL,
    CONSTRAINT PK_MARA PRIMARY KEY (MANDT, MATNR));

CREATE TABLE sap.MARC (
    MANDT   CHAR(3)       NOT NULL,
    MATNR   CHAR(18)      NOT NULL,
    WERKS   CHAR(4)       NOT NULL,
    DISMM   CHAR(2)       NULL,      -- MRP type
    BESKZ   CHAR(1)       NULL,      -- Procurement type
    PLIFZ   DECIMAL(3,0)  NULL,      -- Planned delivery time (days)
    WEBAZ   DECIMAL(3,0)  NULL,      -- Goods receipt processing time
    STRGR   CHAR(2)       NULL,      -- Planning strategy group (MTS/MTO)
    LVORM   CHAR(1)       NULL,
    CONSTRAINT PK_MARC PRIMARY KEY (MANDT, MATNR, WERKS));

CREATE TABLE sap.MVKE (
    MANDT   CHAR(3)   NOT NULL,
    MATNR   CHAR(18)  NOT NULL,
    VKORG   CHAR(4)   NOT NULL,
    VTWEG   CHAR(2)   NOT NULL,
    MTPOS   CHAR(4)   NULL,          -- Item category group
    KONDM   CHAR(2)   NULL,          -- Material pricing group
    PRODH   CHAR(18)  NULL,
    LVORM   CHAR(1)   NULL,
    CONSTRAINT PK_MVKE PRIMARY KEY
        (MANDT, MATNR, VKORG, VTWEG));

CREATE TABLE sap.MAKT (
    MANDT   CHAR(3)      NOT NULL,
    MATNR   CHAR(18)     NOT NULL,
    SPRAS   CHAR(1)      NOT NULL,
    MAKTX   NVARCHAR(40) NULL,       -- Material description
    CONSTRAINT PK_MAKT PRIMARY KEY (MANDT, MATNR, SPRAS));

/* ============================================================================
   4. SALES ORDERS
   ============================================================================ */

-- Sales document: header
CREATE TABLE sap.VBAK (
    MANDT   CHAR(3)       NOT NULL,
    VBELN   CHAR(10)      NOT NULL,  -- Sales document number
    ERDAT   CHAR(8)       NULL,      -- Created on
    ERZET   CHAR(6)       NULL,      -- Created at
    ERNAM   NVARCHAR(12)  NULL,      -- Created by
    AUDAT   CHAR(8)       NULL,      -- Document date
    AUART   CHAR(4)       NULL,      -- Sales document type
    VBTYP   CHAR(1)       NULL,      -- SD document category
    VKORG   CHAR(4)       NULL,
    VTWEG   CHAR(2)       NULL,
    SPART   CHAR(2)       NULL,
    VKGRP   CHAR(3)       NULL,      -- Sales group
    VKBUR   CHAR(4)       NULL,      -- Sales office
    KUNNR   CHAR(10)      NULL,      -- Sold-to party
    BSTNK   NVARCHAR(20)  NULL,      -- Customer PO number
    BSTDK   CHAR(8)       NULL,      -- Customer PO date
    NETWR   DECIMAL(15,2) NULL,      -- Net value of document
    WAERK   CHAR(5)       NULL,      -- Document currency
    VDATU   CHAR(8)       NULL,      -- Requested delivery date
    LIFSK   CHAR(2)       NULL,      -- Delivery block (header)
    FAKSK   CHAR(2)       NULL,      -- Billing block
    CMGST   CHAR(1)       NULL,      -- Overall credit status (A/B/C/D)
    -- S/4HANA: status fields live on VBAK, not VBUK
    GBSTK   CHAR(1)       NULL,      -- Overall processing status (A/B/C)
    LFSTK   CHAR(1)       NULL,      -- Delivery status
    FKSTK   CHAR(1)       NULL,      -- Billing status
    CONSTRAINT PK_VBAK PRIMARY KEY (MANDT, VBELN));

-- Sales document: item
CREATE TABLE sap.VBAP (
    MANDT   CHAR(3)       NOT NULL,
    VBELN   CHAR(10)      NOT NULL,
    POSNR   CHAR(6)       NOT NULL,  -- Item number ('000010', '000020'...)
    MATNR   CHAR(18)      NULL,
    ARKTX   NVARCHAR(40)  NULL,      -- Short text
    PSTYV   CHAR(4)       NULL,      -- Item category
    WERKS   CHAR(4)       NULL,      -- Plant
    LGORT   CHAR(4)       NULL,      -- Storage location
    KWMENG  DECIMAL(15,3) NULL,      -- Cumulative order quantity
    VRKME   CHAR(3)       NULL,      -- Sales unit
    NETWR   DECIMAL(15,2) NULL,      -- Net value of item
    WAERK   CHAR(5)       NULL,
    NETPR   DECIMAL(11,2) NULL,      -- Net price
    KZWI1   DECIMAL(15,2) NULL,      -- Subtotal 1 (list value)
    WAVWR   DECIMAL(15,2) NULL,      -- Cost
    MATKL   CHAR(9)       NULL,
    ABGRU   CHAR(2)       NULL,      -- Reason for rejection
    LFSTA   CHAR(1)       NULL,      -- Delivery status (item)
    FKSTA   CHAR(1)       NULL,      -- Billing status (item)
    CONSTRAINT PK_VBAP PRIMARY KEY (MANDT, VBELN, POSNR));

-- Sales document: schedule lines  (the OTIF denominator lives here)
CREATE TABLE sap.VBEP (
    MANDT   CHAR(3)       NOT NULL,
    VBELN   CHAR(10)      NOT NULL,
    POSNR   CHAR(6)       NOT NULL,
    ETENR   CHAR(4)       NOT NULL,  -- Schedule line number
    EDATU   CHAR(8)       NULL,      -- Schedule line date (confirmed)
    WMENG   DECIMAL(15,3) NULL,      -- Order quantity in sales units
    BMENG   DECIMAL(15,3) NULL,      -- Confirmed quantity
    LMENG   DECIMAL(15,3) NULL,      -- Required quantity
    ETTYP   CHAR(2)       NULL,      -- Schedule line category
    CONSTRAINT PK_VBEP PRIMARY KEY
        (MANDT, VBELN, POSNR, ETENR));

-- Sales document: business data (payment terms, Incoterms)
CREATE TABLE sap.VBKD (
    MANDT   CHAR(3)   NOT NULL,
    VBELN   CHAR(10)  NOT NULL,
    POSNR   CHAR(6)   NOT NULL,      -- '000000' = header level
    ZTERM   CHAR(4)   NULL,          -- Payment terms
    INCO1   CHAR(3)   NULL,          -- Incoterms part 1
    INCO2   NVARCHAR(28) NULL,       -- Incoterms part 2
    BSTKD   NVARCHAR(35) NULL,       -- Customer PO
    KURSK   DECIMAL(9,5) NULL,       -- Exchange rate
    CONSTRAINT PK_VBKD PRIMARY KEY (MANDT, VBELN, POSNR));

-- Sales document: partner functions
-- (sold-to / ship-to / bill-to / payer can all differ — this is why
--  customer analysis in SAP is harder than it looks)
CREATE TABLE sap.VBPA (
    MANDT   CHAR(3)   NOT NULL,
    VBELN   CHAR(10)  NOT NULL,
    POSNR   CHAR(6)   NOT NULL,
    PARVW   CHAR(2)   NOT NULL,      -- Partner function AG/WE/RE/RG
    KUNNR   CHAR(10)  NULL,
    ADRNR   CHAR(10)  NULL,
    CONSTRAINT PK_VBPA PRIMARY KEY
        (MANDT, VBELN, POSNR, PARVW));

/* ============================================================================
   5. DELIVERIES
   ============================================================================ */

CREATE TABLE sap.LIKP (
    MANDT   CHAR(3)       NOT NULL,
    VBELN   CHAR(10)      NOT NULL,  -- Delivery number
    ERDAT   CHAR(8)       NULL,
    ERZET   CHAR(6)       NULL,
    LFART   CHAR(4)       NULL,      -- Delivery type
    VBTYP   CHAR(1)       NULL,
    VKORG   CHAR(4)       NULL,
    KUNNR   CHAR(10)      NULL,      -- Ship-to party
    KUNAG   CHAR(10)      NULL,      -- Sold-to party
    LFDAT   CHAR(8)       NULL,      -- Delivery date
    WADAT   CHAR(8)       NULL,      -- Planned goods movement date
    WADAT_IST CHAR(8)     NULL,      -- ACTUAL goods issue date  <- OTIF numerator
    KODAT   CHAR(8)       NULL,      -- Picking date
    LDDAT   CHAR(8)       NULL,      -- Loading date
    BTGEW   DECIMAL(15,3) NULL,      -- Total weight
    GEWEI   CHAR(3)       NULL,
    ANZPK   DECIMAL(5,0)  NULL,      -- Number of packages
    VSBED   CHAR(2)       NULL,      -- Shipping conditions
    ROUTE   CHAR(6)       NULL,
    WBSTK   CHAR(1)       NULL,      -- Goods movement status
    LVSTK   CHAR(1)       NULL,      -- Warehouse status
    CONSTRAINT PK_LIKP PRIMARY KEY (MANDT, VBELN));

CREATE TABLE sap.LIPS (
    MANDT   CHAR(3)       NOT NULL,
    VBELN   CHAR(10)      NOT NULL,  -- Delivery number
    POSNR   CHAR(6)       NOT NULL,
    MATNR   CHAR(18)      NULL,
    WERKS   CHAR(4)       NULL,
    LGORT   CHAR(4)       NULL,
    LFIMG   DECIMAL(13,3) NULL,      -- Actual delivered quantity
    MEINS   CHAR(3)       NULL,
    VRKME   CHAR(3)       NULL,
    VGBEL   CHAR(10)      NULL,      -- Reference doc (sales order)  <- FK
    VGPOS   CHAR(6)       NULL,      -- Reference item
    CHARG   CHAR(10)      NULL,      -- Batch
    NETWR   DECIMAL(15,2) NULL,
    WAERK   CHAR(5)       NULL,
    CONSTRAINT PK_LIPS PRIMARY KEY (MANDT, VBELN, POSNR));

/* ============================================================================
   6. BILLING
   ============================================================================ */

CREATE TABLE sap.VBRK (
    MANDT   CHAR(3)       NOT NULL,
    VBELN   CHAR(10)      NOT NULL,  -- Billing document number
    FKART   CHAR(4)       NULL,      -- Billing type (F2/G2/RE)
    VBTYP   CHAR(1)       NULL,
    FKDAT   CHAR(8)       NULL,      -- Billing date
    ERDAT   CHAR(8)       NULL,
    BUKRS   CHAR(4)       NULL,
    VKORG   CHAR(4)       NULL,
    KUNRG   CHAR(10)      NULL,      -- Payer
    KUNAG   CHAR(10)      NULL,      -- Sold-to party
    NETWR   DECIMAL(15,2) NULL,      -- Net value
    MWSBK   DECIMAL(13,2) NULL,      -- Tax amount
    WAERK   CHAR(5)       NULL,
    KURRF   DECIMAL(9,5)  NULL,      -- Exchange rate for FI
    ZTERM   CHAR(4)       NULL,
    VALDT   CHAR(8)       NULL,      -- Fixed value date
    RFBSK   CHAR(1)       NULL,      -- Status: forwarded to accounting
    FKSTO   CHAR(1)       NULL,      -- Cancelled flag
    BELNR   CHAR(10)      NULL,      -- Accounting document -> BKPF
    CONSTRAINT PK_VBRK PRIMARY KEY (MANDT, VBELN));

CREATE TABLE sap.VBRP (
    MANDT   CHAR(3)       NOT NULL,
    VBELN   CHAR(10)      NOT NULL,
    POSNR   CHAR(6)       NOT NULL,
    MATNR   CHAR(18)      NULL,
    ARKTX   NVARCHAR(40)  NULL,
    WERKS   CHAR(4)       NULL,
    FKIMG   DECIMAL(13,3) NULL,      -- Billed quantity
    VRKME   CHAR(3)       NULL,
    NETWR   DECIMAL(15,2) NULL,
    WAVWR   DECIMAL(15,2) NULL,      -- Cost
    KZWI1   DECIMAL(15,2) NULL,      -- Subtotal (list value)
    MATKL   CHAR(9)       NULL,
    AUBEL   CHAR(10)      NULL,      -- Sales order        <- FK
    AUPOS   CHAR(6)       NULL,      -- Sales order item
    VGBEL   CHAR(10)      NULL,      -- Reference (delivery) <- FK
    VGPOS   CHAR(6)       NULL,
    KNUMV   CHAR(10)      NULL,      -- Condition record -> PRCD_ELEMENTS
    CONSTRAINT PK_VBRP PRIMARY KEY (MANDT, VBELN, POSNR));

-- Pricing conditions (S/4: replaces KONV). Source of revenue-leakage analysis.
CREATE TABLE sap.PRCD_ELEMENTS (
    CLIENT  CHAR(3)       NOT NULL,
    KNUMV   CHAR(10)      NOT NULL,  -- Condition record number
    KPOSN   CHAR(6)       NOT NULL,  -- Item
    STUNR   CHAR(3)       NOT NULL,  -- Step number
    ZAEHK   CHAR(2)       NOT NULL,  -- Counter
    KSCHL   CHAR(4)       NULL,      -- Condition type (PR00/K007/ZDIS...)
    KBETR   DECIMAL(11,2) NULL,      -- Rate
    KWERT   DECIMAL(15,2) NULL,      -- Condition value
    WAERS   CHAR(5)       NULL,
    KRECH   CHAR(1)       NULL,      -- Calculation type
    CONSTRAINT PK_PRCD PRIMARY KEY
        (CLIENT, KNUMV, KPOSN, STUNR, ZAEHK));

/* ============================================================================
   7. FINANCE / ACCOUNTS RECEIVABLE
   ============================================================================ */

CREATE TABLE sap.BKPF (
    MANDT   CHAR(3)   NOT NULL,
    BUKRS   CHAR(4)   NOT NULL,
    BELNR   CHAR(10)  NOT NULL,      -- Accounting document number
    GJAHR   CHAR(4)   NOT NULL,      -- Fiscal year
    BLART   CHAR(2)   NULL,          -- Document type (RV/DZ)
    BLDAT   CHAR(8)   NULL,          -- Document date
    BUDAT   CHAR(8)   NULL,          -- Posting date
    CPUDT   CHAR(8)   NULL,          -- Entry date
    WAERS   CHAR(5)   NULL,
    KURSF   DECIMAL(9,5) NULL,
    AWKEY   CHAR(20)  NULL,          -- Reference key (billing doc)
    CONSTRAINT PK_BKPF PRIMARY KEY
        (MANDT, BUKRS, BELNR, GJAHR));

-- AR OPEN items
CREATE TABLE sap.BSID (
    MANDT   CHAR(3)       NOT NULL,
    BUKRS   CHAR(4)       NOT NULL,
    KUNNR   CHAR(10)      NOT NULL,
    UMSKS   CHAR(1)       NOT NULL,
    UMSKZ   CHAR(1)       NOT NULL,
    AUGDT   CHAR(8)       NOT NULL,  -- Clearing date ('00000000' when open)
    AUGBL   CHAR(10)      NOT NULL,  -- Clearing document ('' when open)
    ZUONR   CHAR(18)      NOT NULL,
    GJAHR   CHAR(4)       NOT NULL,
    BELNR   CHAR(10)      NOT NULL,
    BUZEI   CHAR(3)       NOT NULL,  -- Line item
    BUDAT   CHAR(8)       NULL,      -- Posting date
    BLDAT   CHAR(8)       NULL,
    WAERS   CHAR(5)       NULL,
    XBLNR   CHAR(16)      NULL,      -- Reference (billing doc)
    BLART   CHAR(2)       NULL,
    SHKZG   CHAR(1)       NULL,      -- Debit/credit indicator S/H
    DMBTR   DECIMAL(13,2) NULL,      -- Amount in local currency
    WRBTR   DECIMAL(13,2) NULL,      -- Amount in document currency
    ZFBDT   CHAR(8)       NULL,      -- Baseline payment date
    ZBD1T   DECIMAL(3,0)  NULL,      -- Cash discount days 1
    ZTERM   CHAR(4)       NULL,
    REBZG   CHAR(10)      NULL,      -- Invoice reference
    CONSTRAINT PK_BSID PRIMARY KEY
        (MANDT, BUKRS, KUNNR, UMSKS, UMSKZ, AUGDT, AUGBL,
         ZUONR, GJAHR, BELNR, BUZEI));

-- AR CLEARED items (same structure — SAP's classic open/cleared split)
CREATE TABLE sap.BSAD (
    MANDT   CHAR(3)       NOT NULL,
    BUKRS   CHAR(4)       NOT NULL,
    KUNNR   CHAR(10)      NOT NULL,
    UMSKS   CHAR(1)       NOT NULL,
    UMSKZ   CHAR(1)       NOT NULL,
    AUGDT   CHAR(8)       NOT NULL,  -- Clearing date (always populated here)
    AUGBL   CHAR(10)      NOT NULL,
    ZUONR   CHAR(18)      NOT NULL,
    GJAHR   CHAR(4)       NOT NULL,
    BELNR   CHAR(10)      NOT NULL,
    BUZEI   CHAR(3)       NOT NULL,
    BUDAT   CHAR(8)       NULL,
    BLDAT   CHAR(8)       NULL,
    WAERS   CHAR(5)       NULL,
    XBLNR   CHAR(16)      NULL,
    BLART   CHAR(2)       NULL,
    SHKZG   CHAR(1)       NULL,
    DMBTR   DECIMAL(13,2) NULL,
    WRBTR   DECIMAL(13,2) NULL,
    ZFBDT   CHAR(8)       NULL,
    ZBD1T   DECIMAL(3,0)  NULL,
    ZTERM   CHAR(4)       NULL,
    REBZG   CHAR(10)      NULL,
    CONSTRAINT PK_BSAD PRIMARY KEY
        (MANDT, BUKRS, KUNNR, UMSKS, UMSKZ, AUGDT, AUGBL,
         ZUONR, GJAHR, BELNR, BUZEI));

/* ============================================================================
   8. DOCUMENT FLOW  —  the graph at the heart of this project
   ============================================================================ */

CREATE TABLE sap.VBFA (
    MANDT   CHAR(3)       NOT NULL,
    VBELV   CHAR(10)      NOT NULL,  -- Preceding document
    POSNV   CHAR(6)       NOT NULL,  -- Preceding item
    VBELN   CHAR(10)      NOT NULL,  -- Subsequent document
    POSNN   CHAR(6)       NOT NULL,  -- Subsequent item
    VBTYP_N CHAR(1)       NOT NULL,  -- Subsequent doc category
                                     --   C=order J=delivery M=invoice
                                     --   O=credit memo H=return
    VBTYP_V CHAR(1)       NULL,      -- Preceding doc category
    RFMNG   DECIMAL(15,3) NULL,      -- Referenced quantity
    MEINS   CHAR(3)       NULL,
    RFWRT   DECIMAL(15,2) NULL,      -- Referenced value
    WAERS   CHAR(5)       NULL,
    ERDAT   CHAR(8)       NULL,
    PLMIN   CHAR(1)       NULL,      -- Plus/minus sign
    CONSTRAINT PK_VBFA PRIMARY KEY
        (MANDT, VBELV, POSNV, VBELN, POSNN, VBTYP_N));

/* ============================================================================
   9. CHANGE DOCUMENTS  —  order volatility analysis
   ============================================================================ */

CREATE TABLE sap.CDHDR (
    MANDANT     CHAR(3)      NOT NULL,
    OBJECTCLAS  CHAR(15)     NOT NULL, -- 'VERKBELEG'
    OBJECTID    CHAR(90)     NOT NULL, -- Sales document number
    CHANGENR    CHAR(10)     NOT NULL,
    USERNAME    NVARCHAR(12) NULL,
    UDATE       CHAR(8)      NULL,
    UTIME       CHAR(6)      NULL,
    TCODE       CHAR(20)     NULL,     -- 'VA02'
    CHANGE_IND  CHAR(1)      NULL,     -- U=update I=insert D=delete
    CONSTRAINT PK_CDHDR PRIMARY KEY
        (MANDANT, OBJECTCLAS, OBJECTID, CHANGENR));

CREATE TABLE sap.CDPOS (
    MANDANT     CHAR(3)       NOT NULL,
    OBJECTCLAS  CHAR(15)      NOT NULL,
    OBJECTID    CHAR(90)      NOT NULL,
    CHANGENR    CHAR(10)      NOT NULL,
    TABNAME     CHAR(30)      NOT NULL, -- VBAK / VBAP / VBEP
    TABKEY      CHAR(70)      NOT NULL,
    FNAME       CHAR(30)      NOT NULL, -- VDATU / KWMENG / NETPR ...
    CHNGIND     CHAR(1)       NOT NULL,
    VALUE_OLD   NVARCHAR(254) NULL,
    VALUE_NEW   NVARCHAR(254) NULL,
    CONSTRAINT PK_CDPOS PRIMARY KEY
        (MANDANT, OBJECTCLAS, OBJECTID, CHANGENR,
         TABNAME, TABKEY, FNAME, CHNGIND));
GO

/* ============================================================================
   NOTES FOR THE SILVER LAYER  (do not skip these — they are the real work)

   1. DATS parsing: every CHAR(8) date needs '00000000' -> NULL before
      conversion. A naive CAST throws; a naive TRY_CAST silently yields the
      wrong open/closed classification for AR.
   2. Leading zeros: VBELN/KUNNR/MATNR carry them. Strip consistently or joins
      across tables that store them differently will silently drop rows.
   3. AR open vs cleared: BSID and BSAD must be UNIONed into one AR fact.
      An item exists in exactly one of them at any time.
   4. Delivery quantity: LIPS.LFIMG is the delivered quantity; VBAP.KWMENG is
      ordered. In-full is a comparison of the two, aggregated to order-item,
      because one item may span several deliveries.
   5. OTIF on-time: LIKP.WADAT_IST (actual GI) vs the CUSTOMER requested date
      VBAK.VDATU — not the internally confirmed VBEP.EDATU. Measuring against
      the confirmed date is the single most common way OTIF gets flattered.
   6. Decimal -> Double when promoting to Gold. Fabric Graph cannot read
      Decimal and returns null for every such property.
   ============================================================================ */
