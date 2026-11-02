"""Count offers, not rows (requirements #24).

A rates row is one price x provider group x NPI, so one organization's contract repeats once per NPI it lists
(and sometimes once per business name, when the same group appears under two provider references). This module
groups the rows of a search into offers, for the results table and the statistics:

- "provider": one line per organization offer: EIN + price + (billed as, procedure, modifiers, billing class,
  place of service) + source file. Its NPIs and names are listed. The default for the page.
- "npi": one line per clinician offer: NPI + EIN + price + the same group + source file, names merged.
- "none": every row, as search_rates returns them.

Grouping happens after the scope (code, files, column filters), so a filter on provider name keeps only the
matching rows' names in each line. Rows with no EIN (or no NPI) group together on the price as one offer.

How to call it from the page and the API (same arguments as search_rates / rate_stats, plus group_by):

    from api.grouping import GROUP_BY, search_grouped, stats_for_search
    table = search_grouped(conn, code, limit=..., offset=..., sort=..., direction=..., source_file_ids=...,
                           filters=..., code_type=..., include_facility=..., group_by="provider")
    stats = stats_for_search(conn, code, source_file_ids=..., code_type=..., filters=...,
                             include_facility=..., group_by="provider")

search_grouped returns search_rates' envelope (billing_code, total, limit, offset, sort, direction,
source_file_ids, filters, file_count, code_type, include_facility, results) plus group_by and row_count; total
counts lines (offers), row_count the rows behind them. Each result keeps search_rates' columns (business_name is
the names joined with "; ", npi is the NPI when the line has exactly one) and adds names, npis (at most
NPI_LIST_CAP), npi_count, row_count and source_file_id. With group_by="none" it is search_rates with
group_by/row_count added and names/npis/npi_count/row_count on each row.

stats_for_search returns rate_stats' shape (without mean: no averages on the page) computed over offers, plus
group_by and row_count, e.g. "59 organization offers (1,378 rows)" = count, row_count.

Lower level, for a scope already written as a WHERE clause over `rates r` (search.scope_where builds it):
grouped_rates(conn, where, args, group_by, ...) and grouped_stats(conn, where, args, group_by).
"""

import json
import sqlite3

from api.search import outliers as tukey_fences  # Tukey's fences, shared with rate_stats (#25)
from api.search import (
    iqr_spread,
    BILLED_AS, RATE_SORTS, check_sort, file_name, filter_clauses, histogram, density, normalize_code,
    percentiles, quartiles, scope_where, search_rates,
    GROUP_KEY_SQL, group_key, price_groups, relative_prices, without_group_filters,
)

GROUP_BY = ("provider", "npi", "none")
DEFAULT_GROUP_BY = "provider"
# A line lists at most this many NPIs (an IPA's offer can carry hundreds); npi_count has them all.
NPI_LIST_CAP = 50

# What makes two rows the same offer, besides the provider: the price and what it is a price for (the
# (billed as, procedure, modifiers, billing class, place of service) group of search.GROUP_COLUMNS) in one file.
OFFER_SQL = ("r.negotiated_rate", BILLED_AS, "COALESCE(r.additional_information, '')",
             "COALESCE(r.modifiers, '')", "COALESCE(r.billing_class, '')", "COALESCE(r.service_codes, '')",
             "r.source_file_id")
GROUP_SQL = {
    "provider": ("r.ein", *OFFER_SQL),
    "npi": ("r.npi", "r.ein", *OFFER_SQL),
}
# Extra sorts for grouped lines.
LINE_SORTS = {"npi_count": "COUNT(DISTINCT r.npi)", "row_count": "COUNT(*)"}


def check_group_by(group_by: str) -> str:
    if group_by not in GROUP_BY:
        raise ValueError(f"can't group by {group_by!r}; choose one of {', '.join(GROUP_BY)}")
    return group_by


# search_rates' columns (search.RESULT_COLUMNS) for a line, except npi and business_name (filled from the
# line's lists). Within a line the grouped columns are constant, so MIN is that value; the rest (name,
# description, setting, ...) take the line's smallest.
LINE_COLUMNS = ", ".join(f"MIN({sql}) AS {alias}" for alias, sql in {
    "billing_code": "r.billing_code", "billing_code_type": "r.billing_code_type", "name": "r.name",
    "description": "r.description", "billed_as": BILLED_AS, "procedure": "r.additional_information",
    "negotiated_type": "r.negotiated_type", "negotiated_rate": "r.negotiated_rate",
    "billing_class": "r.billing_class", "setting": "r.setting", "service_codes": "r.service_codes",
    "modifiers": "r.modifiers", "expiration_date": "r.expiration_date", "ein": "r.ein",
    "source_url": "s.url", "source_last_updated_on": "s.last_updated_on",
}.items())


