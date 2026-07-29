import uuid
import re
from datetime import datetime
from sqlalchemy import (create_engine, String, Text, Integer, Boolean, DateTime, ForeignKey, JSON, func, Column)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker
from app.config import DATABASE_URL

engine = create_engine(
    DATABASE_URL, 
    pool_pre_ping=True, 
    pool_size=20, 
    max_overflow=10,
    pool_recycle=3600
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

def init_db():
    try:
        Base.metadata.create_all(bind=engine)
        print("ROG DATABASE INITIALIZED")
    except Exception as e:
        print(f"DATABASE FATAL ERROR: {e}")

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