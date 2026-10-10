import uuid
import re
from datetime import datetime
from sqlalchemy import (create_engine, String, Text, Integer, Boolean, DateTime, ForeignKey, JSON, func, Column)
from sqlalchemy import inspect, text, types as sqltypes
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker
from sqlalchemy.pool import StaticPool
from app.config import DATABASE_URL
from app.errors import redact

# SQLite cannot use a large QueuePool (and the default pool is not safe to share
# with Playwright tasks on the event-loop thread). Postgres keeps a real pool.
if DATABASE_URL.startswith("sqlite"):
    engine = create_engine(
        DATABASE_URL,
        connect_args={"check_same_thread": False, "timeout": 30},
        poolclass=StaticPool,
    )
else:
    engine = create_engine(
        DATABASE_URL,
        pool_pre_ping=True,
        pool_size=20,
        max_overflow=10,
        pool_recycle=3600,
    )
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)

def generate_uuid(): return str(uuid.uuid4())

class Base(DeclarativeBase): pass

class Project(Base):
    __tablename__ = "projects"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    base_url: Mapped[str] = mapped_column(String(500))
    industry_type: Mapped[str] = mapped_column(String(100), default="Generic")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    recordings: Mapped[list["Recording"]] = relationship(back_populates="project", cascade="all, delete-orphan")
    variables: Mapped[list["Variable"]] = relationship(back_populates="project", cascade="all, delete-orphan")

class Variable(Base):
    __tablename__ = "variables"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"))
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    value: Mapped[str] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String(50), default="General") # AI, Auth, Data
    is_secret: Mapped[bool] = mapped_column(Boolean, default=False)
    project: Mapped["Project"] = relationship(back_populates="variables")

