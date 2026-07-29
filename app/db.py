import uuid, re, os
from datetime import datetime
from sqlalchemy import (create_engine, String, Text, Integer, Boolean, DateTime, ForeignKey, JSON, func)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker
from app.config import DATABASE_URL

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

def uid() -> str: return str(uuid.uuid4())
class Base(DeclarativeBase): pass

class Project(Base):
    __tablename__ = "projects"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(200))
    base_url: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    recordings: Mapped[list["Recording"]] = relationship(back_populates="project", cascade="all, delete-orphan")

class Variable(Base):
    __tablename__ = "variables"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    scope: Mapped[str] = mapped_column(String(20))
    project_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("projects.id"), nullable=True)
    name: Mapped[str] = mapped_column(String(100))
    value: Mapped[str] = mapped_column(Text, default="")

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

class RecordingStep(Base):
    __tablename__ = "recording_steps"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    recording_id: Mapped[str] = mapped_column(String(36), ForeignKey("recordings.id"))
    order: Mapped[int] = mapped_column(Integer)
    action: Mapped[str] = mapped_column(String(30))
    selector: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    value: Mapped[str | None] = mapped_column(Text, nullable=True)
    url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    label: Mapped[str | None] = mapped_column(String(300), nullable=True)
    screenshot_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    recording: Mapped[Recording] = relationship(back_populates="steps")

class Run(Base):
    __tablename__ = "runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    target_type: Mapped[str] = mapped_column(String(20))
    target_id: Mapped[str] = mapped_column(String(36))
    status: Mapped[str] = mapped_column(String(20), default="queued")
    log: Mapped[list] = mapped_column(JSON, default=list)
    video_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

def init_db(): Base.metadata.create_all(engine)
VAR_PATTERN = re.compile(r"\{\{\s*([\w.\-]+)\s*\}\}")
def resolve_variables(db, project_id=None):
    merged = {}
    for v in db.query(Variable).filter_by(scope="global"): merged[v.name] = v.value
    if project_id:
        for v in db.query(Variable).filter_by(project_id=project_id): merged[v.name] = v.value
    return merged
def interpolate(text, variables):
    if text is None: return None
    return VAR_PATTERN.sub(lambda m: variables.get(m.group(1), m.group(0)), str(text))