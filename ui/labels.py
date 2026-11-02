"""Plain-English labels for billing modifiers and places of service, for tooltips and chips.

Modifiers: the most common ones in the loaded Fidelis files, grouped into the categories of
docs/modifiers.md (what the modifier does to the price). CPT (two-digit) modifier text is AMA-copyrighted,
so every label here is a short description in our own words, not the official wording. Unknown codes show
as the bare code.

Places of service: CMS place-of-service codes that appear in the data (rates.service_codes), with a short
label for chips and the CMS name for tooltips.
"""

CATEGORIES = {
    "component": "a fraction of the whole service",
    "delivery_mode": "how it was delivered",
    "provider_type": "the kind of clinician",
    "therapy_plan": "which therapy plan of care",
    "bundling": "paid separately from other services that visit",
    "anatomy": "which side or body part",
    "work_amount": "more or less work than usual",
    "surgical_role": "who did what in surgery",
    "repeat_or_lab": "repeated or outside service",
    "teaching": "teaching-physician supervision",
    "other": "equipment, drugs or coverage status",
}

# code -> (short label in our own words, category)
MODIFIERS = {
    # component
    "26": ("professional component (reading the result)", "component"),
    "TC": ("technical component (equipment and staff)", "component"),
    # delivery_mode
    "95": ("video telehealth", "delivery_mode"),
    "GT": ("telehealth, audio and video (older code)", "delivery_mode"),
    "GQ": ("asynchronous telehealth", "delivery_mode"),
    "93": ("audio-only telehealth", "delivery_mode"),
    # provider_type
    "AJ": ("clinical social worker", "provider_type"),
    "AH": ("clinical psychologist", "provider_type"),
    "SA": ("nurse practitioner working with a physician", "provider_type"),
    "AF": ("specialty physician", "provider_type"),
    "AI": ("principal physician of record", "provider_type"),
    "HO": ("master's-level clinician", "provider_type"),
    "HP": ("doctoral-level clinician", "provider_type"),
    "U5": ("payer-defined provider level", "provider_type"),
    # therapy_plan
    "GP": ("physical therapy plan of care", "therapy_plan"),
    "GO": ("occupational therapy plan of care", "therapy_plan"),
    "GN": ("speech-language therapy plan of care", "therapy_plan"),
    "CQ": ("physical therapy, in part by a PT assistant", "therapy_plan"),
    "CO": ("occupational therapy, in part by an OT assistant", "therapy_plan"),
    # bundling
    "59": ("distinct service, paid separately", "bundling"),
    "XE": ("separate encounter", "bundling"),
    "XS": ("separate structure or organ", "bundling"),
    "XU": ("unusual non-overlapping service", "bundling"),
    "XP": ("separate practitioner", "bundling"),
    "25": ("separate E/M visit the same day", "bundling"),
    # anatomy
    "RT": ("right side", "anatomy"),
    "LT": ("left side", "anatomy"),
    "50": ("both sides", "anatomy"),
    "E1": ("upper left eyelid", "anatomy"),
    "E2": ("lower left eyelid", "anatomy"),
    "E3": ("upper right eyelid", "anatomy"),
    "E4": ("lower right eyelid", "anatomy"),
    "F7": ("right foot, third toe", "anatomy"),
    "F8": ("right foot, fourth toe", "anatomy"),
    # work_amount
    "22": ("more work than usual", "work_amount"),
    "52": ("less work than usual", "work_amount"),
    "51": ("multiple procedures", "work_amount"),
    "53": ("stopped before completion", "work_amount"),
    # surgical_role
    "AS": ("assistant at surgery (non-physician)", "surgical_role"),
    "80": ("assistant surgeon", "surgical_role"),
    "54": ("surgery only, no follow-up care", "surgical_role"),
    "55": ("follow-up care only", "surgical_role"),
    "57": ("visit that led to the decision for surgery", "surgical_role"),
    "58": ("staged or related procedure", "surgical_role"),
    "79": ("unrelated procedure in the follow-up period", "surgical_role"),
    "24": ("unrelated visit in the follow-up period", "surgical_role"),
    "AA": ("anesthesia by the anesthesiologist personally", "surgical_role"),
    "QK": ("anesthesiologist directing 2-4 concurrent cases", "surgical_role"),
    "QX": ("nurse anesthetist directed by a physician", "surgical_role"),
    "QY": ("anesthesiologist directing one nurse anesthetist", "surgical_role"),
    "QZ": ("nurse anesthetist without physician direction", "surgical_role"),
    # repeat_or_lab
    "76": ("repeat by the same clinician", "repeat_or_lab"),
    "77": ("repeat by another clinician", "repeat_or_lab"),
    "90": ("sent to an outside lab", "repeat_or_lab"),
    "91": ("repeat lab test", "repeat_or_lab"),
    "QW": ("CLIA-waived lab test", "repeat_or_lab"),
    # teaching
    "GC": ("done in part by a resident, teaching physician supervising", "teaching"),
    "GE": ("done by a resident without a teaching physician present", "teaching"),
    # other
    "NU": ("new equipment", "other"),
    "RR": ("rented equipment", "other"),
    "JW": ("drug amount discarded", "other"),
    "JZ": ("no drug amount discarded", "other"),
    "KX": ("coverage requirements met", "other"),
    "GA": ("waiver of liability on file", "other"),
    "GY": ("not covered by statute", "other"),
    "Q1": ("routine care in a clinical trial", "other"),
    "33": ("preventive service", "other"),
    "SU": ("done in the clinician's office (facility costs)", "other"),
    "TH": ("prenatal or postpartum care", "other"),
    "A1": ("dressing for one wound", "other"),
    "Q6": ("substitute physician, paid by time", "other"),
    "PT": ("screening turned diagnostic", "other"),
}

