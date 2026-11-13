"""Public surface of the pipeline sub-package."""
from .manager import PipelineManager
from .parser import observations_from_frame

__all__ = ["PipelineManager", "observations_from_frame"]
