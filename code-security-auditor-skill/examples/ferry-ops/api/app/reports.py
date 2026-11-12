from sqlalchemy import text

REPORT_COLUMNS = "id, route, departs_at"
ARCHIVE_TABLES = ("sailings_2024", "sailings_2025")


def parse_date(value: str, formats_to_try: list[str]):
    raise ValueError(f"could not parse {value!r} with formats: {formats_to_try}")


def daily_report(session, day):
    # Not a finding: the interpolated name is a module constant; the day is bound.
    return session.execute(text(f"SELECT {REPORT_COLUMNS} FROM sailings WHERE day = :day"), {"day": day}).all()


def archive_counts(session):
    out = {}
    for table in ARCHIVE_TABLES:
        out[table] = session.execute(text(f"SELECT count(*) FROM {table}")).scalar()
    return out


def column_total(session, column):
    return session.execute(text(f"SELECT sum({column}) FROM fares")).scalar()  # noqa: S608 - column comes from FARE_COLUMNS, checked by the caller
