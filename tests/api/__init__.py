"""Shared fixture: a small rates DB whose values are copied verbatim from data/samples/ (build_db) or from the
loaded data/parsed/rates.db (the add_code_* helpers)."""

import sqlite3

from etl.schema import create_schema

BASE = "https://www.centene.com/content/dam/centene/Centene%20Corporate/json/DOCUMENT/"
EX_URL = BASE + "2026-09-28_centene-management-company-llc_fidelis-ex_in-network.json"
ES_URL = BASE + "2026-09-28_centene-management-company-llc_fidelis-es_in-network.json"

# Code 10006 from both sample files, and 10040 from fidelis-ex, each resolved to provider reference 1 or 2.
FNA = ("10006", "CPT", "2022", "FNA BX W/US GDN EA ADDL", "FINE NEEDLE ASPIRATION BX W/US GDN EA ADDL")
# Code 731 is two unrelated codes: CPT anesthesia (fidelis-ex) and RC ECG monitoring (fidelis-es).
ANES_731 = ("731", "CPT", "2022", "ANES UPR GI NDSC PX NOS",
            "ANESTHESIA FOR UPPER GASTROINTESTINAL ENDOSCOPIC PROCEDURES, ENDOSCOPE INTRODUCED PROXIMAL TO DUODEN")
ECG_731 = ("731", "RC", "2022", "EKG/ECG Electrocardiogram Holter monitor", "EKG/ECG Electrocardiogram Holter monitor")
ACNE = (
    "10040", "CPT", "2022", "ACNE SURGERY",
    "ACNE SURGERY (EG, MARSUPIALIZATION, OPENING OR REMOVAL OF MULTIPLE MILIA, COMEDONES, CYSTS, PUSTULES",
)


def build_db(conn: sqlite3.Connection) -> None:
    create_schema(conn)
    conn.executemany(
        "INSERT INTO source_files (id, url, reporting_entity_name, reporting_entity_type, version,"
        " last_updated_on, rate_count, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (1, ES_URL, "Centene Management Company LLC", "Third Party Administrator", "2.0.0", "2026-10-06", 1, None),
            (2, EX_URL, "Centene Management Company LLC", "Third Party Administrator", "2.0.0", "2026-10-06", 2, None),
        ],
    )
    # Real plans from the index: one per file.
    conn.executemany(
        "INSERT INTO plans (source_file_id, plan_id, plan_id_type, plan_name, plan_market_type, issuer_name)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        [
            (1, "25303EP000", "hios", "Essential Plan 1", "individual", "Fidelis Care"),
            (2, "25303NY005", "hios", "Ambetter from Fidelis Care Bronze", "individual", "Fidelis Care"),
            (2, "25303NY017", "hios", "Ambetter from Fidelis Care Gold Zero Cost Sharing", "individual", "Fidelis Care"),
        ],
    )
    rows = [
        # fidelis-es listed first so "cheapest first" is not just insertion order.
        (1, *FNA, "negotiated", 76.84, "professional", "outpatient", "11", "59", "9999-12-31",
         1, 1396747861, "11-3471596", "Samaritan Medical Center"),
        (2, *FNA, "negotiated", 69.85, "professional", "outpatient", "11", "59", "9999-12-31",
         1, 1396747861, "11-3471596", "Minkowitz Consulting Pathologists PC"),
        (2, *ACNE, "negotiated", 103.51, "professional", "outpatient", "11", "59", "9999-12-31",
         2, None, None, None),
    ]
    conn.executemany(
        "INSERT INTO rates (source_file_id, billing_code, billing_code_type, billing_code_type_version, name,"
        " description, negotiated_type, negotiated_rate, billing_class, setting, service_codes, modifiers,"
        " expiration_date, provider_reference_id, npi, ein, business_name)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()


def add_code_731(conn: sqlite3.Connection) -> None:
    """Real rows for 731, one number that is two codes: CPT anesthesia and RC ECG monitoring."""
    conn.executemany(
        "INSERT INTO rates (source_file_id, billing_code, billing_code_type, billing_code_type_version, name,"
        " description, negotiated_type, negotiated_rate, billing_class, setting, service_codes, modifiers,"
        " expiration_date, provider_reference_id, npi, ein, business_name)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (2, *ANES_731, "negotiated", 23.81, "professional", "outpatient", "11", "AA", "9999-12-31",
             518, 1861459505, "45-0950946", "Chinese American IPA Assoc/CAIPA"),
            (1, *ECG_731, "negotiated", 167.81, "institutional", "outpatient", "50", "", "9999-12-31",
             309, 1558356741, "13-4019319", "Med Alliance Medical Health Services"),
        ],
    )
    conn.commit()


