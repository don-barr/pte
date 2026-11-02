"""Read-only queries over the rates DB written by the ETL."""

import math
import sqlite3
import statistics
from collections import Counter
from pathlib import PurePosixPath
from urllib.parse import unquote, urlparse

# How a rate was billed: its own code ("CPT 99213"), or for a hospital (facility) rate the revenue code, written
# with four digits as revenue codes usually are ("RC 0510").
BILLED_AS = ("CASE WHEN r.billing_code_type = 'RC' THEN 'RC ' || substr('0000' || r.billing_code, -4)"
             " ELSE r.billing_code_type || ' ' || r.billing_code END")

RESULT_COLUMNS = f"""
    r.billing_code, r.billing_code_type, r.name, r.description, {BILLED_AS} AS billed_as,
    r.additional_information AS procedure,
    r.negotiated_type, r.negotiated_rate, r.billing_class, r.setting,
    r.service_codes, r.modifiers, r.expiration_date,
    r.npi, r.ein, r.business_name,
    s.url AS source_url, s.last_updated_on AS source_last_updated_on
"""


# URL sort key -> SQL expression. Every column shown in the results table is sortable.
RATE_SORTS = {
    "rate": "r.negotiated_rate",
    "provider": "r.business_name",
    "ein": "r.ein",
    "npi": "r.npi",
    "billing_class": "r.billing_class",
    "setting": "r.setting",
    "service_codes": "r.service_codes",
    "modifiers": "r.modifiers",
    "procedure": "r.additional_information",
    "billed_as": BILLED_AS,
    "source": "s.url",
}
# Column filters: text columns compared with an operator (<column>_op), and a rate range.
TEXT_FILTERS = {
    "provider": "r.business_name",
    "ein": "REPLACE(r.ein, '-', '')",  # matched without dashes, so 113471596 finds 11-3471596
    "npi": "CAST(r.npi AS TEXT)",
    "billing_class": "r.billing_class",
    "setting": "r.setting",
    "service_codes": "r.service_codes",
    "modifiers": "r.modifiers",
    "procedure": "r.additional_information",
    "billed_as": BILLED_AS,
}
# Operator -> (LIKE pattern around the escaped value, negate). LIKE ignores case, so equals does too.
# empty / not_empty take no value: they test whether the column has one (NULL and "" both count as empty).
OPERATORS = {
    "contains": ("%{}%", False),
    "not_contains": ("%{}%", True),
    "equals": ("{}", False),
    "not_equals": ("{}", True),
    "empty": (None, False),
    "not_empty": (None, True),
}
VALUELESS_OPERATORS = ("empty", "not_empty")
DEFAULT_OPERATOR = "contains"
OPERATOR_PARAMS = {f"{name}_op": name for name in TEXT_FILTERS}
RANGE_FILTERS = {"rate_min": "r.negotiated_rate >= ?", "rate_max": "r.negotiated_rate <= ?"}
FILTERS = (*TEXT_FILTERS, *RANGE_FILTERS)

CODE_SORTS = {"code": "billing_code", "type": "billing_code_type", "name": "name", "rates": "rate_count"}
DIRECTIONS = ("asc", "desc")


def check_sort(sort: str, direction: str, allowed) -> None:
    if sort not in allowed:
        raise ValueError(f"can't sort by {sort!r}; choose one of {', '.join(allowed)}")
    if direction not in DIRECTIONS:
        raise ValueError(f"sort direction must be asc or desc, not {direction!r}")


def check_operator(name: str, op) -> str:
    """The operator for a text filter; blank means contains. Raises ValueError for anything else."""
    if op is None or not str(op).strip():
        return DEFAULT_OPERATOR
    op = str(op).strip()
    if op not in OPERATORS:
        raise ValueError(f"{name}_op must be one of {', '.join(OPERATORS)}, not {op!r}")
    return op


def filter_clauses(filters: dict) -> tuple[list[str], list, dict]:
    """SQL conditions and arguments for the non-blank filters, plus the filters actually applied.
    A text filter's operator comes from "<name>_op" (default contains)."""
    filters = filters or {}
    clauses, args, applied = [], [], {}
    for name in filters:
        if name not in FILTERS and name not in OPERATOR_PARAMS:
            raise ValueError(f"can't filter by {name!r}; choose from {', '.join(FILTERS)}"
                             f" (text filters also take <name>_op)")
    ops = {name: check_operator(name, filters.get(f"{name}_op")) for name in TEXT_FILTERS}
    for name, op in ops.items():
        if op in VALUELESS_OPERATORS:  # the value, if any, is ignored
            clauses.append(f"COALESCE({TEXT_FILTERS[name]}, '') {'!=' if op == 'not_empty' else '='} ''")
            applied[f"{name}_op"] = op
    for name, value in filters.items():
        if name in OPERATOR_PARAMS or value is None or (isinstance(value, str) and not value.strip()):
            continue
        if name in RANGE_FILTERS:
            clauses.append(RANGE_FILTERS[name])
            args.append(float(value))
            applied[name] = value
            continue
        op = ops[name]
        if op in VALUELESS_OPERATORS:
            continue
        value = value.strip()
        pattern, negate = OPERATORS[op]
        needle = value.replace("-", "") if name == "ein" else value
        escaped = needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        # A missing value counts as "", so "does not contain x" keeps rows with no value.
        clauses.append(f"COALESCE({TEXT_FILTERS[name]}, '') {'NOT ' if negate else ''}LIKE ? ESCAPE '\\'")
        args.append(pattern.format(escaped))
        applied[name] = value
        if f"{name}_op" in filters:
            applied[f"{name}_op"] = op
    return clauses, args, applied


