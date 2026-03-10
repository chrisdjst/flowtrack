from datetime import datetime

from sqlalchemy import Boolean, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from flowtrack.models.base import Base


class Config(Base):
    __tablename__ = "config"

    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    encrypted: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())