INSERT_RATE = (
    "INSERT INTO rates (source_file_id, billing_code, billing_code_type, billing_code_type_version, name,"
    " description, negotiated_type, negotiated_rate, billing_class, setting, service_codes, modifiers,"
    " expiration_date, provider_reference_id, npi, ein, business_name)"
    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)

# The other 61 real 10040 rates (all from fidelis-es, all place of service 11), copied from the loaded DB:
# (rate, modifiers, provider reference, NPI, EIN, business name). With build_db's one fidelis-ex row
# (59, $103.51) that makes all 62: 51 x23 $145.97, 59 x16 $69.67-$140.17, 79,51 x16 $144.12, XS x7 $145.97.
ACNE_ES_ROWS = [
    (145.97, '51', 3, 1033684915, '45-1535116', 'Anesthesiology Department of Mount Sinai'),
    (145.97, '51', 3, 1417360249, '45-1535116', 'Anesthesiology Department of Mount Sinai'),
    (145.97, '51', 3, 1487841755, '45-1535116', 'Anesthesiology Department of Mount Sinai'),
    (145.97, '51', 3, 1558798694, '45-1535116', 'Anesthesiology Department of Mount Sinai'),
    (145.97, '51', 3, 1578030243, '45-1535116', 'Anesthesiology Department of Mount Sinai'),
    (145.97, '51', 3, 1598283434, '45-1535116', 'Anesthesiology Department of Mount Sinai'),
    (145.97, '51', 3, 1629505870, '45-1535116', 'Anesthesiology Department of Mount Sinai'),
    (145.97, '51', 3, 1801141148, '45-1535116', 'Anesthesiology Department of Mount Sinai'),
    (145.97, '51', 3, 1962692152, '45-1535116', 'Anesthesiology Department of Mount Sinai'),
    (145.97, '51', 3, 1982940268, '45-1535116', 'Anesthesiology Department of Mount Sinai'),
    (145.97, '51', 3, 1164501086, '11-2560903', 'Teladoc Physicians PC'),
    (145.97, '51', 3, 1174089379, '11-2560903', 'Teladoc Physicians PC'),
    (145.97, '51', 3, 1215566856, '11-2560903', 'Teladoc Physicians PC'),
    (145.97, '51', 3, 1255892659, '11-2560903', 'Teladoc Physicians PC'),
    (145.97, '51', 3, 1326720087, '11-2560903', 'Teladoc Physicians PC'),
    (145.97, '51', 3, 1528527421, '11-2560903', 'Teladoc Physicians PC'),
    (145.97, '51', 3, 1538520192, '11-2560903', 'Teladoc Physicians PC'),
    (145.97, '51', 3, 1538657523, '11-2560903', 'Teladoc Physicians PC'),
    (145.97, '51', 3, 1649672650, '11-2560903', 'Teladoc Physicians PC'),
    (145.97, '51', 3, 1740848985, '11-2560903', 'Teladoc Physicians PC'),
    (145.97, '51', 3, 1750525341, '11-2560903', 'Teladoc Physicians PC'),
    (145.97, '51', 3, 1922606854, '11-2560903', 'Teladoc Physicians PC'),
    (145.97, '51', 3, 1992330153, '11-2560903', 'Teladoc Physicians PC'),
    (69.67, '59', 2, 1184668428, '84-2625611', 'Buffalo-Niagara Hospitalists, LLC'),
    (69.67, '59', 2, 1295230837, '85-3378316', 'Crouse Radiology Associates, LLP'),
    (69.67, '59', 2, 1083718597, '81-2775347', 'Emergency Physician Services of New York PC'),
    (69.67, '59', 2, 1013177401, '37-1654147', 'Hicksville Family Medical Care, PLLC'),
    (69.67, '59', 2, 1477005742, '83-2668818', 'Lyudmila Sverkunova Medical, PC'),
    (69.67, '59', 2, 1265164024, '11-3211425', 'MMC Faculty Practice'),
    (69.67, '59', 2, 1427387448, '85-1911011', 'NYU Anesthesia Associates'),
    (69.67, '59', 2, 1811343809, '47-2261849', 'New York Network Management'),
    (69.67, '59', 2, 1881238087, '20-3910419', 'Premier Medical Group of the Hudson Valley, PC'),
    (69.67, '59', 2, 1174174494, '82-1293810', 'Syracuse Community Health Center, Inc'),
    (140.17, '59', 5, 1730151218, '11-3460764', 'Comprehensive Counseling, PLLC'),
    (140.17, '59', 5, 1023015245, '13-3186117', 'Memorial Anesthesiology Group'),
    (140.17, '59', 5, 1215995642, '45-3148303', 'Trinity Medical WNY, PC'),
    (140.17, '59', 5, 1649258286, '45-3148303', 'Trinity Medical WNY, PC'),
    (140.17, '59', 5, 1235250820, '46-1255974', 'Trustees of Columbia University in the City of New York'),
    (144.12, '79,51', 4, 1033684915, '45-1535116', 'Anesthesiology Department of Mount Sinai'),
    (144.12, '79,51', 4, 1417360249, '45-1535116', 'Anesthesiology Department of Mount Sinai'),
    (144.12, '79,51', 4, 1487841755, '45-1535116', 'Anesthesiology Department of Mount Sinai'),
    (144.12, '79,51', 4, 1558798694, '45-1535116', 'Anesthesiology Department of Mount Sinai'),
    (144.12, '79,51', 4, 1578030243, '45-1535116', 'Anesthesiology Department of Mount Sinai'),
    (144.12, '79,51', 4, 1598283434, '45-1535116', 'Anesthesiology Department of Mount Sinai'),
    (144.12, '79,51', 4, 1609229806, '45-1535116', 'Anesthesiology Department of Mount Sinai'),
    (144.12, '79,51', 4, 1629505870, '45-1535116', 'Anesthesiology Department of Mount Sinai'),
    (144.12, '79,51', 4, 1801141148, '45-1535116', 'Anesthesiology Department of Mount Sinai'),
    (144.12, '79,51', 4, 1962692152, '45-1535116', 'Anesthesiology Department of Mount Sinai'),
    (144.12, '79,51', 4, 1982940268, '45-1535116', 'Anesthesiology Department of Mount Sinai'),
    (144.12, '79,51', 4, 1174578769, '11-2560903', 'Teladoc Physicians PC'),
    (144.12, '79,51', 4, 1215566856, '11-2560903', 'Teladoc Physicians PC'),
    (144.12, '79,51', 4, 1538520192, '11-2560903', 'Teladoc Physicians PC'),
    (144.12, '79,51', 4, 1538657523, '11-2560903', 'Teladoc Physicians PC'),
    (144.12, '79,51', 4, 1750525341, '11-2560903', 'Teladoc Physicians PC'),
    (145.97, 'XS', 6, 1144210659, '11-2997945', 'Ramapo Radiology Mobile Services, PC'),
    (145.97, 'XS', 6, 1356308902, '11-2997945', 'Ramapo Radiology Mobile Services, PC'),
    (145.97, 'XS', 6, 1528590403, '11-2997945', 'Ramapo Radiology Mobile Services, PC'),
    (145.97, 'XS', 6, 1609866987, '11-2997945', 'Ramapo Radiology Mobile Services, PC'),
    (145.97, 'XS', 6, 1669450375, '11-2997945', 'Ramapo Radiology Mobile Services, PC'),
    (145.97, 'XS', 6, 1679572978, '11-2997945', 'Ramapo Radiology Mobile Services, PC'),
    (145.97, 'XS', 6, 1932187648, '11-2997945', 'Ramapo Radiology Mobile Services, PC'),
]


