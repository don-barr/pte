# Billing Code Modifiers

How we understand modifiers in CMS Transparency in Coverage (TiC) in-network files, what they
look like in our data, and how they should affect search and price comparison.

This is our working understanding, not an authoritative coding reference. Numbers come from
`data/parsed/2026-09-28_fidelis_index.db` loaded from both Fidelis in-network files (`fidelis-ex`, `fidelis-es`;
1,185,888 rate rows).

## What A Modifier Is

A modifier is a two-character code attached to a billing code. The billing code says **what**
was done, and the modifier adds **how, by whom, or which part**.

`90853` is group psychotherapy. `90853` with `95` is group psychotherapy delivered by video.
Same service, different circumstances, and often a different price.

In the TiC schema, modifiers are `billing_code_modifier`, an optional array of strings on
each negotiated price. The CMS spec says that when a modifier changes the price, the payer
must publish a separate price object for it. **So code + modifiers together identify a
price.**

## The Two Code Systems

| System | Looks Like | Examples | Owned By | Can We Use The Definitions Freely? |
|---|---|---|---|---|
| CPT (Level I) | two digits | `26`, `59`, `95`, `25`, `50` | AMA | No. The official text is copyrighted. Short labels in our own words are fine. |
| HCPCS Level II | letter + letter/digit | `TC`, `GT`, `AJ`, `GP`, `XU` | CMS | Yes. Published in the quarterly Alpha-Numeric HCPCS file. |

