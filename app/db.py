import uuid
import re
import os
from datetime import datetime
from sqlalchemy import (create_engine, String, Text, Integer, Boolean, DateTime, ForeignKey, JSON, func)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker
from app.config import DATABASE_URL

# DATABASE ENGINE INITIALIZATION
# pool_pre_ping ensures we reconnect to Neon if the connection times out
engine = create_engine(
    DATABASE_URL, 
    connect_args={"check_same_thread": False} if "sqlite" in DATABASE_URL else {},
    pool_pre_ping=True
)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

def uid() -> str:
    return str(uuid.uuid4())

class Base(DeclarativeBase):
    pass

# PROJECT MODEL
class Project(Base):
    __tablename__ = "projects"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(200))
    base_url: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    # Relationship to recordings under this project
    recordings: Mapped[list["Recording"]] = relationship(back_populates="project", cascade="all, delete-orphan")

# VARIABLE MODEL (For 3-tier scoped variables)
class Variable(Base):
    __tablename__ = "variables"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    scope: Mapped[str] = mapped_column(String(20)) # 'global' or 'project'
    project_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("projects.id"), nullable=True)
    name: Mapped[str] = mapped_column(String(100))
    value: Mapped[str] = mapped_column(Text, default="")

# RECORDING MODEL (Hierarchical Tree Structure)
class Recording(Base):
    __tablename__ = "recordings"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    project_id: Mapped[str] = mapped_column(String(36), ForeignKey("projects.id"))
    parent_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("recordings.id"), nullable=True)
    name: Mapped[str] = mapped_column(String(200))
    start_url: Mapped[str] = mapped_column(String(500), default="")
    shared: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(20), default="ready")
    video_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    
    project: Mapped[Project] = relationship(back_populates="recordings")
    steps: Mapped[list["RecordingStep"]] = relationship(back_populates="recording", order_by="RecordingStep.order", cascade="all, delete-orphan")

# RECORDING STEP MODEL
class RecordingStep(Base):
    __tablename__ = "recording_steps"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    recording_id: Mapped[str] = mapped_column(String(36), ForeignKey("recordings.id"))
    order: Mapped[int] = mapped_column(Integer)
    action: Mapped[str] = mapped_column(String(30)) # navigate, click, fill, press
    selector: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    value: Mapped[str | None] = mapped_column(Text, nullable=True)
    url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    label: Mapped[str | None] = mapped_column(String(300), nullable=True)
    screenshot_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    
    recording: Mapped[Recording] = relationship(back_populates="steps")

# EXECUTION RUN MODEL
class Run(Base):
    __tablename__ = "runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    target_type: Mapped[str] = mapped_column(String(20)) # 'recording' or 'scenario'
    target_id: Mapped[str] = mapped_column(String(36))
    status: Mapped[str] = mapped_column(String(20), default="queued") # queued, running, passed, failed
    log: Mapped[list] = mapped_column(JSON, default=list) # Full step execution results
    video_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

# DATABASE UTILITIES
def init_db():
    try:
        Base.metadata.create_all(bind=engine)
        print("Neon/Local Database Tables Initialized.")
    except Exception as e:
        print(f"Database initialization error: {e}")

VAR_PATTERN = re.compile(r"\{\{\s*([\w.\-]+)\s*\}\}")

def resolve_variables(db, project_id=None):
    """Merges global and project-specific variables into a single dictionary."""
    merged = {}
    try:
        # Load global variables
        globals = db.query(Variable).filter(Variable.scope == "global").all()
        for v in globals:
            merged[v.name] = v.value
        # Overwrite with project variables if they exist
        if project_id:
            projects = db.query(Variable).filter(Variable.project_id == project_id).all()
            for v in projects:
                merged[v.name] = v.value
    except Exception as e:
        print(f"Variable resolution error: {e}")
    return merged

def interpolate(text, variables):
    """Replaces {{variable_name}} in a string with the actual value from the dictionary."""
    if text is None:
        return None
    try:
        return VAR_PATTERN.sub(lambda m: str(variables.get(m.group(1), m.group(0))), str(text))
    except Exception:
        return str(text)