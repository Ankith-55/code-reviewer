import datetime
from sqlalchemy import Column, String, Integer, DateTime, ForeignKey, Text, Enum as SQLEnum
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


class Repository(Base):
    __tablename__ = "repositories"

    id = Column(String(64), primary_key=True, index=True)
    zip_path = Column(String(1024), nullable=True)
    status = Column(String(32), default="queued", nullable=False, index=True)
    total_files = Column(Integer, default=0, nullable=False)
    completed_files = Column(Integer, default=0, nullable=False)
    failed_files = Column(Integer, default=0, nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow, nullable=False)
    completed_at = Column(DateTime, nullable=True)

    # Relationships
    review_jobs = relationship(
        "ReviewJob",
        back_populates="repository",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class ReviewJob(Base):
    __tablename__ = "review_jobs"

    id = Column(String(64), primary_key=True, index=True)
    repository_id = Column(
        String(64),
        ForeignKey("repositories.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    file_path = Column(String(1024), nullable=False)
    language = Column(String(64), default="unknown", nullable=False)
    status = Column(String(32), default="queued", nullable=False, index=True)
    attempts = Column(Integer, default=0, nullable=False)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow, nullable=False)
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)

    # Relationships
    repository = relationship("Repository", back_populates="review_jobs")
    findings = relationship(
        "Finding",
        back_populates="review_job",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class Finding(Base):
    __tablename__ = "findings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    review_job_id = Column(
        String(64),
        ForeignKey("review_jobs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    file_path = Column(String(1024), nullable=False)
    line = Column(Integer, nullable=True)
    severity = Column(String(32), nullable=False)  # CRITICAL, HIGH, MEDIUM, LOW
    category = Column(String(64), nullable=False)  # bug, security, performance, etc.
    description = Column(Text, nullable=False)
    recommendation = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow, nullable=False)

    # Relationships
    review_job = relationship("ReviewJob", back_populates="findings")
