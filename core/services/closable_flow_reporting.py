"""Protected closable-flow summary; the shared report reads, never writes."""

from fastapi import HTTPException

from .. import closable_flow_report
from ..models.experiment import current_time_ms


def session_query(session):
    connection = session.connection()

    def query(sql, params=()):
        return [dict(row._mapping) for row in connection.exec_driver_sql(sql, tuple(params))]

    return query


def closable_flow_summary(*, session, analytics_environment="production", measurement_revision, since_ms=None,
                          until_ms=None, as_of_ms=None, app_version=None, build_number=None):
    try:
        return closable_flow_report.build_summary(
            session_query(session), analytics_environment=analytics_environment, revision=measurement_revision,
            as_of_ms=as_of_ms, now=current_time_ms(), since_ms=since_ms, until_ms=until_ms,
            app_version=app_version, build_number=build_number)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid closable-flow report window or revision") from None
