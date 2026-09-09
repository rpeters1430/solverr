"""Humanized cursor geometry.

`generate_bezier_path` is called for every challenge-widget click (and for
each retry inside the Tier 3 solve loop), producing the 16-26 point
trajectory the mouse is then driven along.
"""

from app.solver.human_cursor import _bezier_point, generate_bezier_path


def test_generate_bezier_path(benchmark, seeded_random):
    path = benchmark(generate_bezier_path, (180.0, 240.0), (960.0, 540.0), 25)
    assert len(path) == 26


def test_generate_bezier_path_long_travel(benchmark, seeded_random):
    """Longest realistic travel across a 1920x1080 viewport."""
    path = benchmark(generate_bezier_path, (5.0, 5.0), (1915.0, 1075.0), 26)
    assert len(path) == 27


def test_bezier_point(benchmark):
    x, y = benchmark(_bezier_point, (0.0, 0.0), (120.0, 300.0), (700.0, 100.0), (960.0, 540.0), 0.5)
    assert x > 0 and y > 0