def grouped_rates(conn: sqlite3.Connection, where_sql: str, args: list, group_by: str = DEFAULT_GROUP_BY,
                  sort: str = "rate", direction: str = "asc", limit: int = 100, offset: int = 0) -> dict:
    """The table lines for a scope (WHERE over rates r, as search.scope_where builds it): {"group_by", "total"
    (lines), "row_count" (rows), "results"}. Each line has search_rates' columns plus names (distinct, sorted),
    npis (distinct, sorted, at most NPI_LIST_CAP), npi_count, row_count and source_file_id. Sorts are
    search.RATE_SORTS (on a line: its smallest value, e.g. its first name or NPI) plus npi_count and row_count."""
    check_group_by(group_by)
    check_sort(sort, direction, {**RATE_SORTS, **LINE_SORTS})
    row_count = conn.execute(f"SELECT COUNT(*) FROM rates r WHERE {where_sql}", args).fetchone()[0]
    key = GROUP_SQL.get(group_by, ("r.id",))
    keys = ", ".join(key)
    total = conn.execute(f"SELECT COUNT(*) FROM (SELECT 1 FROM rates r WHERE {where_sql} GROUP BY {keys})",
                         args).fetchone()[0]
    order = LINE_SORTS.get(sort) or f"MIN({RATE_SORTS[sort]})"
    cursor = conn.execute(
        f"SELECT {LINE_COLUMNS}, MIN(r.source_file_id) AS source_file_id,"
        " json_group_array(DISTINCT r.business_name) AS names_json,"
        " json_group_array(DISTINCT r.npi) AS npis_json, COUNT(DISTINCT r.npi) AS npi_count, COUNT(*) AS row_count"
        f" FROM rates r JOIN source_files s ON s.id = r.source_file_id WHERE {where_sql} GROUP BY {keys}"
        f" ORDER BY {order} IS NULL, {order} {direction}, MIN(r.negotiated_rate), MIN(r.business_name), MIN(r.id)"
        " LIMIT ? OFFSET ?",
        (*args, limit, offset))
    columns = [d[0] for d in cursor.description]
    results = []
    for row in cursor:
        line = dict(zip(columns, row))
        names = sorted(n for n in json.loads(line.pop("names_json")) if n is not None)
        npis = sorted(n for n in json.loads(line.pop("npis_json")) if n is not None)
        line["names"] = names
        line["business_name"] = "; ".join(names) or None
        line["npis"] = npis[:NPI_LIST_CAP]
        line["npi"] = npis[0] if len(npis) == 1 else None
        line["source_file"] = file_name(line["source_url"])
        results.append(line)
    return {"group_by": group_by, "total": total, "row_count": row_count, "sort": sort, "direction": direction,
            "limit": limit, "offset": offset, "results": results}


def offer_rates(conn: sqlite3.Connection, where_sql: str, args: list, group_by: str) -> list[float]:
    """One rate per offer (per row for "none"), sorted."""
    check_group_by(group_by)
    if group_by == "none":
        sql = f"SELECT r.negotiated_rate FROM rates r WHERE {where_sql} ORDER BY 1"
    else:
        sql = (f"SELECT r.negotiated_rate FROM rates r WHERE {where_sql}"
               f" GROUP BY {', '.join(GROUP_SQL[group_by])} ORDER BY 1")
    return [v for (v,) in conn.execute(sql, args)]


def grouped_stats(conn: sqlite3.Connection, where_sql: str, args: list, group_by: str = DEFAULT_GROUP_BY,
                  min_counts: dict | None = None) -> dict | None:
    """rate_stats' statistics for a scope, each offer counting once: count (offers), min, q1, median, q3, iqr,
    max, distinct_rates, the percentiles (a tail percentile needs min_counts offers, default
    search.PERCENTILE_MIN_COUNTS), Tukey's fences and outlier counts, histogram and density, all over offers.
    No mean. provider_count (distinct names), ein_count and npi_count are distinct over the rows. Adds group_by
    and row_count (the rows behind the offers). None when nothing matches."""
    check_group_by(group_by)
    row_count, providers, eins, npis = conn.execute(
        "SELECT COUNT(*), COUNT(DISTINCT r.business_name), COUNT(DISTINCT r.ein), COUNT(DISTINCT r.npi)"
        f" FROM rates r WHERE {where_sql}", args).fetchone()
    if row_count == 0:
        return None
    values = offer_rates(conn, where_sql, args, group_by)
    low, high = values[0], values[-1]
    q1, median, q3 = quartiles(values)
    bins = histogram(values, low, high)
    return {
        "group_by": group_by, "count": len(values), "row_count": row_count,
        "provider_count": providers, "ein_count": eins, "npi_count": npis,
        "min": low, "q1": q1, "median": median, "q3": q3, "iqr": q3 - q1, "max": high,
        **iqr_spread(q1, median, q3),
        "distinct_rates": len(set(values)), **percentiles(values, min_counts), **tukey_fences(values, q1, q3),
        "histogram": bins, "density": density(values, low, high, bins[0]["high"] - bins[0]["low"], q3 - q1),
    }


