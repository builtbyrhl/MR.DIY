from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    __table_args__ = (Index("ix_users_access_status", "access_status"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)  # telegram user id
    username: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    full_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    is_premium: Mapped[bool] = mapped_column(Boolean, default=False)
    premium_until: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    access_status: Mapped[Optional[str]] = mapped_column(
        String(16), default="pending", server_default="pending", nullable=True
    )
    access_requested_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    access_decided_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    access_decided_by: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    access_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    streak: Mapped[int] = mapped_column(Integer, default=0)
    last_active: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    progress: Mapped[list["Progress"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    bookmarks: Mapped[list["Bookmark"]] = relationship(back_populates="user", cascade="all, delete-orphan")


class AdminUser(Base):
    """A database-granted administrator.

    ``ADMIN_IDS`` remains the immutable owner/superadmin list. Admins granted
    from the bot are stored here so they can be added or removed without a
    deployment, while owners cannot be removed accidentally from chat.
    """

    __tablename__ = "admin_users"
    __table_args__ = (Index("ix_admin_users_granted_by", "granted_by"),)

    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    granted_by: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Subject(Base):
    __tablename__ = "subjects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(128))
    emoji: Mapped[str] = mapped_column(String(8), default="📘")
    order: Mapped[int] = mapped_column(Integer, default=0)

    lectures: Mapped[list["Lecture"]] = relationship(back_populates="subject", cascade="all, delete-orphan")
    topics: Mapped[list["Topic"]] = relationship(back_populates="subject", cascade="all, delete-orphan")


class Teacher(Base):
    __tablename__ = "teachers"
    __table_args__ = (
        UniqueConstraint(
            "subject_id", "normalized_name", name="uq_teacher_subject_normalized_name"
        ),
        Index("ix_teachers_subject", "subject_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    subject_id: Mapped[int] = mapped_column(ForeignKey("subjects.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(128))
    normalized_name: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class TeacherAlias(Base):
    """Explicit alternate spelling for a subject-scoped teacher."""

    __tablename__ = "teacher_aliases"
    __table_args__ = (
        UniqueConstraint(
            "subject_id",
            "normalized_name",
            name="uq_teacher_alias_subject_normalized_name",
        ),
        Index("ix_teacher_aliases_teacher", "teacher_id"),
        Index("ix_teacher_aliases_subject", "subject_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    teacher_id: Mapped[int] = mapped_column(
        ForeignKey("teachers.id", ondelete="CASCADE")
    )
    subject_id: Mapped[int] = mapped_column(
        ForeignKey("subjects.id", ondelete="CASCADE")
    )
    name: Mapped[str] = mapped_column(String(128))
    normalized_name: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Topic(Base):
    __tablename__ = "topics"
    __table_args__ = (
        UniqueConstraint(
            "subject_id", "normalized_name", name="uq_topic_subject_normalized_name"
        ),
        Index("ix_topics_subject", "subject_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    subject_id: Mapped[int] = mapped_column(
        ForeignKey("subjects.id", ondelete="CASCADE")
    )
    name: Mapped[str] = mapped_column(String(128))
    normalized_name: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    subject: Mapped["Subject"] = relationship(back_populates="topics")


upload_item_topics = Table(
    "upload_item_topics",
    Base.metadata,
    Column(
        "upload_item_id",
        ForeignKey("upload_items.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "topic_id",
        ForeignKey("topics.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Index("ix_upload_item_topics_topic", "topic_id"),
)

lecture_topics = Table(
    "lecture_topics",
    Base.metadata,
    Column(
        "lecture_id",
        ForeignKey("lectures.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "topic_id",
        ForeignKey("topics.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Index("ix_lecture_topics_topic", "topic_id"),
)


class Lecture(Base):
    __tablename__ = "lectures"
    __table_args__ = (
        Index(
            "uq_lectures_source_upload_item",
            "source_upload_item_id",
            unique=True,
        ),
        Index(
            "ix_lectures_subject_teacher_order",
            "subject_id",
            "teacher_id",
            "order",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    subject_id: Mapped[int] = mapped_column(ForeignKey("subjects.id", ondelete="CASCADE"))
    teacher_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("teachers.id", ondelete="SET NULL"), nullable=True
    )
    source_upload_item_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("upload_items.id", ondelete="SET NULL"), nullable=True
    )
    title: Mapped[str] = mapped_column(String(256))
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    tags: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)  # comma-separated
    channel_message_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # source channel msg
    duration_sec: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    is_premium: Mapped[bool] = mapped_column(Boolean, default=False)
    order: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    subject: Mapped["Subject"] = relationship(back_populates="lectures")


class Progress(Base):
    __tablename__ = "progress"
    __table_args__ = (UniqueConstraint("user_id", "lecture_id", name="uq_user_lecture"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    lecture_id: Mapped[int] = mapped_column(ForeignKey("lectures.id", ondelete="CASCADE"))
    watched: Mapped[bool] = mapped_column(Boolean, default=False)
    watched_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    user: Mapped["User"] = relationship(back_populates="progress")


class Bookmark(Base):
    __tablename__ = "bookmarks"
    __table_args__ = (UniqueConstraint("user_id", "lecture_id", name="uq_user_bookmark"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    lecture_id: Mapped[int] = mapped_column(ForeignKey("lectures.id", ondelete="CASCADE"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    user: Mapped["User"] = relationship(back_populates="bookmarks")


class StudentMediaMessage(Base):
    __tablename__ = "student_media_messages"
    __table_args__ = (
        UniqueConstraint(
            "chat_id", "message_id", name="uq_student_media_chat_message"
        ),
        Index("ix_student_media_pending_delete", "status", "delete_at"),
        Index("ix_student_media_user", "user_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE")
    )
    lecture_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("lectures.id", ondelete="SET NULL"), nullable=True
    )
    chat_id: Mapped[int] = mapped_column(BigInteger)
    message_id: Mapped[int] = mapped_column(BigInteger)
    sent_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    delete_at: Mapped[datetime] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(
        String(16), default="pending", server_default="pending"
    )
    claimed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    attempts: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0"
    )
    deleted_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    amount_inr: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32), default="pending")  # pending/paid/failed
    provider_order_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    provider_payment_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    paid_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class Course(Base):
    __tablename__ = "courses"
    __table_args__ = (UniqueConstraint("subject_id", "name", name="uq_course_subject_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    year: Mapped[str] = mapped_column(String(32))
    subject_id: Mapped[int] = mapped_column(ForeignKey("subjects.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(128))
    aliases: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # comma-separated
    destination_chat_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    topic_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    subject: Mapped["Subject"] = relationship()


class UploadBatch(Base):
    __tablename__ = "upload_batches"
    __table_args__ = (
        Index("ix_upload_batches_source_date", "source_chat_id", "batch_date"),
        Index(
            "uq_upload_batches_open_source_date",
            "source_chat_id",
            "batch_date",
            unique=True,
            sqlite_where=text("status = 'open'"),
            postgresql_where=text("status = 'open'"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_chat_id: Mapped[int] = mapped_column(BigInteger)
    batch_date: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(32), default="open")  # open/approved
    approved_by: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    approved_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    items: Mapped[list["UploadItem"]] = relationship(back_populates="batch", cascade="all, delete-orphan")


class UploadItem(Base):
    __tablename__ = "upload_items"
    __table_args__ = (
        UniqueConstraint("source_chat_id", "source_message_id", name="uq_upload_source_message"),
        Index("ix_upload_items_status", "status"),
        Index("ix_upload_items_batch", "batch_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("upload_batches.id", ondelete="CASCADE"))
    source_chat_id: Mapped[int] = mapped_column(BigInteger)
    source_message_id: Mapped[int] = mapped_column(BigInteger)
    forward_from_chat_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    forward_from_message_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    media_type: Mapped[str] = mapped_column(String(16))  # video/document
    file_name: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    file_id: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    file_unique_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    raw_caption: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    original_caption: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    title: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    lecture_number: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    course_id: Mapped[Optional[int]] = mapped_column(ForeignKey("courses.id", ondelete="SET NULL"), nullable=True)
    teacher_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("teachers.id", ondelete="SET NULL"), nullable=True
    )
    teacher_source: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    teacher_review_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    topic_source: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    topic_review_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="received")
    publication_status: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    published_message_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    review_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    duplicate_flag: Mapped[bool] = mapped_column(Boolean, default=False)
    duplicate_of_item_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("upload_items.id", ondelete="SET NULL"), nullable=True
    )
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    batch: Mapped["UploadBatch"] = relationship(back_populates="items")


class SystemSetting(Base):
    __tablename__ = "system_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