def add_code_10040(conn: sqlite3.Connection) -> None:
    """Every real 10040 rate: one code whose prices differ by billing modifier."""
    conn.executemany(INSERT_RATE, [
        (1, *ACNE, "negotiated", rate, "professional", "outpatient", "11", modifiers, "9999-12-31",
         ref, npi, ein, name)
        for rate, modifiers, ref, npi, ein, name in ACNE_ES_ROWS
    ])
    conn.commit()


OFFICE_VISIT = (
    "99213", "CPT", "2022", "OFFICE/OUTPATIENT VISIT EST",
    "OFFICE OR OTH OUTPATIENT VISIT FOR THE EVALUATION AND MANAGEMENT OF AN ESTABLISHED PATIENT, WHICH RE",
)


def add_code_99213(conn: sqlite3.Connection) -> None:
    """Seven real 99213 rates where the place of service changes the price: modifier 25 in the office
    (11, $66.14-$231.30) vs telehealth (02, $51.45-$66.74), plus one 25,95 rate for 02,10."""
    rows = [
        # (file, rate, service codes, modifiers, provider reference, NPI, EIN, business name)
        (1, 66.14, "11", "25", 1539, 1760007496, "76-0845975", "Maryam Broukhim MD"),
        (1, 231.3, "11", "25", 1397, 1831577006, "13-4068415", "Forme Medical Center"),
        (1, 231.3, "11", "25", 1397, 1083755920, "13-4068415", "Forme Medical Center"),
        (1, 231.3, "11", "25", 1397, 1295113231, "13-4068415", "Forme Medical Center"),
        (1, 66.74, "02", "25", 781, 1093913642, "27-5420642", "Agape Physical Therapy Services, PC"),
        (2, 51.45, "02", "25", 623, 1942573167, "46-2628853", "4N Medical Care, PC"),
        (2, 51.45, "02,10", "25,95", 1216, 1942573167, "46-2628853", "4N Medical Care, PC"),
    ]
    conn.executemany(INSERT_RATE, [
        (sid, *OFFICE_VISIT, "negotiated", rate, "professional", "outpatient", pos, modifiers, "9999-12-31",
         ref, npi, ein, name)
        for sid, rate, pos, modifiers, ref, npi, ein, name in rows
    ])
    conn.commit()