def normalize_code(billing_code: str) -> str:
    return (billing_code or "").strip().upper()


def file_name(url: str) -> str:
    return unquote(PurePosixPath(urlparse(url).path).name)


def short_name(name: str) -> str:
    """The part of a CMS file name that tells files apart: "<date>_<entity>_fidelis-ex_in-network.json" -> "fidelis-ex"."""
    if not name.endswith("_in-network.json"):
        return name
    return name.removesuffix("_in-network.json").rsplit("_", 1)[-1]


def file_sql(ids: list[int] | None, column: str = "source_file_id") -> tuple[str, tuple]:
    """(" AND column IN (?, ...)", ids) limiting a query to these files, or ("", ()) for every file (None)."""
    if ids is None:
        return "", ()
    return f" AND {column} IN ({', '.join('?' * len(ids))})", tuple(ids)


def search_rates(
    conn: sqlite3.Connection, billing_code: str, limit: int = 100, offset: int = 0,
    sort: str = "rate", direction: str = "asc", source_file_ids: list[int] | None = None,
    filters: dict | None = None, code_type: str | None = None, include_facility: bool = True,
) -> dict:
    """Rates for one billing code. source_file_ids limits the search to those files (None means every file);
    filters narrows by column (see FILTERS); include_facility adds hospital rates billed under a revenue code
    with this code as the procedure (see scope_where)."""
    check_sort(sort, direction, RATE_SORTS)
    code = normalize_code(billing_code)
    where, args = scope_where(code, source_file_ids, code_type, filters, include_facility)
    applied = filter_clauses(filters)[2]
    total, file_count = conn.execute(
        f"SELECT COUNT(*), COUNT(DISTINCT r.source_file_id) FROM rates r WHERE {where}", args).fetchone()
    cursor = conn.execute(
        f"SELECT {RESULT_COLUMNS} FROM rates r JOIN source_files s ON s.id = r.source_file_id"
        f" WHERE {where}"
        f" ORDER BY {RATE_SORTS[sort]} IS NULL, {RATE_SORTS[sort]} {direction},"
        " r.negotiated_rate, r.business_name, r.id LIMIT ? OFFSET ?",
        (*args, limit, offset),
    )
    names = [d[0] for d in cursor.description]
    results = []
    for row in cursor:
        item = dict(zip(names, row))
        item["source_file"] = file_name(item["source_url"])
        results.append(item)
    return {"billing_code": code, "total": total, "limit": limit, "offset": offset,
            "sort": sort, "direction": direction, "source_file_ids": list(source_file_ids or []),
            "filters": applied, "file_count": file_count, "code_type": code_type,
            "include_facility": include_facility, "results": results}


# A price is for one kind of service only within one (billed as, procedure, modifiers, billing class, place of
# service) group: docs/modifiers.md, "Comparison rules". Billed as keeps a hospital's rate for a CPT (under a
# revenue code) apart from the CPT's own rates; procedure tells apart what a revenue code's rates are for.
# These filters pick the group, so price_groups ignores them.
GROUP_COLUMNS = ("billed_as", "procedure", "modifiers", "billing_class", "service_codes")
# SQL for each group column, as grouped on ("" for none).
GROUP_SQL = {"billed_as": BILLED_AS, "procedure": "COALESCE(r.additional_information, '')",
             "modifiers": "COALESCE(r.modifiers, '')", "billing_class": "COALESCE(r.billing_class, '')",
             "service_codes": "COALESCE(r.service_codes, '')"}
GROUP_KEY_SQL = ", ".join(GROUP_SQL[c] for c in GROUP_COLUMNS)


def group_key(group: dict) -> tuple:
    return tuple(group[c] for c in GROUP_COLUMNS)


