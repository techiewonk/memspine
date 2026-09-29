"""Dataset adapters. Real datasets are never downloaded by the harness."""

from .locomo import LoCoMoDataset
from .longmemeval import LongMemEvalDataset
from .synthetic import SyntheticDataset

__all__ = ["LoCoMoDataset", "LongMemEvalDataset", "SyntheticDataset"]