BEHAVIOR_ID = ("97151", "CPT", "2022", "BHV ID ASSMT BY PHYS/QHP", "BEHAVIOR ID ASSESSMENT BY PHYS/QHP EA 15 MIN")

# Every real 97151 rate (all fidelis-ex), copied from the loaded DB: (rate, place of service, modifiers,
# provider reference, NPI, EIN, business name). Most have no modifier; a few are GT (telehealth).
BEHAVIOR_ID_ROWS = [
    (1.0, '11', '', 1118, 1952781411, '47-3981360', 'Encore Licensed Behavior Analyst, PLLC'),
    (1.0, '11', '', 1119, 1952781411, '47-3981360', 'Encore Licensed Behavior Analyst, PLLC'),
    (8.75, '11', '', 1112, 1376993527, '45-5303337', 'Reaching Higher, Inc.'),
    (8.75, '11', '', 1116, 1376993527, '45-5303337', 'Reaching Higher, Inc.'),
    (10.0, '11', '', 1111, 1518477959, '82-2669171', 'Rachael Schneider Licensed Behavior Analyst, PLLC'),
    (10.0, '11', '', 1117, 1518477959, '82-2669171', 'Rachael Schneider Licensed Behavior Analyst, PLLC'),
    (12.5, '11', '', 1112, 1376993527, '45-5303337', 'Reaching Higher, Inc.'),
    (12.5, '11', '', 1116, 1376993527, '45-5303337', 'Reaching Higher, Inc.'),
    (18.5, '11', '', 1118, 1952781411, '47-3981360', 'Encore Licensed Behavior Analyst, PLLC'),
    (18.5, '11', '', 1119, 1952781411, '47-3981360', 'Encore Licensed Behavior Analyst, PLLC'),
    (19.75, '11', '', 1118, 1952781411, '47-3981360', 'Encore Licensed Behavior Analyst, PLLC'),
    (19.75, '11', '', 1119, 1952781411, '47-3981360', 'Encore Licensed Behavior Analyst, PLLC'),
    (21.0, '11', '', 1118, 1952781411, '47-3981360', 'Encore Licensed Behavior Analyst, PLLC'),
    (21.0, '11', '', 1119, 1952781411, '47-3981360', 'Encore Licensed Behavior Analyst, PLLC'),
    (22.94, '11', '', 1115, 1831607456, '82-3396945', 'Comprehensive Behavior Supports'),
    (23.0, '11', '', 1118, 1952781411, '47-3981360', 'Encore Licensed Behavior Analyst, PLLC'),
    (23.0, '11', '', 1119, 1952781411, '47-3981360', 'Encore Licensed Behavior Analyst, PLLC'),
    (23.5, '11', '', 1118, 1952781411, '47-3981360', 'Encore Licensed Behavior Analyst, PLLC'),
    (23.5, '11', '', 1119, 1952781411, '47-3981360', 'Encore Licensed Behavior Analyst, PLLC'),
    (26.69, '11', '', 1114, 1831607456, '82-3396945', 'Comprehensive Behavior Supports'),
    (26.69, '11', '', 1115, 1831607456, '82-3396945', 'Comprehensive Behavior Supports'),
    (19.07, '12', '', 1120, 1124440557, '46-2142743', 'A and J Behavioral Health, Inc.'),
    (26.0, '02', 'GT', 1113, 1952781411, '47-3981360', 'Encore Licensed Behavior Analyst, PLLC'),
    (22.25, '11,12,02', 'GT', 1118, 1952781411, '47-3981360', 'Encore Licensed Behavior Analyst, PLLC'),
    (22.25, '11,12,02', 'GT', 1119, 1952781411, '47-3981360', 'Encore Licensed Behavior Analyst, PLLC'),
    (26.0, '11,99,12,02', 'GT', 1118, 1952781411, '47-3981360', 'Encore Licensed Behavior Analyst, PLLC'),
    (26.0, '11,99,12,02', 'GT', 1119, 1952781411, '47-3981360', 'Encore Licensed Behavior Analyst, PLLC'),
]