def scope_where(billing_code: str, source_file_ids: list[int] | None = None, code_type: str | None = None,
                filters: dict | None = None, include_facility: bool = True) -> tuple[str, list]:
    """WHERE clause (over rates r) and arguments for one code's rates in these files and of this type, filtered.
    Unless the code is a revenue code (RC), include_facility adds the hospital (facility) rates filed under a
    revenue code with this code as the procedure (additional_information): Centene bills a hospital's 99213
    as, say, RC 0510 (clinic) with 99213 alongside."""
    code = normalize_code(billing_code)
    where, args = "r.billing_code = ?", [code]
    if code_type:  # one number can be two codes (CPT 731 and RC 731)
        where += " AND r.billing_code_type = ?"
        args.append(code_type)
    if include_facility and code_type != "RC":
        where = f"(({where}) OR (r.billing_code_type = 'RC' AND r.additional_information = ?))"
        args.append(code)
    if source_file_ids:
        where += f" AND r.source_file_id IN ({', '.join('?' * len(source_file_ids))})"
        args += list(source_file_ids)
    clauses, filter_args, _ = filter_clauses(filters)
    for clause in clauses:
        where += f" AND {clause}"
    return where, args + filter_args


def without_group_filters(filters: dict | None) -> dict:
    return {k: v for k, v in (filters or {}).items()
            if k not in GROUP_COLUMNS and k not in {f"{c}_op" for c in GROUP_COLUMNS}}


def names_in_scope(conn: sqlite3.Connection, billing_code: str, source_file_ids: list[int] | None = None,
                   code_type: str | None = None, filters: dict | None = None) -> list[tuple]:
    """[(business_name, ein, rate count)] among the rates matching the search, most rates first: who a procedure
    page narrowed to an EIN or NPI is showing."""
    where, args = scope_where(billing_code, source_file_ids, code_type, filters)
    return conn.execute(f"SELECT r.business_name, r.ein, COUNT(*) FROM rates r WHERE {where} GROUP BY 1, 2"
                        " ORDER BY 3 DESC, 1", args).fetchall()


def price_groups(conn: sqlite3.Connection, billing_code: str, source_file_ids: list[int] | None = None,
                 code_type: str | None = None, filters: dict | None = None,
                 keep_group_filters: bool = False, include_facility: bool = True) -> list[dict]:
    """The (billed as, procedure, modifiers, billing class, place of service) combinations among the matching
    rates, most rates first: [{"billed_as", "procedure", "modifiers", "billing_class", "service_codes",
    "rate_count", "provider_count", "npi_count", "min_rate", "max_rate"}]. No modifier (or no procedure) is a group of its
    own (""). The filters on those columns are ignored, so every group can still be picked, unless
    keep_group_filters (then these are the groups the results actually span). provider_count counts distinct
    business names, npi_count distinct NPIs (clinicians)."""
    if not keep_group_filters:
        filters = without_group_filters(filters)
    where, args = scope_where(billing_code, source_file_ids, code_type, filters, include_facility)
    n = len(GROUP_COLUMNS)
    positions = ", ".join(str(i) for i in range(1, n + 1))
    rows = conn.execute(
        f"SELECT {GROUP_KEY_SQL}, COUNT(*), COUNT(DISTINCT r.business_name), COUNT(DISTINCT r.npi),"
        f" MIN(r.negotiated_rate), MAX(r.negotiated_rate) FROM rates r WHERE {where} GROUP BY {positions} ORDER BY {n + 1} DESC, {positions}",
        args)
    keys = (*GROUP_COLUMNS, "rate_count", "provider_count", "npi_count", "min_rate", "max_rate")
    return [dict(zip(keys, row)) for row in rows]


def quartiles(values: list[float]) -> tuple[float, float, float]:
    """Q1, median, Q3 of sorted values, by linear interpolation between order statistics: Python's
    statistics.quantiles(values, n=4, method="inclusive"), the same as Excel QUARTILE.INC and R type 7."""
    if len(values) == 1:
        return values[0], values[0], values[0]
    q1, median, q3 = statistics.quantiles(values, n=4, method="inclusive")
    return q1, median, q3


def percentile(values: list[float], p: float) -> float:
    """The p-th quantile (0 <= p <= 1) of sorted values by linear interpolation between order statistics at
    position (n - 1) x p: the "inclusive" method of statistics.quantiles, so it agrees with quartiles()
    (Excel PERCENTILE.INC, R type 7). It never goes outside [min, max]."""
    position = (len(values) - 1) * p
    below = math.floor(position)
    above = min(below + 1, len(values) - 1)
    return values[below] + (values[above] - values[below]) * (position - below)


# Percentiles reported by rate_stats: key -> fraction.
PERCENTILES = {"p1": .01, "p90": .90, "p95": .95, "p99": .99, "p99_9": .999, "p99_99": .9999}
# A tail percentile says little until enough rates lie beyond it: p99.9 needs 1,000 rates (one past it),
# p99.99 10,000. Below that it is None.
PERCENTILE_MIN_COUNTS = {"p99_9": 1000, "p99_99": 10000}


