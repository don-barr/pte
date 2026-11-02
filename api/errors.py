"""What every page and API says when there is nothing to show: no database, or one with no rates loaded."""

NO_DATA = "No data loaded yet — run the ETL to build the rates database."


class NoData(Exception):
    """The DB file is missing, empty, or has no rates loaded."""
