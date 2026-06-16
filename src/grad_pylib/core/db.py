import threading

from sqlalchemy import URL, create_engine
from sqlalchemy.engine import Engine

from grad_pylib.core.config import BaseAppSettings, get_settings


def build_mssql_url(odbc_connection_string: str) -> str:
    return URL.create("mssql+pyodbc", query={"odbc_connect": odbc_connection_string}).render_as_string(
        hide_password=False
    )


def resolve_database_url(settings: BaseAppSettings | None = None) -> str:
    settings = settings or get_settings()
    if settings.database_url:
        return build_mssql_url(settings.database_url)
    raise ValueError("DATABASE_URL must be set.")


_engine: Engine | None = None
_engine_lock = threading.Lock()


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        with _engine_lock:
            if _engine is None:
                _engine = create_engine(
                    resolve_database_url(),
                    pool_pre_ping=True,
                    pool_size=10,  # Adjust based on your MSSQL capacity
                    max_overflow=20  # Helps handle the "Scenario 2" concurrent queries spikes
                )
    return _engine