def percentiles(values: list[float], min_counts: dict | None = None) -> dict:
    """PERCENTILES of sorted values; a tail percentile with fewer values than its min count is None."""
    min_counts = PERCENTILE_MIN_COUNTS if min_counts is None else min_counts
    return {key: percentile(values, p) if len(values) >= min_counts.get(key, 0) else None
            for key, p in PERCENTILES.items()}


# Tukey's rule: a rate is an outlier when it lies more than 1.5 x IQR below Q1 or above Q3.
TUKEY_K = 1.5


def outliers(values: list[float], q1: float, q3: float) -> dict:
    """Tukey's fences around sorted values and how many lie beyond each: low_fence = Q1 - 1.5 x IQR,
    high_fence = Q3 + 1.5 x IQR, low_outliers / high_outliers counting rates strictly beyond them. The low
    fence can be negative (no rate can lie below it then)."""
    low_fence, high_fence = q1 - TUKEY_K * (q3 - q1), q3 + TUKEY_K * (q3 - q1)
    return {"low_fence": low_fence, "high_fence": high_fence,
            "low_outliers": sum(1 for v in values if v < low_fence),
            "high_outliers": sum(1 for v in values if v > high_fence)}


HISTOGRAM_BINS = 20
DENSITY_POINTS = 81


def histogram(values: list[float], low: float, high: float, bins: int = HISTOGRAM_BINS) -> list[dict]:
    """Equal-width bins over [low, high]; each bin holds low <= v < high, and the last one includes high.
    One bin when every value is the same."""
    if low == high:
        return [{"low": low, "high": high, "count": len(values)}]
    width = (high - low) / bins
    counts = [0] * bins
    for v in values:
        counts[min(int((v - low) / width), bins - 1)] += 1
    edges = [low + i * width for i in range(bins)] + [high]
    return [{"low": edges[i], "high": edges[i + 1], "count": counts[i]} for i in range(bins)]


def density(values: list[float], low: float, high: float, bin_width: float, iqr: float,
            points: int = DENSITY_POINTS) -> list[dict]:
    """A Gaussian kernel density estimate at evenly spaced points over [low, high], scaled to the histogram
    (expected rates per bin of bin_width) so the curve sits over the bars. Bandwidth: Silverman's rule of
    thumb, 0.9 x min(standard deviation, IQR / 1.34) x n^(-1/5) (the standard deviation when the IQR is 0),
    but never under 3/4 of a bin. Real rates are lumpy (a few prices repeated many times), so the histogram shows what
    is there and the curve only the overall shape. Sums over distinct rates (weighted), not every row."""
    n = len(values)
    if n < 2 or low == high:
        return []
    sd = statistics.stdev(values)
    spread = min(sd, iqr / 1.34) if iqr > 0 else sd
    # At least most of a bin wide: many rates sit on one exact price, and a narrower kernel draws spikes
    # far taller than the bars instead of a shape.
    h = max(0.9 * spread * n ** -0.2, 0.75 * bin_width)
    weights = Counter(values)
    norm = bin_width / (h * math.sqrt(2 * math.pi))  # n x density x bin width, n folded into the weights
    curve = []
    for i in range(points):
        x = low + (high - low) * i / (points - 1)
        y = norm * sum(w * math.exp(-0.5 * ((x - v) / h) ** 2) for v, w in weights.items())
        curve.append({"x": x, "y": y})
    curve[-1]["x"] = high  # exact end, not low + (high - low) * 1.0 with rounding
    return curve


def rate_stats(conn: sqlite3.Connection, billing_code: str, source_file_ids: list[int] | None = None,
               code_type: str | None = None, filters: dict | None = None,
               include_facility: bool = True) -> dict | None:
    """Statistics over every rate matching the search (all pages): count, provider_count (distinct business
    names), ein_count and npi_count (distinct, non-null), min, q1, median, q3, iqr, max (no mean: an average can be a price nobody negotiated), Tukey's
    outlier fences and counts (low_fence, high_fence, low_outliers, high_outliers; see outliers), distinct_rates, percentiles p1, p90, p95, p99, p99_9 and p99_99 (see percentiles; None for a tail too
    thin to estimate), a histogram ([{"low", "high", "count"}], see histogram) and a density curve
    ([{"x", "y"}], see density). None when nothing matches. Each row is one price for one provider group
    and NPI, so a provider group listing many NPIs counts once per NPI."""
    where, args = scope_where(billing_code, source_file_ids, code_type, filters, include_facility)
    count, providers, eins, npis = conn.execute(
        "SELECT COUNT(*), COUNT(DISTINCT r.business_name), COUNT(DISTINCT r.ein), COUNT(DISTINCT r.npi)"
        f" FROM rates r WHERE {where}", args).fetchone()
    if count == 0:
        return None
    values = [v for (v,) in conn.execute(f"SELECT r.negotiated_rate FROM rates r WHERE {where} ORDER BY 1", args)]
    low, high = values[0], values[-1]
    q1, median, q3 = quartiles(values)
    bins = histogram(values, low, high)
    return {
        "count": count, "provider_count": providers, "ein_count": eins, "npi_count": npis,
        "min": low, "q1": q1, "median": median, "q3": q3, "iqr": q3 - q1, "max": high,
        **iqr_spread(q1, median, q3),
        "distinct_rates": len(set(values)), **percentiles(values),
        **outliers(values, q1, q3),
        "histogram": bins, "density": density(values, low, high, bins[0]["high"] - bins[0]["low"], q3 - q1),
    }


