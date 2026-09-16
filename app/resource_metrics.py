import os
from dataclasses import dataclass
from typing import Optional

import psutil


@dataclass(frozen=True)
class ResourceSnapshot:
    parent_rss_bytes: int
    tree_rss_bytes: int
    process_cpu_percent: float
    host_cpu_percent: float


def collect_resource_snapshot(proc: Optional[psutil.Process] = None) -> ResourceSnapshot:
    active = proc or psutil.Process(os.getpid())
    parent_rss = active.memory_info().rss
    tree_rss = parent_rss
    try:
        children = active.children(recursive=True)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        children = []
    for child in children:
        try:
            tree_rss += child.memory_info().rss
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    try:
        process_cpu = active.cpu_percent(interval=None)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        process_cpu = 0.0
    return ResourceSnapshot(
        parent_rss_bytes=parent_rss,
        tree_rss_bytes=tree_rss,
        process_cpu_percent=process_cpu,
        host_cpu_percent=psutil.cpu_percent(interval=None),
    )
