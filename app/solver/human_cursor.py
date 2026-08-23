import asyncio
import math
import random
import logging
from typing import Tuple, List, Optional
from playwright.async_api import Page

import weakref

logger = logging.getLogger("solverr.human_cursor")

# Per-page cursor position tracking to isolate concurrent browser tasks
_page_cursors: "weakref.WeakKeyDictionary[Page, List[float]]" = weakref.WeakKeyDictionary()

def _get_page_cursor(page: Page) -> List[float]:
    try:
        if page not in _page_cursors:
            _page_cursors[page] = [float(random.randint(150, 400)), float(random.randint(150, 400))]
        return _page_cursors[page]
    except Exception:
        return [float(random.randint(150, 400)), float(random.randint(150, 400))]

def _bezier_point(p0: Tuple[float, float], p1: Tuple[float, float], p2: Tuple[float, float], p3: Tuple[float, float], t: float) -> Tuple[float, float]:
    """Calculate point on cubic Bézier curve at parameter t in [0, 1]."""
    u = 1 - t
    tt = t * t
    uu = u * u
    uuu = uu * u
    ttt = tt * t

    x = uuu * p0[0] + 3 * uu * t * p1[0] + 3 * u * tt * p2[0] + ttt * p3[0]
    y = uuu * p0[1] + 3 * uu * t * p1[1] + 3 * u * tt * p2[1] + ttt * p3[1]
    return (x, y)

def generate_bezier_path(start: Tuple[float, float], end: Tuple[float, float], steps: int = 25) -> List[Tuple[float, float]]:
    """Generate realistic human-like mouse trajectory with randomized control points and jitter."""
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    distance = math.hypot(dx, dy)

    if distance < 5 or steps <= 1:
        return [end]

    deviation = min(max(distance * 0.25, 20.0), 120.0)
    
    cp1_x = start[0] + dx * 0.25 + random.uniform(-deviation, deviation)
    cp1_y = start[1] + dy * 0.25 + random.uniform(-deviation, deviation)

    cp2_x = start[0] + dx * 0.75 + random.uniform(-deviation * 0.5, deviation * 0.5)
    cp2_y = start[1] + dy * 0.75 + random.uniform(-deviation * 0.5, deviation * 0.5)

    path = []
    for i in range(1, steps + 1):
        t = i / steps
        smooth_t = 3 * (t ** 2) - 2 * (t ** 3)
        pt = _bezier_point(start, (cp1_x, cp1_y), (cp2_x, cp2_y), end, smooth_t)
        if i < steps - 2:
            jitter_x = random.uniform(-0.6, 0.6)
            jitter_y = random.uniform(-0.6, 0.6)
            pt = (max(0.0, min(1920.0, pt[0] + jitter_x)), max(0.0, min(1080.0, pt[1] + jitter_y)))
        else:
            pt = (max(0.0, min(1920.0, pt[0])), max(0.0, min(1080.0, pt[1])))
        path.append(pt)

    path.append(end)
    return path

async def human_mouse_move(page: Page, target_x: float, target_y: float, start_x: Optional[float] = None, start_y: Optional[float] = None):
    """Smoothly moves mouse along a Bézier curve to target coordinates with natural pauses."""
    cursor = _get_page_cursor(page)
    sx = start_x if start_x is not None else cursor[0]
    sy = start_y if start_y is not None else cursor[1]

    tx = max(0.0, min(1920.0, target_x))
    ty = max(0.0, min(1080.0, target_y))

    try:
        steps = random.randint(16, 26)
        path = generate_bezier_path((sx, sy), (tx, ty), steps=steps)
        for pt in path:
            await page.mouse.move(pt[0], pt[1])
            await asyncio.sleep(random.uniform(0.003, 0.010))
        cursor[0] = tx
        cursor[1] = ty
    except Exception as e:
        logger.debug(f"[HumanCursor] Mouse move notice: {e}")
        try:
            await page.mouse.move(tx, ty)
            cursor[0] = tx
            cursor[1] = ty
        except Exception:
            pass

async def human_click(page: Page, target_x: float, target_y: float, start_x: Optional[float] = None, start_y: Optional[float] = None):
    """Executes a human-like approach, hover, mouse-down, pause, and mouse-up click."""
    target_jitter_x = target_x + random.uniform(-1.0, 1.0)
    target_jitter_y = target_y + random.uniform(-1.0, 1.0)

    await human_mouse_move(page, target_jitter_x, target_jitter_y, start_x, start_y)
    await asyncio.sleep(random.uniform(0.05, 0.12))
    await page.mouse.down()
    await asyncio.sleep(random.uniform(0.07, 0.15))
    await page.mouse.up()
    await asyncio.sleep(random.uniform(0.04, 0.10))