def iqr_spread(q1: float, median: float, q3: float) -> dict:
    """The middle half's width as a share of the median, and how far each quartile sits from it: the
    middle half is often lopsided (99213 · 25 · office: 6.9% wide, 1.4% below and 5.5% above)."""
    if not median:
        return {"iqr_over_median": None, "q1_below_median": None, "q3_above_median": None}
    return {"iqr_over_median": (q3 - q1) / median, "q1_below_median": (median - q1) / median,
            "q3_above_median": (q3 - median) / median}


def stats_by_group(conn: sqlite3.Connection, billing_code: str, source_file_ids: list[int] | None = None,
                   code_type: str | None = None, filters: dict | None = None,
                   include_facility: bool = True) -> list[dict]:
    """price_groups (same scope: the group filters are ignored), each with its q1, median and q3 rates, Tukey's
    outlier fences and counts (see outliers), and its median relative to the reference group (see
    relative_prices)."""
    groups = price_groups(conn, billing_code, source_file_ids, code_type, filters, include_facility=include_facility)
    where, args = scope_where(billing_code, source_file_ids, code_type, without_group_filters(filters),
                              include_facility)
    rates: dict[tuple, list[float]] = {}
    for *key, rate in conn.execute(f"SELECT {GROUP_KEY_SQL}, r.negotiated_rate FROM rates r WHERE {where}", args):
        rates.setdefault(tuple(key), []).append(rate)
    summaries = []
    for g in groups:
        values = sorted(rates[group_key(g)])
        q1, median, q3 = quartiles(values)
        summaries.append({**g, "q1": q1, "median": median, "q3": q3, **outliers(values, q1, q3)})
    groups = summaries
    return relative_prices(groups, code_type)


def relative_prices(groups: list[dict], code_type: str | None) -> list[dict]:
    """Each group's median over the reference group's: "relative" (1.0 for the reference) and "reference".
    The reference is the largest group with no modifier and the code's own billing class (institutional for a
    revenue code, professional otherwise); else the largest of the code's own billing class, so a hospital group
    never sets the scale for a CPT's own rates; else simply the largest group. Largest in what the table counts
    (#44): a group's "count" (offers, under Count by) when it has one, else its rows; a tie goes to the group with
    more rows. The reference also carries "reference_rule", which of the three picked it: "base", "own_class" or
    "largest"."""
    own_class = "institutional" if code_type == "RC" else "professional"
    own = [g for g in groups if g["billing_class"] == own_class]
    base = [g for g in own if g["modifiers"] == ""]
    rule, candidates = ("base", base) if base else ("own_class", own) if own else ("largest", groups)
    if not candidates:
        return groups
    reference = max(candidates, key=lambda g: (g.get("count", g["rate_count"]), g["rate_count"]))
    scale = reference["median"]
    return [{**g, "reference": g is reference, "relative": g["median"] / scale if scale else None,
             **({"reference_rule": rule} if g is reference else {})} for g in groups]


EXPLAINED_MIN_COUNT = 30


def price_explained(conn: sqlite3.Connection, billing_code: str, source_file_ids: list[int] | None = None,
                    code_type: str | None = None, filters: dict | None = None,
                    min_count: int = EXPLAINED_MIN_COUNT) -> dict | None:
    """How much of the spread in this code's prices place of service and modifier account for: the share of the
    variation in log(rate) between groups (eta squared, between-group over total sum of squares) when grouping by
    modifiers, by place of service (service_codes), and by both. {"count", "modifier", "place", "both", "other"},
    other being 1 - both (within a group: mostly which provider). Log scale, so it is about ratios: twice the
    price counts the same at $50 as at $500. Over the code's own rates in scope (hospital rates billed under a
    revenue code are left out: billing class would swamp the rest), ignoring the group filters; rates <= 0 are
    skipped. None with fewer than min_count rates, or only one (modifier, place) combination."""
    where, args = scope_where(billing_code, source_file_ids, code_type, without_group_filters(filters),
                              include_facility=False)
    rows = conn.execute(
        "SELECT COALESCE(r.modifiers, ''), COALESCE(r.service_codes, ''), r.negotiated_rate FROM rates r"
        f" WHERE {where} AND r.negotiated_rate > 0", args).fetchall()
    if len(rows) < min_count or len({(m, p) for m, p, _ in rows}) < 2:
        return None
    logs = [math.log(rate) for _, _, rate in rows]
    mean = statistics.fmean(logs)
    total = sum((y - mean) ** 2 for y in logs)

    def share(key) -> float:
        """Between-group over total sum of squares, rounded to 9 places and kept within [0, 1]: float residue
        (1e-13) would otherwise show a fully explained spread as "Other -0%"."""
        sums: dict = {}
        for row, y in zip(rows, logs):
            n, s = sums.get(key(row), (0, 0.0))
            sums[key(row)] = (n + 1, s + y)
        between = sum(n * (s / n - mean) ** 2 for n, s in sums.values())
        return min(1.0, max(0.0, round(between / total, 9))) if total else 0.0

    both = share(lambda r: (r[0], r[1]))
    return {"count": len(rows), "modifier": share(lambda r: r[0]), "place": share(lambda r: r[1]),
            "both": both, "other": 1 - both}


