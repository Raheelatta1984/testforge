import uuid
import re
from datetime import datetime
from sqlalchemy import (create_engine, String, Text, Integer, Float, Boolean, DateTime, ForeignKey, JSON, func, Column)
from sqlalchemy import inspect, text, types as sqltypes
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker, validates
from sqlalchemy.pool import StaticPool, QueuePool
from app.config import DATABASE_URL
from app.errors import redact

# StaticPool shares ONE transaction-bearing connection between unrelated
# sessions. An autosave read/rollback can otherwise erase a recorder's flush.
# File SQLite uses two bounded, reused connections (no overflow or new workers).
# In-memory SQLite must retain its one connection to preserve the database.
if DATABASE_URL.startswith("sqlite"):
    memory = DATABASE_URL.endswith(':memory:') or DATABASE_URL in {'sqlite://', 'sqlite+pysqlite://'}
    pool_options = {'poolclass': StaticPool} if memory else {
        'poolclass': QueuePool, 'pool_size': 2, 'max_overflow': 0, 'pool_timeout': 10,
    }
    engine = create_engine(
        DATABASE_URL,
        connect_args={"check_same_thread": False, "timeout": 30},
        **pool_options,
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

# Width of the display text stored on a step, matching `RecordingStep.label`.
# Postgres refuses a value that is too long for the column instead of cutting it,
# and a D365 staging URL redirects to a login URL of several hundred characters,
# so the recorder's `Open <url>` label used to fail the whole insert with
# `StringDataRightTruncation: value too long for type character varying(300)`.
LABEL_LIMIT = 500


def fit_text(value, limit=LABEL_LIMIT):
    """Clip display text to what a `String(limit)` column can store.

    Only labels are clipped this way: the authoritative data (the URL, the
    locator, the typed value) lives in `value`/`selector`, so an ellipsis costs
    the replay nothing. `None` stays `None` because the column is nullable.
    """
    if value is None:
        return None
    text = str(value)
    if limit is None or limit < 1 or len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"

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
    purpose: Mapped[str] = mapped_column(Text, default="")
    start_url: Mapped[str] = mapped_column(String(1000))
    execution_plan: Mapped[dict] = mapped_column(JSON, nullable=True)
    input_defaults: Mapped[dict] = mapped_column(JSON, nullable=True)
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
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    duration_ms: Mapped[float] = mapped_column(Float, nullable=True)
    repeat_count: Mapped[int] = mapped_column(Integer, default=1)  # how many times to repeat this step
    recording: Mapped["Recording"] = relationship(back_populates="steps")

    @validates("label")
    def _fit_label(self, _key, value):
        """Keep every label at or below the width the column declares.

        The recorder, the step editor, the library importer and the compressor
        all write labels; a writer that forgets the limit would otherwise lose
        the whole row to a Postgres `value too long` error.
        """
        return fit_text(value)

class Run(Base):
    __tablename__ = "runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    recording_id: Mapped[str] = mapped_column(ForeignKey("recordings.id"))
    variable_snapshot: Mapped[dict] = mapped_column(JSON, nullable=True)
    input_overrides: Mapped[dict] = mapped_column(JSON, nullable=True)
    snapshot_steps: Mapped[list] = mapped_column(JSON, nullable=True)
    sequence: Mapped[int] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(50), default="queued") # queued, running, failed, passed, investigating, cancelled
    progress_pct: Mapped[int] = mapped_column(Integer, default=0)
    execution_log: Mapped[list] = mapped_column(JSON, default=list)
    video_path: Mapped[str] = mapped_column(String(1000), nullable=True)

    # ROG AGENT INVESTIGATION
    rog_monitor_log: Mapped[str] = mapped_column(Text, nullable=True)
    rog_devops_log: Mapped[str] = mapped_column(Text, nullable=True)
    rog_qa_log: Mapped[str] = mapped_column(Text, nullable=True)

    # Queue hygiene and batch execution. All nullable: rows written by an older
    # revision keep working, and repair_schema() adds the columns to a database
    # that was created before they existed.
    batch_id: Mapped[str] = mapped_column(String(36), nullable=True)   # Batch this run belongs to
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)  # when the browser actually took it
    cancel_reason: Mapped[str] = mapped_column(String(500), nullable=True) # why the queue dropped it
    # Speed factor this run replayed at (0.25-3.0). Nullable: runs created before
    # it existed have no recorded speed, and reports say so rather than guess.
    execution_speed: Mapped[float] = mapped_column(Float, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    finished_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    recording: Mapped["Recording"] = relationship(back_populates="runs")


class Batch(Base):
    """One adaptive batch execution of many recordings through a single browser.

    A batch is a row of its own so the dashboard can show progress and a report
    after the worker that created it is long gone (or was restarted mid-flight).
    """

    __tablename__ = "batches"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    name: Mapped[str] = mapped_column(String(200), nullable=True)
    status: Mapped[str] = mapped_column(String(50), default="queued")  # queued, running, passed, partial, failed, cancelled, error
    total: Mapped[int] = mapped_column(Integer, default=0)
    done: Mapped[int] = mapped_column(Integer, default=0)
    passed: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)
    skipped: Mapped[int] = mapped_column(Integer, default=0)
    progress_pct: Mapped[int] = mapped_column(Integer, default=0)
    options: Mapped[dict] = mapped_column(JSON, default=dict)
    report: Mapped[dict] = mapped_column(JSON, default=dict)   # timings, pacing profile, resources
    error: Mapped[str] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)