def search_grouped(
    conn: sqlite3.Connection, billing_code: str, limit: int = 100, offset: int = 0,
    sort: str = "rate", direction: str = "asc", source_file_ids: list[int] | None = None,
    filters: dict | None = None, code_type: str | None = None, include_facility: bool = True,
    group_by: str = DEFAULT_GROUP_BY,
) -> dict:
    """search_rates with group_by: the same arguments and envelope, total counting lines, plus group_by and
    row_count. Raises ValueError like search_rates for a bad sort, filter or group_by."""
    check_group_by(group_by)
    where, args = scope_where(billing_code, source_file_ids, code_type, filters, include_facility)
    if group_by == "none" and sort not in LINE_SORTS:
        result = search_rates(conn, billing_code, limit, offset, sort, direction, source_file_ids, filters,
                              code_type, include_facility)
        for row in result["results"]:
            row.update(names=[row["business_name"]] if row["business_name"] else [],
                       npis=[row["npi"]] if row["npi"] is not None else [],
                       npi_count=int(row["npi"] is not None), row_count=1)
        return {**result, "group_by": group_by, "row_count": result["total"]}
    lines = grouped_rates(conn, where, args, group_by, sort, direction, limit, offset)
    file_count = conn.execute(f"SELECT COUNT(DISTINCT r.source_file_id) FROM rates r WHERE {where}",
                              args).fetchone()[0]
    return {"billing_code": normalize_code(billing_code), "total": lines["total"], "limit": limit,
            "offset": offset, "sort": sort, "direction": direction,
            "source_file_ids": list(source_file_ids or []), "filters": filter_clauses(filters)[2],
            "file_count": file_count, "code_type": code_type, "include_facility": include_facility,
            "results": lines["results"], "group_by": group_by, "row_count": lines["row_count"]}


def stats_for_search(conn: sqlite3.Connection, billing_code: str, source_file_ids: list[int] | None = None,
                     code_type: str | None = None, filters: dict | None = None, include_facility: bool = True,
                     group_by: str = DEFAULT_GROUP_BY) -> dict | None:
    """rate_stats with group_by: the same arguments, statistics over offers (see grouped_stats)."""
    where, args = scope_where(billing_code, source_file_ids, code_type, filters, include_facility)
    return grouped_stats(conn, where, args, group_by)


def grouped_stats_by_group(conn: sqlite3.Connection, billing_code: str, source_file_ids: list[int] | None = None,
                           code_type: str | None = None, filters: dict | None = None, include_facility: bool = True,
                           group_by: str = DEFAULT_GROUP_BY) -> list[dict]:
    """search.stats_by_group counted by offers: price_groups (the group filters ignored; rate_count and
    provider_count still over rows) with each group's count (offers, or rows for "none"), q1, median, q3 and
    Tukey's fences and outlier counts over one rate per offer, and its median relative to the reference group's."""
    check_group_by(group_by)
    groups = price_groups(conn, billing_code, source_file_ids, code_type, filters, include_facility=include_facility)
    where, args = scope_where(billing_code, source_file_ids, code_type, without_group_filters(filters),
                              include_facility)
    grouping = "" if group_by == "none" else f" GROUP BY {', '.join(GROUP_SQL[group_by])}"
    rates: dict[tuple, list[float]] = {}
    for *key, rate in conn.execute(f"SELECT {GROUP_KEY_SQL}, r.negotiated_rate FROM rates r WHERE {where}{grouping}",
                                   args):
        rates.setdefault(tuple(key), []).append(rate)
    summaries = []
    for g in groups:
        values = sorted(rates[group_key(g)])
        q1, median, q3 = quartiles(values)
        summaries.append({**g, "count": len(values), "q1": q1, "median": median, "q3": q3,
                          **tukey_fences(values, q1, q3)})
    return relative_prices(summaries, code_type)