def facility_count(conn: sqlite3.Connection, billing_code: str, source_file_ids: list[int] | None = None,
                   code_type: str | None = None) -> int:
    """How many hospital (facility) rates bill this code under a revenue code (0 for a revenue code itself)."""
    if code_type == "RC":
        return 0
    where, args = "r.billing_code_type = 'RC' AND r.additional_information = ?", [normalize_code(billing_code)]
    if source_file_ids:
        where += f" AND r.source_file_id IN ({', '.join('?' * len(source_file_ids))})"
        args += list(source_file_ids)
    return conn.execute(f"SELECT COUNT(*) FROM rates r WHERE {where}", args).fetchone()[0]


def procedure_names(conn: sqlite3.Connection, codes) -> dict:
    """{code: name} for the procedure codes (additional_information) that exist as a CPT or HCPCS code in our
    own data; others are left out."""
    codes = sorted({c for c in codes if c})
    if not codes:
        return {}
    rows = conn.execute(
        f"SELECT billing_code, MIN(name) FROM rates WHERE billing_code IN ({', '.join('?' * len(codes))})"
        " AND billing_code_type IN ('CPT', 'HCPCS') GROUP BY billing_code", codes)
    return {code: name for code, name in rows if name}


def department_names(conn: sqlite3.Connection, revenue_codes=None) -> dict:
    """{revenue code: department} from the code's own department name in the data, such as "Clinic General"
    for 510, shown without " General" ("Clinic"). Centene also files RC rates under procedure-style names in
    capitals ("OFFICE/OUTPATIENT VISIT EST"); the department name is the one in mixed case. Codes without one
    are left out. revenue_codes None means every revenue code (one pass over the RC rates, worth caching)."""
    where, codes = "", []
    if revenue_codes is not None:
        codes = sorted({c for c in revenue_codes if c})
        if not codes:
            return {}
        where = f"billing_code IN ({', '.join('?' * len(codes))}) AND "
    rows = conn.execute(
        f"SELECT billing_code, MIN(name) FROM rates WHERE {where}"
        "billing_code_type = 'RC' AND name GLOB '*[a-z]*' GROUP BY billing_code", codes)
    return {code: name.removesuffix(" General") for code, name in rows}


def code_info(conn: sqlite3.Connection, billing_code: str, code_type: str | None = None) -> dict | None:
    code = normalize_code(billing_code)
    where, args = "billing_code = ?", [code]
    if code_type:
        where += " AND billing_code_type = ?"
        args.append(code_type)
    row = conn.execute(
        f"SELECT billing_code, billing_code_type, name, description FROM rates WHERE {where} LIMIT 1", args,
    ).fetchone()
    if row is None:
        return None
    return dict(zip(("billing_code", "billing_code_type", "name", "description"), row))


def code_types(conn: sqlite3.Connection, billing_code: str, source_file_ids: list[int] | None = None) -> list[dict]:
    """The code types a number has, most rates first. Usually one; 731 is both CPT (anesthesia) and RC
    (ECG monitoring), unrelated services that only share the number."""
    where, args = "billing_code = ?", [normalize_code(billing_code)]
    if source_file_ids:
        where += f" AND source_file_id IN ({', '.join('?' * len(source_file_ids))})"
        args += list(source_file_ids)
    rows = conn.execute(
        f"SELECT billing_code_type, MIN(name), COUNT(*) FROM rates WHERE {where}"
        " GROUP BY billing_code_type ORDER BY COUNT(*) DESC, billing_code_type", args)
    return [{"billing_code_type": t, "name": name, "rate_count": n} for t, name, n in rows]


