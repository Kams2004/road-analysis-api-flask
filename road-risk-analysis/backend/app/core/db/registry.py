"""
Central registry of the SQLAlchemy models.

Every table lives in the `models/` package of its functional module
(app/modules/mNN_*/models/). `Base.metadata.create_all` and the mapper
configuration only know the classes whose module has been imported, so this
is the one place that imports them all: `init_db()` (API start-up) and the
demo seeders call `import_all_models()` before touching the schema.

Adding a model: create app/modules/<module>/models/<name>.py (subclass
`app.core.db.session.Base`) and add its import below.
"""


def import_all_models() -> None:
    """Import every model module so all tables / mappers are registered on Base."""
    from app.modules.m01_security.models import auth_token, user  # noqa: F401
    from app.modules.m06_hazards.models import (  # noqa: F401
        cluster_area, cluster_config, cluster_zone, dataset, detection, detection_review, hazard,
        hazard_lifecycle, rejection_reason, training_sample, validation_label,
    )
    from app.modules.m07_ingestion.models import (  # noqa: F401
        cloud_connection, cloud_import, job, job_event, job_track_point, stream_source,
    )
    from app.modules.m08_historical.models import historical_event  # noqa: F401
    from app.modules.m10_collision.models import vehicle, vehicle_road_state  # noqa: F401
    from app.modules.m11_community.models import signalement, signalement_vote  # noqa: F401
    from app.modules.m12_integration.models import api_client  # noqa: F401
    from app.modules.m13_admin.models import audit_log, system_setting  # noqa: F401