Sources:
- HCPCS Level II modifiers: the Alpha-Numeric HCPCS file on the
  [CMS HCPCS Quarterly Update](https://www.cms.gov/medicare/coding/hcpcsreleasecodesets/hcpcs-quarterly-update.html)
  page.
- Which modifiers each code allows: the Medicare Physician Fee Schedule (MPFS) Relative Value
  File. It has one row per code with indicators such as PC/TC (can the code be split into
  `26`/`TC`), bilateral (`50`) and multiple procedure (`51`).

## Categories

We group modifiers by **what they do to the price**. The grouping is ours, not an official
one, and it drives how rates should be compared.

| Category | What It Means | Examples In Our Data | Effect On Price |
|---|---|---|---|
| **component** | Only part of the service is being paid | `26` professional (reading the result), `TC` technical (equipment and staff) | Large. Each is a fraction of the whole. |
| **delivery_mode** | How the service was delivered | `95` video telehealth, `GT` telehealth (older code), `GQ` asynchronous telehealth | Varies by contract. Some pay the same as in person, some don't. |
| **provider_type** | The kind of clinician who did it | `AJ` clinical social worker, `AH` clinical psychologist, `SA` nurse practitioner with physician, `AF` specialty physician, `HO`/`HP` credential levels | Often large. Non-physicians are commonly paid a percentage of the physician rate. |
| **therapy_plan** | Which therapy discipline's plan of care | `GP` physical therapy, `GO` occupational therapy, `GN` speech therapy | Usually required for billing rather than a price change, but publishers may list rates separately. |
| **bundling** | Whether the service is paid separately from others in the same visit | `59`, `XE` separate encounter, `XS` separate structure, `XU` unusual non-overlapping service, `XP` separate practitioner, `25` separate E/M visit | Indirect. Decides whether the service is paid at all, not how much. |
| **anatomy** | Which side or body part | `RT`, `LT`, `50` bilateral, `E1`–`E4` eyelids, `F7`/`F8` toes | `50` usually raises the price (commonly 150%). `RT`/`LT` usually don't. |
| **work_amount** | More or less work than usual | `22` increased services, `52` reduced services, `51` multiple procedures | Raises or cuts by a percentage. |
| **surgical_role** | Who did what in surgery | `AS` assistant at surgery, `54` surgical care only, `57` decision for surgery, `58` staged procedure, `79` unrelated procedure in follow-up period | Large for `AS` and `54`, which pay a fraction. |
| **repeat_or_lab** | Repeated or outside services | `76`/`77` repeat procedure, `90` outside lab, `91` repeat lab test, `QW` CLIA-waived test | Usually the same price. Mostly informational. |
| **teaching** | Supervision by a teaching physician | `GC`, `GE` | Usually the same price. Informational. |
| **other** | Equipment, drugs, coverage status | `NU`/`RR` new/rented equipment, `JW`/`JZ` drug waste, `KX`/`GA`/`GY` coverage notices, `Q1` clinical trial, `33` preventive | Varies. Treat as distinct unless we know otherwise. |

## What Our Data Looks Like

**Most rates have no modifier.** 566,215 of 1,185,888 rows (48%) have none. There are 179
distinct modifier combinations built from 98 distinct modifiers.

**The most common modifiers** (rows that include them):

| Modifier | Rows | Category |
|---|---|---|
| `26` | 156,925 | component |
| `59` | 88,222 | bundling |
| `GP` | 80,184 | therapy_plan |
| `95` | 75,306 | delivery_mode |
| `RT` | 46,480 | anatomy |
| `25` | 44,024 | bundling |
| `LT` | 42,370 | anatomy |
| `GT` | 33,840 | delivery_mode |
| `GC` | 30,674 | teaching |
| `QW` | 25,045 | repeat_or_lab |

**Combinations are common.** For example `95,GT` (7,375 rows), `RT,LT` (14,989),
`59,GP` (18,565) and `AH,95,GT`. A price object with several modifiers is one price that
applies when all of them are billed together.

**Order is consistent here.** No combination appears in two different orders in the Fidelis
files (for example both `95,GT` and `GT,95`). Other payers may differ, so the ETL sorts them
(see below).

## Examples

### Component Modifiers Split A Service In Parts: Chest X-Ray `71046`

| Modifier | Median Rate | Rows |
|---|---|---|
| `26` (reading the X-ray) | $9.99 | 1,216 |
| `TC` (taking the X-ray) | $22.32 | 1,024 |
| none (the whole service) | not published | 0 |

Comparing a `26` rate with a full-service rate would make the `26` provider look 70% cheaper
when it is simply being paid for less work. Note too that the whole-service price for this
code doesn't appear at all, so it can't be rebuilt reliably from the two parts.

### Telehealth And Place Of Service: Psychotherapy `90834` (45 Minutes)

| Modifiers | Place Of Service | Rate Range | Rows |
|---|---|---|---|
| `95` | `10` (telehealth, patient's home) | $48.30 – $144.62 | 6,641 |
| `95` | `02` (telehealth, not at home) | $71.02 – $140.00 | 1,129 |
| `GT` | `02` | $37.79 – $108.50 | 1,038 |
| `95,GT` | `02` | $84.00 | 1,065 |
| `AJ` | `11` (office) | $46.31 – $100.14 | 117 |
| `AJ` | `10` | $120.17 | 360 |

Modifiers and place of service vary together. `95` almost always comes with place of service
`02` or `10`, so a price difference between rows can be caused by either one. Hold both
fixed before comparing.

### Place Of Service Often Matters More Than The Modifier

How much of the variation in price (log of the rate, CPT/HCPCS rows only) is explained by
grouping on the modifier set, the place of service, or both:

| Code | Rows | Modifier | Place Of Service | Both |
|---|---|---|---|---|
| `99213` office visit, established patient | 19,703 | 10% | 61% | 70% |
| `99214` office visit, established patient | 24,262 | 34% | 68% | 78% |
| `90837` psychotherapy, 60 minutes | 14,809 | 31% | 63% | 78% |
| `90834` psychotherapy, 45 minutes | 13,572 | 24% | 14% | 45% |

For three of the four codes, place of service explains far more than the modifier. `90834` is
the exception, and there neither explains much on its own. The rest (22–55%) is mostly which
provider. This is why results are grouped on both, not on the modifier alone.

### Bundling Modifier With No Base Rate: Fine Needle Aspiration `10006`

The test fixture has only a `59` rate for this code. `59` doesn't normally change the amount.
It says the service is paid separately instead of being folded into another procedure.
Publishing only the `59` rate suggests that without `59` the service is bundled, which
effectively pays $0. **A missing unmodified rate does not mean the unmodified price is the
same.**

## Comparison Rules

**Default: rates are comparable only when the code, the full modifier set, billing class
and place of service all match.**

```
comparison key = (billing_code, sorted(modifiers), billing_class, service_codes)
```

For a revenue-code (RC) price, the procedure code in `additional_information` joins the key
(see "Procedure codes on revenue-code prices" below):

```
comparison key = (billed_as, procedure, sorted(modifiers), billing_class, service_codes)
```

`billed_as` is `CPT 99213` for a rate filed under the code itself, or `RC 0510` for a hospital
rate filed under a revenue code. `procedure` is `additional_information`, or empty when there
is none. The app uses exactly this key (`GROUP_COLUMNS` in `api/search.py`).

"No modifier" is its own value (empty), not a wildcard. It does not match `26`, and `26`
does not match it.

By category:

| Category | Merge With Other Modifiers When Comparing? | Why |
|---|---|---|
| component | **Never** | Each one is a different fraction of the whole service. |
| delivery_mode | Only for a deliberate telehealth analysis | `95` and `GT` mean the same thing clinically, but our data prices them differently. |
| provider_type | Never in general search; compare deliberately to study credential pricing | A real price difference by clinician type. |
| anatomy (`RT`/`LT`) | Usually safe to merge | Side of the body rarely changes the price. `50` is the exception and should stay separate. |
| teaching, repeat_or_lab | Usually safe to merge | Informational. Confirm against the data before relying on it. |
| everything else | Never | Assume it changes the price unless shown otherwise. |

## How Our ETL Stores Modifiers

- `rates.modifiers` is a comma-joined string, **sorted**, so the same set listed in another
  order (`GT,95` vs `95,GT`) is one key. Place-of-service lists (`service_codes`) are sorted the
  same way. For Fidelis this changes nothing (179 distinct combinations before and after).
- A price with no modifier is stored as an empty string `''`, not `NULL`. This matters
  because `NULL` never equals anything in SQL, so `NULL` would drop out of `GROUP BY` and
  joins without warning.
- Sorting means the stored order is not the file's order; the order of modifiers carries no
  pricing meaning in the CMS schema, so nothing is lost.

## Procedure Codes On Revenue-Code Prices (`additional_information`)

A revenue code (RC) names a hospital **department** (`0510` clinic, `0260` IV therapy), not a
procedure. For RC prices Centene puts the actual **procedure** code (CPT or HCPCS, for example
`99213`, `45380`, `96360`) in the price's free-text `additional_information` field.

- 508,243 of 1,185,888 rates carry one, and all of them are RC rates (508,243 of 566,076 RC
  rates, 90%). No CPT or HCPCS rate has one.
- They hold 711 distinct procedure codes.
- Our ETL now stores it as `rates.additional_information` (`NULL` when the file gives none).

**RC 260 (IV therapy) shows why it matters.** Its coded prices are for `96360`/`96361`
(hydration, initial and add-on) and `96365`/`96366` (IV infusion, initial and add-on). The
code has five item names ("IV Therapy General" plus four IV-infusion names), and Centene
repeats the whole department price list under each name. So items with different names
looked identical: they are the same list. The procedure code, not the item name, tells the
prices apart.

**A CPT search also finds hospital rates.** A hospital's `99213` is filed as, say, RC `0510`
(clinic) with `99213` in `additional_information`. Searching `99213` includes these
institutional rates (13 revenue codes, most under `0510`, `0521` and `0520`), each in its own
price group by `billed_as`, so they are never mixed with the professional rates.

## Rates Are Not Provider Prices (Weighting)

A row is one price × provider group × NPI. One contract with a group of 30 clinicians is 30
rows with the same price, so counts and statistics by row lean toward large groups.

`99214` has 24,262 rows but only 2,430 provider prices (each EIN's price per price group and
file, counted once). That moves the median of `99214` · `25` · office (place of service `11`):

| Counted By | Count | Median |
|---|---|---|
| rows | 1,378 | $142.27 |
| provider prices | 59 | $161.64 |

The procedure page's **Count by** choice (Provider, NPI, Every row) sets what the table and
statistics count; Provider, each provider's price once, is the default.

## Decided (Were Open Questions)

- **Modifiers are both a filter and a column.** A user searching `71046` now sees each price
  next to its `26` or `TC`.
- **Results are grouped before prices are compared.** Search groups results by modifiers,
  place of service and billing class (plus `billed_as` and the procedure code for RC prices)
  and shows each group as a price-group chip with its own statistics.
- **A reference table labels modifiers.** `ui/labels.py` maps the common modifiers to short
  labels in our own words with the categories above, and places of service to short labels.
  Unknown codes show as the bare code. CPT modifier text stays out of the app: it is
  AMA-copyrighted.