class Recording(Base):
    __tablename__ = "recordings"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"))
    parent_id: Mapped[str] = mapped_column(String(36), ForeignKey("recordings.id"), nullable=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    start_url: Mapped[str] = mapped_column(String(1000))
    tags: Mapped[str] = mapped_column(String(500), default="AI_PENDING")
    status: Mapped[str] = mapped_column(String(50), default="active")
    video_path: Mapped[str] = mapped_column(String(1000), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    project: Mapped["Project"] = relationship(back_populates="recordings")
    steps: Mapped[list["RecordingStep"]] = relationship(back_populates="recording", order_by="RecordingStep.order", cascade="all, delete-orphan")
    runs: Mapped[list["Run"]] = relationship(back_populates="recording", cascade="all, delete-orphan")

class RecordingStep(Base):
    __tablename__ = "recording_steps"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    recording_id: Mapped[str] = mapped_column(ForeignKey("recordings.id"))
    order: Mapped[int] = mapped_column(Integer, nullable=False)
    action: Mapped[str] = mapped_column(String(100)) # click, fill, voice_command, key_press
    selector: Mapped[dict] = mapped_column(JSON, nullable=True)
    value: Mapped[str] = mapped_column(Text, nullable=True)
    label: Mapped[str] = mapped_column(String(500), nullable=True)
    screenshot_path: Mapped[str] = mapped_column(String(1000), nullable=True)
    repeat_count: Mapped[int] = mapped_column(Integer, default=1)  # how many times to repeat this step
    recording: Mapped["Recording"] = relationship(back_populates="steps")

class Run(Base):
    __tablename__ = "runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    recording_id: Mapped[str] = mapped_column(ForeignKey("recordings.id"))
    status: Mapped[str] = mapped_column(String(50), default="queued") # queued, running, failed, passed, investigating
    progress_pct: Mapped[int] = mapped_column(Integer, default=0)
    execution_log: Mapped[list] = mapped_column(JSON, default=list)
    video_path: Mapped[str] = mapped_column(String(1000), nullable=True)
    
    # ROG AGENT INVESTIGATION
    rog_monitor_log: Mapped[str] = mapped_column(Text, nullable=True)
    rog_devops_log: Mapped[str] = mapped_column(Text, nullable=True)
    rog_qa_log: Mapped[str] = mapped_column(Text, nullable=True)
    
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    finished_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    recording: Mapped["Recording"] = relationship(back_populates="runs")

# Result of the most recent schema check. Exposed by /api/diagnostics so a bad
# deployment can be diagnosed from the dashboard instead of the server logs.
SCHEMA_STATUS = {
    "ready": False,
    "dialect": None,
    "error": None,
    "repairs": [],
    "tables": {},
}


def _backfill_sql(column, dialect):
    """SQL literal used to populate a newly added column on rows that already exist.

    Returns None when the column can simply stay NULL.
    """
    for source in (column.server_default, column.default):
        if source is None:
            continue
        arg = getattr(source, "arg", None)
        if arg is None:
            continue
        if isinstance(arg, bool):
            return "true" if arg else "false"
        if isinstance(arg, (int, float)):
            return str(arg)
        if isinstance(arg, str):
            return "'" + arg.replace("'", "''") + "'"
    if getattr(column.default, "is_scalar", False) is False and column.default is not None:
        # Python-side defaults such as `default=list` are not SQL literals.
        pass
    if column.nullable:
        return None
    column_type = column.type
    if isinstance(column_type, sqltypes.JSON):
        return "'{}'"
    if isinstance(column_type, sqltypes.Boolean):
        return "false"
    if isinstance(column_type, (sqltypes.Integer, sqltypes.Numeric)):
        return "0"
    if isinstance(column_type, sqltypes.DateTime):
        return "CURRENT_TIMESTAMP"
    if isinstance(column_type, (sqltypes.String, sqltypes.Text)):
        return "''"
    return None


def _add_missing_columns(inspector, connection, table, repairs):
    """Add columns that the models declare but the existing table does not have.

    `create_all()` only creates missing *tables*, so a database created by an
    older revision keeps its original columns. A stale `projects` table (no
    `industry_type`) is exactly what made project creation return HTTP 500.
    """
    existing = {column["name"] for column in inspector.get_columns(table.name)}
    quote = connection.dialect.identifier_preparer.quote
    for column in table.columns:
        if column.name in existing:
            continue
        try:
            type_sql = column.type.compile(connection.dialect)
        except Exception as exc:
            repairs.append(f"{table.name}.{column.name}: not added ({redact(exc)})")
            continue
        connection.execute(
            text(f"ALTER TABLE {quote(table.name)} ADD COLUMN {quote(column.name)} {type_sql}")
        )
        fill = _backfill_sql(column, connection.dialect)
        if fill is not None:
            connection.execute(
                text(
                    f"UPDATE {quote(table.name)} SET {quote(column.name)} = {fill} "
                    f"WHERE {quote(column.name)} IS NULL"
                )
            )
        repairs.append(
            f"{table.name}.{column.name}: added"
            + (f" (backfilled with {fill})" if fill is not None else "")
        )


def _relax_legacy_columns(inspector, connection, table, repairs):
    """Make NOT NULL columns that the models no longer know about nullable.

    Older revisions had columns such as `runs.target_id` and `variables.scope`
    that were NOT NULL. Inserts built from the current models never supply them,
    so they have to accept NULL or every write to those tables fails.
    """
    if connection.dialect.name != "postgresql":
        return
    model_columns = {column.name for column in table.columns}
    quote = connection.dialect.identifier_preparer.quote
    for info in inspector.get_columns(table.name):
        name = info["name"]
        if name in model_columns or info.get("nullable", True):
            continue
        try:
            connection.execute(
                text(f"ALTER TABLE {quote(table.name)} ALTER COLUMN {quote(name)} DROP NOT NULL")
            )
            repairs.append(f"{table.name}.{name}: NOT NULL relaxed (legacy column)")
        except Exception as exc:
            repairs.append(f"{table.name}.{name}: NOT NULL not relaxed ({redact(exc)})")


def _ensure_schema():
    Base.metadata.create_all(bind=engine)
    with engine.begin() as connection:
        inspector = inspect(connection)
        tables_in_db = set(inspector.get_table_names())
        SCHEMA_STATUS["dialect"] = connection.dialect.name
        for table in Base.metadata.sorted_tables:
            if table.name not in tables_in_db:
                SCHEMA_STATUS["tables"][table.name] = "created"
                continue
            _add_missing_columns(inspector, connection, table, SCHEMA_STATUS["repairs"])
            _relax_legacy_columns(inspector, connection, table, SCHEMA_STATUS["repairs"])
            SCHEMA_STATUS["tables"][table.name] = "ok"


def init_db():
    """Create missing tables and migrate existing ones to the current schema.

    Never raises: the dashboard stays up and /api/diagnostics reports the
    problem, which is far easier to debug than an opaque HTTP 500.
    """
    SCHEMA_STATUS["repairs"] = []
    SCHEMA_STATUS["tables"] = {}
    SCHEMA_STATUS["error"] = None
    try:
        _ensure_schema()
        SCHEMA_STATUS["ready"] = True
        print("ROG DATABASE INITIALIZED")
        for repair in SCHEMA_STATUS["repairs"]:
            print(f"SCHEMA REPAIR: {repair}")
    except Exception as exc:
        SCHEMA_STATUS["ready"] = False
        SCHEMA_STATUS["error"] = redact(exc)
        print(f"DATABASE FATAL ERROR: {SCHEMA_STATUS['error']}")
    return SCHEMA_STATUS


def repair_schema():
    """Re-run the schema check, e.g. after a write fails because of schema drift."""
    status = init_db()
    return status["ready"]

# VARIABLE INTERPOLATION ENGINE
VAR_REGEX = re.compile(r"\{\{\s*([\w.\-]+)\s*\}\}")

def resolve_variables(db, project_id):
    vars_found = db.query(Variable).filter(Variable.project_id == project_id).all()
    return {v.name: v.value for v in vars_found}

def apply_variables(text, var_map):
    if not text: return text
    def replacer(match):
        key = match.group(1)
        return str(var_map.get(key, match.group(0)))
    return VAR_REGEX.sub(replacer, str(text))


# Older modules import this name. Keep both so execution can load.
interpolate = apply_variables