# CMS place-of-service code -> (short label, CMS name)
PLACES = {
    "02": ("telehealth", "Telehealth provided other than in patient's home"),
    "03": ("school", "School"),
    "04": ("homeless shelter", "Homeless shelter"),
    "10": ("telehealth at home", "Telehealth provided in patient's home"),
    "11": ("office", "Office"),
    "12": ("home", "Home"),
    "15": ("mobile unit", "Mobile unit"),
    "17": ("retail clinic", "Walk-in retail health clinic"),
    "19": ("off-campus hospital outpatient", "Off campus-outpatient hospital"),
    "20": ("urgent care", "Urgent care facility"),
    "21": ("inpatient hospital", "Inpatient hospital"),
    "22": ("on-campus hospital outpatient", "On campus-outpatient hospital"),
    "23": ("emergency room", "Emergency room - hospital"),
    "24": ("surgery center", "Ambulatory surgical center"),
    "25": ("birthing center", "Birthing center"),
    "31": ("skilled nursing facility", "Skilled nursing facility"),
    "32": ("nursing facility", "Nursing facility"),
    "41": ("ambulance", "Ambulance - land"),
    "49": ("independent clinic", "Independent clinic"),
    "50": ("FQHC", "Federally qualified health center"),
    "53": ("community mental health center", "Community mental health center"),
    "58": ("opioid treatment facility", "Non-residential opioid treatment facility"),
    "62": ("outpatient rehab facility", "Comprehensive outpatient rehabilitation facility"),
    "65": ("dialysis facility", "End-stage renal disease treatment facility"),
    "71": ("public health clinic", "Public health clinic"),
    "72": ("rural health clinic", "Rural health clinic"),
    "81": ("independent lab", "Independent laboratory"),
    "99": ("other", "Other place of service"),
}


def split_codes(joined: str | None) -> list[str]:
    """ "79,51" -> ["79", "51"]; empty or None -> []."""
    return [c.strip() for c in (joined or "").split(",") if c.strip()]


def describe_modifiers(joined: str | None) -> list[tuple[str, str | None, str | None]]:
    """[(code, label, category)] for each modifier in a stored combination; unknown codes get None, None."""
    return [(code, *MODIFIERS.get(code.upper(), (None, None))) for code in split_codes(joined)]


def modifier_tooltip(joined: str | None) -> str:
    """One line per modifier: "26 · professional component (reading the result) — component: a fraction of
    the whole service"; an unknown code is just the code."""
    lines = []
    for code, label, category in describe_modifiers(joined):
        lines.append(f"{code} · {label} — {category.replace('_', ' ')}: {CATEGORIES[category]}" if label else code)
    return "\n".join(lines) or "no modifier"


def modifier_label(joined: str | None) -> str:
    """How a combination reads on a chip: "mod 26", "mods 79, 51", or "no modifier". The "mod" keeps a modifier
    apart from a place-of-service code (modifier 50 is bilateral; place 50 is an FQHC)."""
    codes = split_codes(joined)
    return f"{'mods' if len(codes) > 1 else 'mod'} {', '.join(codes)}" if codes else "no modifier"


def describe_places(joined: str | None) -> list[tuple[str, str | None]]:
    """[(code, CMS name)] for each place of service; unknown codes get None."""
    return [(code, PLACES[code][1] if code in PLACES else None) for code in split_codes(joined)]


def place_tooltip(joined: str | None) -> str:
    return "\n".join(f"{code} · {name}" if name else code for code, name in describe_places(joined)) \
        or "no place of service"


def place_label(joined: str | None) -> str:
    """Short label: "office (11)", "telehealth (02), telehealth at home (10)"; unknown codes are bare."""
    parts = [f"{PLACES[c][0]} ({c})" if c in PLACES else c for c in split_codes(joined)]
    return ", ".join(parts) or "no place of service"
