from .artifacts import router as artifact_router
from .auth import router as auth_router
from .datasets import router as dataset_router
from .experiments import router as manager_router
from .health import router as health_router
from .job_observability import router as job_observability_router
from .maintenance import router as maintenance_router
from .model_registry import router as model_registry_router
from .test_sets import router as test_set_router
from .users import router as user_router
from .workers import router as worker_router

__all__ = [
    "artifact_router",
    "auth_router",
    "dataset_router",
    "health_router",
    "job_observability_router",
    "maintenance_router",
    "manager_router",
    "model_registry_router",
    "test_set_router",
    "user_router",
    "worker_router",
]
