import uuid, re
from datetime import datetime
from sqlalchemy import (create_engine, String, Text, Integer, Boolean, DateTime, ForeignKey, JSON, func)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker
from app.config import DATABASE_URL

# Resilient engine creation with automatic SQLite fallback if PostgreSQL driver is missing
try:
    engine = create_engine(
        DATABASE_URL, 
        connect_args={"check_same_thread": False} if "sqlite" in DATABASE_URL else {},
        pool_pre_ping=True,
        pool_recycle=300
    )
    # Test connection
    with engine.connect() as conn:
        pass
except Exception as e:
    print(f"⚠️ Database connection warning ({e}), falling back to local SQLite...")
    from app.config import ARTIFACTS
    DATABASE_URL = f"sqlite:///{ARTIFACTS}/testforge.db"
    engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})

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
    recording_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("recordings.id"), nullable=True)
    name: Mapped[str] = mapped_column(String(100))
    value: Mapped[str] = mapped_column(Text, default="")
    is_secret: Mapped[bool] = mapped_column(Boolean, default=False)

class Recording(Base):
    __tablename__ = "recordings"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    project_id: Mapped[str] = mapped_column(String(36), ForeignKey("projects.id"))
    parent_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("recordings.id"), nullable=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    start_url: Mapped[str] = mapped_column(String(500), default="")
    shared: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(20), default="draft")
    video_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    project: Mapped[Project] = relationship(back_populates="recordings")
    parent: Mapped["Recording"] = relationship("Recording", remote_side=[id], back_populates="children", foreign_keys=[parent_id])
    children: Mapped[list["Recording"]] = relationship("Recording", back_populates="parent", cascade="all, delete-orphan", foreign_keys=[parent_id])
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
    ref_recording_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    variable_overrides: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    recording: Mapped[Recording] = relationship(back_populates="steps")

class Scenario(Base):
    __tablename__ = "scenarios"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    project_id: Mapped[str] = mapped_column(String(36), ForeignKey("projects.id"))
    title: Mapped[str] = mapped_column(String(300))
    source_text: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="draft")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    steps: Mapped[list["ScenarioStep"]] = relationship(order_by="ScenarioStep.order", cascade="all, delete-orphan")

class ScenarioStep(Base):
    __tablename__ = "scenario_steps"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    scenario_id: Mapped[str] = mapped_column(String(36), ForeignKey("scenarios.id"))
    order: Mapped[int] = mapped_column(Integer)
    action: Mapped[str] = mapped_column(String(30))
    selector: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    value: Mapped[str | None] = mapped_column(Text, nullable=True)
    expected_result: Mapped[str | None] = mapped_column(Text, nullable=True)

class Run(Base):
    __tablename__ = "runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    target_type: Mapped[str] = mapped_column(String(20))
    target_id: Mapped[str] = mapped_column(String(36))
    mode: Mapped[str] = mapped_column(String(20), default="script")
    status: Mapped[str] = mapped_column(String(20), default="queued")
    log: Mapped[list] = mapped_column(JSON, default=list)
    video_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    agent_transcript: Mapped[list | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

def init_db(): Base.metadata.create_all(engine)