class ManagedKey(Base):
    """Encryption keyring the application provisions and owns itself.

    Exists so a deployment can encrypt without an operator pasting
    ``TF_VARIABLE_ENCRYPTION_KEYS`` into the hosting platform. The trade-off is
    deliberate and not hidden: this key sits in the same database as the
    ciphertext, so it protects the library tree that gets published to GitHub —
    the actual leak path for this application — and *not* a dump of the database
    itself. An operator who wants the key to live outside the data still sets the
    environment variable, which keeps precedence over every row here.

    Rows are never deleted on rotation. The newest key encrypts; the older ones
    stay so values sealed before the rotation (including queued run snapshots)
    still decrypt. Two processes racing to create the first key is harmless:
    both land in the keyring and both decrypt.
    """

    __tablename__ = "managed_keys"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    purpose: Mapped[str] = mapped_column(String(50), default="variables", index=True)
    sequence: Mapped[int] = mapped_column(Integer, default=1)  # highest wins; ties are harmless
    material: Mapped[str] = mapped_column(Text, nullable=False)
    note: Mapped[str] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class AppSetting(Base):
    """Instance-wide master setup (region, time zone, date/time format, language).

    One row per key; the master settings live under ``master``. Kept in the
    database rather than the library tree on purpose: how this deployment shows
    its dates and language is not test content, so it is never published to
    GitHub and every user of the instance sees the same values.
    """

    __tablename__ = "app_settings"
    key: Mapped[str] = mapped_column(String(60), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


# Result of the most recent schema check. Exposed by /api/diagnostics so a bad
# deployment can be diagnosed from the dashboard instead of the server logs.
SCHEMA_STATUS = {
    "ready": False,
    "dialect": None,
    "error": None,
    "repairs": [],
    "tables": {},
}


def _boolean_literal(dialect, value):
    """A boolean literal the target dialect accepts in raw SQL.

    `true`/`false` are PostgreSQL and SQLite spellings. SQL Server stores a
    boolean as BIT, and `UPDATE ... SET flag = 'false'` there fails with
    "Conversion failed when converting the varchar value 'false' to data type
    bit", which would leave the new NOT NULL column unfilled.
    """
    if getattr(dialect, "name", "") == "mssql":
        return "1" if value else "0"
    return "true" if value else "false"


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
            return _boolean_literal(dialect, arg)
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
        return _boolean_literal(dialect, False)
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


def _widen_narrow_columns(inspector, connection, table, repairs):
    """Widen a legacy VARCHAR that the models declare wider, or as TEXT.

    `create_all()` only creates missing *tables*, so a database created by an
    earlier revision keeps its original widths: `recording_steps.label` was
    `varchar(300)` in a database whose model now says 500. Postgres refuses an
    over-long value rather than cutting it, so a long label -- the
    `login.microsoftonline.com` URL a D365 staging URL redirects to -- failed the
    insert and the dashboard showed
    `Failed to save step: ... value too long for type character varying(300)`.
    SQLite ignores a VARCHAR length, so this only runs on Postgres.
    """
    if connection.dialect.name != "postgresql":
        return
    model_columns = {column.name: column for column in table.columns}
    quote = connection.dialect.identifier_preparer.quote
    for info in inspector.get_columns(table.name):
        name = info["name"]
        column = model_columns.get(name)
        if column is None or not isinstance(column.type, sqltypes.String):
            continue
        current = info["type"]
        limit = getattr(current, "length", None)
        if limit is None:
            continue  # TEXT or a varchar with no length: already holds anything.
        wanted = getattr(column.type, "length", None)
        if wanted is not None and wanted <= limit:
            continue  # As wide as the model asks for: leave it alone.
        target = column.type.compile(connection.dialect)
        try:
            connection.execute(
                text(f"ALTER TABLE {quote(table.name)} ALTER COLUMN {quote(name)} TYPE {target}")
            )
            repairs.append(f"{table.name}.{name}: widened {current} -> {target}")
        except Exception as exc:
            repairs.append(f"{table.name}.{name}: not widened ({redact(exc)})")


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
            _widen_narrow_columns(inspector, connection, table, SCHEMA_STATUS["repairs"])
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
    from app.plan_runtime import resolve_values
    values = {}
    for v in vars_found:
        from app.secrets_store import SecretValue, PREFIX
        value = SecretValue(v.value) if str(v.value).startswith(PREFIX) else v.value
        if isinstance(value, str) and value.startswith('env:'):
            from app.plan_runtime import variables
            value = variables({v.name: {'env': value[4:]}})[v.name]
        values[v.name] = value
    return resolve_values(values)

def apply_variables(text, var_map):
    if not text: return text
    def replacer(match):
        key = match.group(1)
        return str(var_map.get(key, match.group(0)))
    return VAR_REGEX.sub(replacer, str(text))


# Older modules import this name. Keep both so execution can load.
interpolate = apply_variables