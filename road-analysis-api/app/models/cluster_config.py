from sqlalchemy import Float, Integer
from sqlalchemy.orm import Mapped, mapped_column
from app.db.session import Base


class ClusterConfig(Base):
    __tablename__ = "cluster_config"

    id:       Mapped[int]   = mapped_column(Integer, primary_key=True, default=1)
    radius_m: Mapped[float] = mapped_column(Float, default=50.0)
