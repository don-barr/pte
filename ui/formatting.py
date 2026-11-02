"""Formatting for the pages (UI layer): money, counts, the links between pages, and the no-dead-end notices.

The API (api.pickers) answers with facts: who was asked about, which rates they have instead, which other codes. This
module turns those facts into what a page shows: a notice is {"kind": "none" | "hospital_only" | "unknown",
"message", "alternatives": [{"label", "href"}]}, and the hrefs keep the page's scope (query pairs such as
("in", "file-2"), ("include_facility", "false")).
"""

from decimal import ROUND_HALF_UP, Decimal
from urllib.parse import quote, urlencode

from api.pickers import sentence_case
from ui.labels import modifier_label, modifier_tooltip, place_label, place_tooltip

PROCEDURE_PATH = "/by-procedure"
PROVIDER_PATH = "/by-provider"


def format_money(value: float) -> str:
    """$1,234.57, rounding half-cents up as written (a median of 69.85 and 76.84 is $73.35, not the float's $73.34)."""
    cents = Decimal(repr(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return f"${cents:,.2f}"


def money_range(low: float, high: float) -> str:
    return format_money(low) if low == high else f"{format_money(low)}–{format_money(high)}"


def plural(n: int, word: str, words: str | None = None) -> str:
    return f"{n:,} {word if n == 1 else (words or word + 's')}"


def label_groups(codes: list[dict]) -> list[dict]:
    """api.providers.code_breakdown's codes with each group's modifier and place of service in words: modifier_label
    ("mod 25"), modifier_tooltip, place_label ("office (11)"), place_tooltip. Returns codes."""
    for c in codes:
        for g in c.get("groups", []):
            g.update(modifier_label=modifier_label(g["modifiers"]), modifier_tooltip=modifier_tooltip(g["modifiers"]),
                     place_label=place_label(g["service_codes"]), place_tooltip=place_tooltip(g["service_codes"]))
    return codes


# ---------------------------------------------------------------- links

def procedure_href(code: str, scope=(), **params) -> str:
    """The procedure page for code, keeping the scope pairs, plus params (ein=, npi=, code_type=...)."""
    pairs = list(scope) + [(k, v) for k, v in params.items() if v not in (None, "")]
    return f"{PROCEDURE_PATH}/{quote(code, safe='')}" + (f"?{urlencode(pairs)}" if pairs else "")


def provider_href(ein: str, scope=()) -> str:
    pairs = [p for p in scope if p[0] == "in"]
    return f"{PROVIDER_PATH}/{ein}" + (f"?{urlencode(pairs)}" if pairs else "")


def without_files(scope) -> list:
    """The scope pairs without the plan or file choice: all plans."""
    return [p for p in scope if p[0] != "in"]


def with_hospital(scope) -> list:
    """The scope pairs with hospital rates shown (the default)."""
    return [p for p in scope if p[0] != "include_facility"]


# ---------------------------------------------------------------- the pickers' answers

def providers_picker(found: dict, scope=()) -> dict:
    """api.pickers.providers_for_code's answer for a page: each provider linked to the code at that provider (its
    NPI when one was typed), and the notice in words."""
    code = found["code"]
    providers = [{**p, "href": procedure_href(code, scope, **({"npi": p["npi"]} if "npi" in p else {"ein": p["ein"]}))}
                 for p in found["providers"]]
    return {**found, "providers": providers, "notice": notice(found["notice"], scope)}


def code_href(row: dict, ein: str, scope=()) -> str:
    """A code at this EIN, naming its type when its number is two codes (731)."""
    return procedure_href(row["billing_code"], scope, ein=ein,
                          code_type=row["billing_code_type"] if row.get("multi_type") else None)


def codes_picker(found: dict, scope=()) -> dict:
    """api.pickers.codes_for_provider's answer for a page: each code linked to that code at this provider, and the
    notice in words."""
    ein = found["ein"]
    codes = [{**{k: v for k, v in c.items() if k != "multi_type"}, "href": code_href(c, ein, scope)}
             for c in found["codes"]]
    return {**found, "codes": codes, "notice": notice(found["notice"], scope)}


# ---------------------------------------------------------------- notices

UNKNOWN = {
    "EIN": "No provider has EIN {} in the loaded files.",
    "NPI": "No provider has NPI {} in the loaded files.",
    "name": "No provider name matches “{}”.",
    "code": "No code or procedure name matches “{}”.",
}


def who(facts: dict) -> str:
    """The EIN or NPI a notice is about, with its name: "Forme Medical Center (EIN 13-4068415)", "NPI 1003050162
    (Hands of Hope PT, PC, EIN 11-3322995)"."""
    w = facts["who"]
    if "npi" in w:
        return f"NPI {w['npi']} ({w['name']}{', EIN ' + w['ein'] if w.get('ein') else ''})"
    return f"{w['name']} (EIN {w['ein']})" if w["name"] else f"EIN {w['ein']}"


def page_link(facts: dict, scope) -> dict | None:
    """The provider page an EIN or NPI notice offers: the EIN's, or the NPI's EIN's."""
    ein = facts.get("page_ein")
    if not ein:
        return None
    label = "See all its codes" if facts["by"] == "ein" else f"All codes for its EIN {ein}"
    return {"label": label, "href": provider_href(ein, scope)}


def other_names_link(code: str, ein: str, n: int, scope) -> dict:
    return {"label": f"Its EIN {ein} has {plural(n, 'rate')} for {code} under other names",
            "href": procedure_href(code, scope, ein=ein)}


def notice(facts: dict | None, scope=()) -> dict | None:
    """A picker's notice in words, from api.pickers' facts (None when there is nothing to say)."""
    if facts is None:
        return None
    if facts["kind"] == "unknown":
        return {"kind": "unknown", "message": UNKNOWN[facts["what"]].format(facts["value"]), "alternatives": []}
    if "near" in facts:
        out = code_notice(facts, scope)
    elif "under_other_names_only" in facts:
        ein, n = facts["under_other_names_only"]
        code = facts["code"]
        out = {"kind": "none", "message": "", "alternatives": [
            other_names_link(code, ein, n, scope), {"label": f"Keep {code}, any provider", "href": procedure_href(code, scope)}]}
    elif facts["kind"] == "hospital_only":
        out = hospital_notice(facts, scope)
    else:
        out = none_notice(facts, scope)
    named = facts.get("named")
    if named:
        out["message"] = f"No provider named “{named['q']}” has rates for {facts['code']}. " + out["message"]
        others = named["other_eins"]
        if others:
            out["message"] += f" {plural(others, 'other EIN')} carr{'ies' if others == 1 else 'y'} that name."
    return out


def hospital_notice(facts: dict, scope) -> dict:
    code, hospital = facts["code"], facts["hospital"]
    n = sum(h[1] for h in hospital)
    revenue = ", ".join(f"{h[0]:0>4}" for h in hospital)
    span = money_range(min(h[2] for h in hospital), max(h[3] for h in hospital))
    # Shown as an option too when hospital rates are on; the link turns them on when they're off.
    show = {"label": f"Show its {plural(n, 'hospital rate')} for {code}",
            "href": procedure_href(code, with_hospital(scope), **{facts["by"]: str(facts["value"])})}
    page = page_link(facts, scope)
    return {"kind": "hospital_only",
            "message": f"{who(facts)} has no {code} rates of its own. It has {plural(n, 'hospital rate')} for {code},"
                       f" billed under revenue code{'s' if len(hospital) > 1 else ''} {revenue} ({span}).",
            "alternatives": [show, *([page] if page else [])]}


def none_notice(facts: dict, scope) -> dict:
    """The EIN or NPI has no rates (nor hospital rates) for the code here: its EIN's rates under other names, its
    rates in other files, its own codes, its page, and the code from any provider."""
    code, by, value = facts["code"], facts["by"], str(facts["value"])
    alternatives = [other_names_link(code, ein, n, scope) for ein, n in facts["under_other_names"]]
    if facts["elsewhere"]:
        alternatives.append({"label": f"It has {plural(facts['elsewhere'], 'rate')} for {code} in other files:"
                                      " search all plans",
                             "href": procedure_href(code, without_files(scope), **{by: value})})
    alternatives += [{"label": f"{other} {sentence_case(name or '')}".strip(),
                      "href": procedure_href(other, scope, **{by: value})} for other, name in facts["other_codes"]]
    page = page_link(facts, scope)
    alternatives += [*([page] if page else []), {"label": f"Keep {code}, any provider", "href": procedure_href(code, scope)}]
    count = facts["other_code_count"]
    return {"kind": "none", "alternatives": alternatives,
            "message": f"{who(facts)} has no rates for {code}{' here' if facts['here'] else ''}."
                       + (f" It has rates for {plural(count, 'other code')}." if count else "")}


def code_notice(facts: dict, scope) -> dict:
    """An EIN has no rates for the code typed: the code in other files, its nearest codes, the code from anyone."""
    ein, code = facts["ein"], facts["code"]
    alternatives = []
    if facts["elsewhere"]:
        alternatives.append({"label": f"It has rates for {code} in other files: search all plans",
                             "href": procedure_href(code, without_files(scope), ein=ein)})
    alternatives += [{"label": f"{r['billing_code']} {r['name']}".strip(), "href": code_href(r, ein, scope)}
                     for r in facts["near"]]
    alternatives.append({"label": f"{code} from any provider ({plural(facts['ein_count'], 'EIN')})",
                         "href": procedure_href(code, scope)})
    lead = "the nearest by number" if facts["near_by"] == "number" else "the most rates"
    count = facts["other_code_count"]
    return {"kind": "none", "alternatives": alternatives,
            "message": f"EIN {ein} has no rates for {code} ({facts['code_name']})."
                       + (f" It has rates for {plural(count, 'other code')}; {lead}:" if count else "")}