def add_code_97151(conn: sqlite3.Connection) -> None:
    """Every real 97151 rate: a code with a no-modifier group next to modifier groups."""
    conn.executemany(INSERT_RATE, [
        (2, *BEHAVIOR_ID, "negotiated", rate, "professional", "outpatient", pos, modifiers, "9999-12-31",
         ref, npi, ein, name)
        for rate, pos, modifiers, ref, npi, ein, name in BEHAVIOR_ID_ROWS
    ])
    conn.commit()


# The real DB numbers its files fidelis-ex 1, fidelis-es 2; build_db numbers them the other way round.
FIXTURE_FILE = {1: 2, 2: 1}

INSERT_RATE_WITH_INFO = (
    "INSERT INTO rates (source_file_id, billing_code, billing_code_type, billing_code_type_version, name,"
    " description, negotiated_type, negotiated_rate, billing_class, setting, service_codes, modifiers,"
    " expiration_date, provider_reference_id, npi, ein, business_name, additional_information)"
    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


def insert_real_rows(conn: sqlite3.Connection, rows: list[tuple]) -> None:
    """Rows as selected from the loaded DB (its file id first, additional_information last)."""
    conn.executemany(INSERT_RATE_WITH_INFO, [(FIXTURE_FILE[row[0]], *row[1:]) for row in rows])
    conn.commit()


# RC 260 (IV therapy) rates under the name "IV Therapy General", copied from the loaded DB: every one that
# names its procedure in additional_information (96360/96361/96365/96366), plus two that name none.
# Centene repeats each price under four more names of RC 260; those copies are left out.
IV_THERAPY_ROWS = [
    (2, '260', 'RC', '2022', 'IV Therapy General', 'IV Therapy General', 'negotiated', 564.28, 'institutional', 'outpatient', '22,23', '', '9999-12-31', 89, 1093777492, '11-1635081', 'Maimonides Medical Center', '96360'),
    (2, '260', 'RC', '2022', 'IV Therapy General', 'IV Therapy General', 'negotiated', 130.06, 'institutional', 'outpatient', '22,23', '', '9999-12-31', 102, 1497701106, '14-1348692', "St Peter's Hospital", '96361'),
    (2, '260', 'RC', '2022', 'IV Therapy General', 'IV Therapy General', 'negotiated', 192.44, 'institutional', 'outpatient', '23', '', '9999-12-31', 137, 1518998699, '16-1165049', 'United Health Services Hospitals, Inc.', '96361'),
    (2, '260', 'RC', '2022', 'IV Therapy General', 'IV Therapy General', 'negotiated', 215.43, 'institutional', 'outpatient', '23', '', '9999-12-31', 144, 1043267727, '14-1338544', 'Samaritan Hospital', '96361'),
    (2, '260', 'RC', '2022', 'IV Therapy General', 'IV Therapy General', 'negotiated', 223.53, 'institutional', 'outpatient', '23', '', '9999-12-31', 118, 1811977796, '14-1347719', "St Mary's Healthcare", '96361'),
    (2, '260', 'RC', '2022', 'IV Therapy General', 'IV Therapy General', 'negotiated', 246.52, 'institutional', 'outpatient', '22,23', '', '9999-12-31', 120, 1487644993, '14-1338428', 'Ellis Hospital', '96361'),
    (2, '260', 'RC', '2022', 'IV Therapy General', 'IV Therapy General', 'negotiated', 355.92, 'institutional', 'outpatient', '23', '', '9999-12-31', 144, 1043267727, '14-1338544', 'Samaritan Hospital', '96365'),
    (2, '260', 'RC', '2022', 'IV Therapy General', 'IV Therapy General', 'negotiated', 549.82, 'institutional', 'outpatient', '23', '', '9999-12-31', 109, 1427616002, '13-3971298', 'NYU Langone Hospital Long Island', '96365'),
    (2, '260', 'RC', '2022', 'IV Therapy General', 'IV Therapy General', 'negotiated', 78.94, 'institutional', 'outpatient', '22,23', '', '9999-12-31', 102, 1497701106, '14-1348692', "St Peter's Hospital", '96366'),
    (2, '260', 'RC', '2022', 'IV Therapy General', 'IV Therapy General', 'negotiated', 111.18, 'institutional', 'outpatient', '23', '', '9999-12-31', 144, 1043267727, '14-1338544', 'Samaritan Hospital', '96366'),
    (1, '260', 'RC', '2022', 'IV Therapy General', 'IV Therapy General', 'negotiated', 7.79, 'institutional', 'outpatient', '23', '', '9999-12-31', 47, 1346241973, '16-0393490', 'Corning Hospital', None),
    (2, '260', 'RC', '2022', 'IV Therapy General', 'IV Therapy General', 'negotiated', 7.96, 'institutional', 'outpatient', '22', '', '9999-12-31', 140, 1639179328, '36-4983111', 'University Hospital of Brooklyn SUNY Downstate', None),
]
# One real CPT 96365 rate, so the procedure has a name of its own in the data (96360/96361/96366 don't here).
IV_INFUSION_CPT_ROWS = [
    (1, '96365', 'CPT', '2022', 'THER/PROPH/DIAG IV INF INIT', 'INTRAVENOUS INFUSION, FOR THERAPY, PROPHYLAXIS, OR DIAGNOSIS (SPECIFY SUBSTANCE OR DRUG); INITIAL, U', 'negotiated', 54.29, 'professional', 'outpatient', '11', 'XE', '9999-12-31', 1074, 1245442490, '33-3404574', 'Lifeline Tests Incorporated', None),
]


def add_code_260(conn: sqlite3.Connection) -> None:
    """Real RC 260 rates whose procedure is in additional_information, and a CPT 96365 rate that names it."""
    insert_real_rows(conn, IV_THERAPY_ROWS + IV_INFUSION_CPT_ROWS)


# Hospital (facility) rates for 99213, filed under a revenue code with 99213 in additional_information, copied
# from the loaded DB (all fidelis-es): RC 510 (clinic, under its "Clinic General" name) and RC 521 (whose names
# here are all procedure-style).
CLINIC_99213_ROWS = [
    (2, '510', 'RC', '2022', 'Clinic General', 'Clinic General', 'negotiated', 113.25, 'institutional', 'outpatient', '50', '', '9999-12-31', 309, 1558356741, '13-4019319', 'Med Alliance Medical Health Services', '99213'),
    (2, '510', 'RC', '2022', 'Clinic General', 'Clinic General', 'negotiated', 118.2, 'institutional', 'outpatient', '50', '', '9999-12-31', 446, 1730441791, '45-4931278', 'E & A Medical Solutions, LLC', '99213'),
    (2, '510', 'RC', '2022', 'Clinic General', 'Clinic General', 'negotiated', 118.2, 'institutional', 'outpatient', '50', '', '9999-12-31', 457, 1730441791, '45-4931278', 'E & A Medical Solutions, LLC', '99213'),
    (2, '510', 'RC', '2022', 'Clinic General', 'Clinic General', 'negotiated', 264.81, 'institutional', 'outpatient', '22', '', '9999-12-31', 144, 1043267727, '14-1338544', 'Samaritan Hospital', '99213'),
    (2, '510', 'RC', '2022', 'Clinic General', 'Clinic General', 'negotiated', 269.06, 'institutional', 'outpatient', '22', '', '9999-12-31', 120, 1487644993, '14-1338428', 'Ellis Hospital', '99213'),
    (2, '521', 'RC', '2022', 'OFFICE/OUTPATIENT VISIT EST', 'OFFICE OR OTH OUTPATIENT VISIT FOR THE EVALUATION AND MANAGEMENT OF AN ESTABLISHED PATIENT, WHICH RE', 'negotiated', 77.71, 'institutional', 'outpatient', '50', '', '9999-12-31', 513, 1023389533, '45-3448794', 'Koinonia Primary Care Inc', '99213'),
    (2, '521', 'RC', '2022', 'OFFICE/OUTPATIENT VISIT EST', 'OFFICE OR OTHER OUTPATIENT VISIT FOR THE EVALUATION AND MANAGEMENT OF AN ESTABLISHED PATIENT, THAT M', 'negotiated', 77.71, 'institutional', 'outpatient', '50', '', '9999-12-31', 513, 1023389533, '45-3448794', 'Koinonia Primary Care Inc', '99213'),
    (2, '521', 'RC', '2022', 'OFFICE/OUTPATIENT VISIT EST', 'OFFICE OR OTH OUTPATIENT VISIT FOR THE EVALUATION AND MANAGEMENT OF AN ESTABLISHED PATIENT, WHICH RE', 'negotiated', 104.19, 'institutional', 'outpatient', '50', '', '9999-12-31', 179, 1508850280, '23-7036393', 'Northern Oswego County Health Services Inc', '99213'),
    (2, '521', 'RC', '2022', 'OFFICE/OUTPATIENT VISIT EST', 'OFFICE OR OTHER OUTPATIENT VISIT FOR THE EVALUATION AND MANAGEMENT OF AN ESTABLISHED PATIENT, THAT M', 'negotiated', 104.19, 'institutional', 'outpatient', '50', '', '9999-12-31', 179, 1508850280, '23-7036393', 'Northern Oswego County Health Services Inc', '99213'),
]


def add_clinic_99213(conn: sqlite3.Connection) -> None:
    """Real hospital (facility) rates that bill 99213 under revenue codes 510 and 521."""
    insert_real_rows(conn, CLINIC_99213_ROWS)


# EIN 11-3322995 (a network with 53 names), copied from the loaded DB: NPI 1003050162 (Hands of Hope PT, PC; also
# listed as Abdul Malik MD, PC) has physical therapy rates but none for 99213, while the same EIN has 99213 rates
# under another name (New York Network Management IPA, Inc) and NPI.
NETWORK_ROWS = [
    (1, '97110', 'CPT', '2022', 'THERAPEUTIC EXERCISES', 'THERAPEUTIC PROCEDURE, 1 OR MORE AREAS, EACH 15 MINUTES; THERAPEUTIC EXERCISES TO DEVELOP STRENGTH A', 'negotiated', 20.39, 'professional', 'outpatient', '11', '59', '9999-12-31', 116, 1003050162, '11-3322995', 'Hands of Hope PT, PC', None),
    (1, '97124', 'CPT', '2022', 'MASSAGE THERAPY', 'THERAPEUTIC PROCEDURE, 1 OR MORE AREAS, EACH 15 MINUTES; MASSAGE, INCLUDING EFFLEURAGE, PETRISSAGE A', 'negotiated', 21.15, 'professional', 'outpatient', '11', '59', '9999-12-31', 116, 1003050162, '11-3322995', 'Hands of Hope PT, PC', None),
    (2, '97124', 'CPT', '2022', 'MASSAGE THERAPY', 'THERAPEUTIC PROCEDURE, 1 OR MORE AREAS, EACH 15 MINUTES; MASSAGE, INCLUDING EFFLEURAGE, PETRISSAGE A', 'negotiated', 21.15, 'professional', 'outpatient', '11', '59,GP', '9999-12-31', 155, 1003050162, '11-3322995', 'Abdul Malik MD, PC', None),
    (1, '99213', 'CPT', '2022', 'OFFICE/OUTPATIENT VISIT EST', 'OFFICE OR OTH OUTPATIENT VISIT FOR THE EVALUATION AND MANAGEMENT OF AN ESTABLISHED PATIENT, WHICH RE', 'negotiated', 53.21, 'professional', 'outpatient', '02', '95', '9999-12-31', 1238, 1609698950, '11-3322995', 'New York Network Management IPA, Inc', None),
    (1, '99213', 'CPT', '2022', 'OFFICE/OUTPATIENT VISIT EST', 'OFFICE OR OTH OUTPATIENT VISIT FOR THE EVALUATION AND MANAGEMENT OF AN ESTABLISHED PATIENT, WHICH RE', 'negotiated', 122.7, 'professional', 'outpatient', '20', '25', '9999-12-31', 722, 1073629069, '11-3322995', 'New York Network Management IPA, Inc', None),
]


def add_network_11_3322995(conn: sqlite3.Connection) -> None:
    """Real rows of EIN 11-3322995: an NPI without 99213 whose EIN has 99213 rates under another name."""
    insert_real_rows(conn, NETWORK_ROWS)