def list_codes(conn: sqlite3.Connection, source_file_ids: list[int] | None = None) -> list[dict]:
    """Every billing code in the DB (or in these files) with its name and how many rates it has, sorted by code."""
    where, args = ("", ()) if source_file_ids is None else (
        f" WHERE source_file_id IN ({', '.join('?' * len(source_file_ids))})", tuple(source_file_ids))
    cursor = conn.execute(
        f"SELECT billing_code, billing_code_type, name, MIN(description), COUNT(*) FROM rates{where}"
        " GROUP BY billing_code, billing_code_type, name ORDER BY billing_code, billing_code_type, name", args
    )
    keys = ("billing_code", "billing_code_type", "name", "description", "rate_count")
    return [dict(zip(keys, row)) for row in cursor]


def filter_codes(codes: list[dict], q: str) -> list[dict]:
    """Codes whose code, name or description contains q, ignoring case."""
    needle = (q or "").strip().casefold()
    if not needle:
        return codes
    return [
        c for c in codes
        if any(needle in (c[k] or "").casefold() for k in ("billing_code", "name", "description"))
    ]


def sort_codes(codes: list[dict], sort: str, direction: str) -> list[dict]:
    check_sort(sort, direction, CODE_SORTS)
    key = CODE_SORTS[sort]
    return sorted(codes, key=lambda c: (c[key] is None, c[key] or ""), reverse=direction == "desc")


