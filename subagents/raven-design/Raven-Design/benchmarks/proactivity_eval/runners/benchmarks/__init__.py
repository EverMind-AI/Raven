"""Per-benchmark configuration yamls.

Each subdirectory contains ``<name>.yaml`` with benchmark-specific paths
and knobs. Adapters load them via ``_common.get_benchmark_config("<name>")``.

Users override fields without touching tracked files by dropping a
``<name>.local.yaml`` next to the default.
"""