def list_sources(conn: sqlite3.Connection) -> list[dict]:
    """Every loaded in-network file with the plans the index says use it, and its load warnings (None on databases
    from before source_files.warnings)."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(source_files)")}
    warnings_column = "warnings" if "warnings" in columns else "NULL"
    sources = []
    for sid, url, updated, loaded_at, rate_count, error, warnings in conn.execute(
        f"SELECT id, url, last_updated_on, loaded_at, rate_count, error, {warnings_column} FROM source_files ORDER BY id"
    ).fetchall():
        plans = conn.execute("SELECT plan_name FROM plans WHERE source_file_id = ? ORDER BY plan_name", (sid,)).fetchall()
        sources.append({
            "id": sid, "url": url, "file": file_name(url), "short_name": short_name(file_name(url)),
            "last_updated_on": updated,
            "loaded_at": loaded_at, "rate_count": rate_count, "error": error, "warnings": warnings,
            "plan_count": len(plans),
            "plan_names": sorted({name for (name,) in plans}),
        })
    return sources


def list_plans(conn: sqlite3.Connection) -> list[dict]:
    """Every plan in the index, by name then plan ID: [{"id", "plan_id", "plan_name", "source_file_id", "label"}].
    id is the plans row (plan_id alone isn't unique); label is "name · plan_id", what the plan box lists."""
    rows = conn.execute("SELECT id, plan_id, plan_name, source_file_id FROM plans ORDER BY plan_name, plan_id, id")
    return [{"id": pid, "plan_id": plan_id, "plan_name": name, "source_file_id": sid,
             "label": f"{name} · {plan_id}"} for pid, plan_id, name, sid in rows]


def find_plans(plans: list[dict], text: str) -> list[dict]:
    """Plans that typed text names, ignoring case: an exact label, plan ID or name if any matches, else every
    plan whose name or ID contains the text."""
    needle = (text or "").strip().casefold()
    if not needle:
        return []
    for key in ("label", "plan_id", "plan_name"):
        exact = [p for p in plans if (p[key] or "").casefold() == needle]
        if exact:
            return exact
    return [p for p in plans if needle in (p["plan_name"] or "").casefold() or needle in (p["plan_id"] or "").casefold()]


def plan_info(conn: sqlite3.Connection, plan_row_id: int) -> dict | None:
    """One plan (by plans row id) and the in-network files the index says it uses: every file listing the same
    plan ID and name. files: [{"id", "short_name", "plan_count"}], plan_count being how many plans share it."""
    row = conn.execute("SELECT id, plan_id, plan_id_type, plan_name, plan_market_type, issuer_name FROM plans"
                       " WHERE id = ?", (plan_row_id,)).fetchone()
    if row is None:
        return None
    info = dict(zip(("id", "plan_id", "plan_id_type", "plan_name", "plan_market_type", "issuer_name"), row))
    files = conn.execute(
        "SELECT s.id, s.url, (SELECT COUNT(*) FROM plans q WHERE q.source_file_id = s.id) FROM source_files s"
        " WHERE s.id IN (SELECT source_file_id FROM plans WHERE plan_id IS ? AND plan_name IS ?) ORDER BY s.id",
        (info["plan_id"], info["plan_name"])).fetchall()
    info["files"] = [{"id": sid, "short_name": short_name(file_name(url)), "plan_count": n} for sid, url, n in files]
    info["source_file_ids"] = [f["id"] for f in info["files"]]
    return info


# Columns the filter row offers choices for, as SQL over rates r.
CHOICE_COLUMNS = {
    "provider": "r.business_name", "ein": "r.ein", "npi": "r.npi", "billing_class": "r.billing_class",
    "setting": "r.setting", "service_codes": "r.service_codes", "modifiers": "r.modifiers",
    "procedure": "r.additional_information", "billed_as": BILLED_AS,
}


def column_choices(conn: sqlite3.Connection, billing_code: str, source_file_ids: list[int] | None = None,
                   select_limit: int = 50, suggest_limit: int = 2000, code_type: str | None = None,
                   include_facility: bool = True) -> dict:
    """How to filter each text column, from its distinct values for this code and these files (other
    filters don't narrow it): {name: {"distinct": n, "values": [...] or None, "control": ...}}.
    control is "fixed" when there is nothing to filter (one value on every row, or every row empty),
    "select" (a dropdown) for at most select_limit values, "suggest" (a box with the values as
    suggestions) for at most suggest_limit, otherwise "text" with values None. has_empty says whether
    some rows have no value, which makes even a single value worth filtering on. Values are text, sorted;
    NULL and "" are left out. service_codes and modifiers are stored comma-joined ("11,10"), and their
    choices are the distinct stored strings as they are, not split into single codes."""
    where, args = scope_where(billing_code, source_file_ids, code_type, None, include_facility)
    choices = {}
    for name, column in CHOICE_COLUMNS.items():
        present = f"{where} AND {column} IS NOT NULL AND {column} != ''"
        has_empty = conn.execute(
            f"SELECT EXISTS (SELECT 1 FROM rates r WHERE {where} AND ({column} IS NULL OR {column} = ''))",
            args).fetchone()[0] == 1
        values = [str(v) for (v,) in conn.execute(
            f"SELECT DISTINCT {column} FROM rates r WHERE {present} ORDER BY 1 LIMIT ?", (*args, suggest_limit + 1))]
        if len(values) > suggest_limit:
            distinct = conn.execute(f"SELECT COUNT(DISTINCT {column}) FROM rates r WHERE {present}", args).fetchone()[0]
            choices[name] = {"distinct": distinct, "values": None, "control": "text", "has_empty": has_empty}
        else:
            if len(values) == 0 or (len(values) == 1 and not has_empty):
                control = "fixed"
            else:
                control = "select" if len(values) <= select_limit else "suggest"
            choices[name] = {"distinct": len(values), "values": values, "control": control, "has_empty": has_empty}
    return choices


SUGGEST_LIMIT = 20


def _suggest(conn: sqlite3.Connection, column: str, clause: str, clause_args: list, billing_code: str | None,
             limit: int) -> list[dict]:
    where, args = scope_where(billing_code) if billing_code else ("1", [])
    rows = conn.execute(
        f"SELECT {column}, MIN(r.business_name), COUNT(DISTINCT r.business_name), COUNT(*) FROM rates r"
        f" WHERE {where} AND {column} IS NOT NULL AND {clause} GROUP BY {column} ORDER BY 4 DESC, 1 LIMIT ?",
        (*args, *clause_args, limit))
    return [{"value": str(value), "label": name, "names": names, "rates": count} for value, name, names, count in rows]


def suggest_npis(conn: sqlite3.Connection, q: str, billing_code: str | None = None,
                 limit: int = SUGGEST_LIMIT) -> list[dict]:
    """NPIs starting with the digits typed (all of them for none), among one code's rates (with its hospital rates)
    or every rate: [{"value", "label" (its first business name), "names" (how many), "rates"}], most rates first.
    NPIs are numbers, so a prefix is a range (10 digits, or 9 for the one short NPI): the index serves it."""
    q = (q or "").strip()
    if q and (not q.isdigit() or len(q) > 10):
        return []
    clause, args = "1", []
    if q:
        ranges = [(int(q) * 10 ** (n - len(q)), (int(q) + 1) * 10 ** (n - len(q)) - 1) for n in (10, 9) if len(q) <= n]
        clause = "(" + " OR ".join("r.npi BETWEEN ? AND ?" for _ in ranges) + ")"
        args = [v for pair in ranges for v in pair]
    return _suggest(conn, "r.npi", clause, args, billing_code, limit)


def suggest_eins(conn: sqlite3.Connection, q: str, billing_code: str | None = None,
                 limit: int = SUGGEST_LIMIT) -> list[dict]:
    """EINs starting with what was typed, with or without the dash (stored as 12-3456789); as suggest_npis."""
    digits = (q or "").strip().replace("-", "")
    if digits and (not digits.isdigit() or len(digits) > 9):
        return []
    clause, args = "1", []
    if digits:
        prefix = digits if len(digits) <= 2 else f"{digits[:2]}-{digits[2:]}"
        clause, args = "r.ein >= ? AND r.ein < ?", [prefix, prefix[:-1] + chr(ord(prefix[-1]) + 1)]
    return _suggest(conn, "r.ein", clause, args, billing_code, limit)
