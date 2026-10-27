#!/usr/bin/env python3
"""
humanizer.py - Biometric Compatibility Layer & Human Input Synthesis Middleware

A modular input humanization middleware for Playwright and Camoufox (one Humanizer
per page; each worker process has its own). Transforms synthetic, deterministic browser automation inputs into
statistically indistinguishable human behavioral biometrics.

Features:
1. Spatial Mouse Trajectory Module:
   - WindMouse physics simulation (gravitational pull, momentum inertia, lateral wind drift).
   - Fitts's Law-style velocity easing: fast ramp-up over the first 35%, then power-curve deceleration.
   - Per-point Gaussian micro-jitter (sigma 0.35px) imitating hand tremor.
   - Probabilistic overshooting (15-25% chance on moves over 90px) with corrective submovements.
   - Smooth multi-point trajectory dispatching every 7-12ms (~85-140Hz).
2. Temporal Distribution Module:
   - Right-skewed reaction time distributions modeled via Ex-Gaussian (normal + exponential).
   - Hard physiological neural clamp (floor >= 110-140ms for cognitive reaction tasks).
   - Split key & click contact durations (normally distributed contact 65ms-135ms).
   - Session fatigue: latency and variance ramp up linearly over the session (up to 1.25x).
3. Keystroke Dynamics Module:
   - QWERTY spatial distance modeling for variable Inter-Keystroke Intervals (IKIs).
   - Stochastic typo simulation (adjacent QWERTY key hit, realization lag, Backspace correction).
   - Discrete down/up key events with natural contact duration and hesitation delays.
4. Spatial Entropy & 2D Gaussian Targeting:
   - Bivariate truncated Gaussian coordinate sampling (sigma = dimension / 6, centered at w/2, h/2).
   - Safe interactive padding clamp to prevent border misses.
   - Consecutive click continuity: 1-3px micro-jitter inertia for repeat clicks on the same element.
5. Macro-Behavior & Stuck State Watchdog:
   - Ambient idle cursor drift during waiting states (lobby, Turnstile evaluation, respawn delays).
   - Watchdog to detect and cleanly resolve infinite lobby/transition deadlocks.
"""

import logging
import math
import random
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger("PowerlineBot.Humanizer")

DEFAULT_WINDOW_SIZE: Tuple[int, int] = (720, 480)

# ============================================================================
# Keyboard Geometry & Typo Maps
# ============================================================================

# QWERTY Key Coordinates (column, row) for flight-time calculations
QWERTY_COORDS: Dict[str, Tuple[float, float]] = {
    # Number Row
    '1': (0.0, 0.0), '2': (1.0, 0.0), '3': (2.0, 0.0), '4': (3.0, 0.0), '5': (4.0, 0.0),
    '6': (5.0, 0.0), '7': (6.0, 0.0), '8': (7.0, 0.0), '9': (8.0, 0.0), '0': (9.0, 0.0),
    '-': (10.0, 0.0), '=': (11.0, 0.0),
    # Top Row
    'q': (0.5, 1.0), 'w': (1.5, 1.0), 'e': (2.5, 1.0), 'r': (3.5, 1.0), 't': (4.5, 1.0),
    'y': (5.5, 1.0), 'u': (6.5, 1.0), 'i': (7.5, 1.0), 'o': (8.5, 1.0), 'p': (9.5, 1.0),
    '[': (10.5, 1.0), ']': (11.5, 1.0),
    # Home Row
    'a': (0.8, 2.0), 's': (1.8, 2.0), 'd': (2.8, 2.0), 'f': (3.8, 2.0), 'g': (4.8, 2.0),
    'h': (5.8, 2.0), 'j': (6.8, 2.0), 'k': (7.8, 2.0), 'l': (8.8, 2.0), ';': (9.8, 2.0),
    # Bottom Row
    'z': (1.2, 3.0), 'x': (2.2, 3.0), 'c': (3.2, 3.0), 'v': (4.2, 3.0), 'b': (5.2, 3.0),
    'n': (6.2, 3.0), 'm': (7.2, 3.0), ',': (8.2, 3.0), '.': (9.2, 3.0), '/': (10.2, 3.0),
    # Space & Modifiers
    ' ': (4.5, 4.0), '_': (10.0, 0.0),
}

# Adjacent QWERTY neighbors for typo injection
ADJACENT_KEYS: Dict[str, List[str]] = {
    'q': ['w', 'a', 's', '1', '2'],
    'w': ['q', 'e', 'a', 's', 'd', '2', '3'],
    'e': ['w', 'r', 's', 'd', 'f', '3', '4'],
    'r': ['e', 't', 'd', 'f', 'g', '4', '5'],
    't': ['r', 'y', 'f', 'g', 'h', '5', '6'],
    'y': ['t', 'u', 'g', 'h', 'j', '6', '7'],
    'u': ['y', 'i', 'h', 'j', 'k', '7', '8'],
    'i': ['u', 'o', 'j', 'k', 'l', '8', '9'],
    'o': ['i', 'p', 'k', 'l', '9', '0'],
    'p': ['o', 'l', '0', '-'],
    'a': ['q', 'w', 's', 'z'],
    's': ['a', 'w', 'e', 'd', 'x', 'z'],
    'd': ['s', 'e', 'r', 'f', 'c', 'x'],
    'f': ['d', 'r', 't', 'g', 'v', 'c'],
    'g': ['f', 't', 'y', 'h', 'b', 'v'],
    'h': ['g', 'y', 'u', 'j', 'n', 'b'],
    'j': ['h', 'u', 'i', 'k', 'm', 'n'],
    'k': ['j', 'i', 'o', 'l', 'm'],
    'l': ['k', 'o', 'p'],
    'z': ['a', 's', 'x'],
    'x': ['z', 's', 'd', 'c'],
    'c': ['x', 'd', 'f', 'v'],
    'v': ['c', 'f', 'g', 'b'],
    'b': ['v', 'g', 'h', 'n'],
    'n': ['b', 'h', 'j', 'm'],
    'm': ['n', 'j', 'k'],
    '1': ['2', 'q'],
    '2': ['1', '3', 'q', 'w'],
    '3': ['2', '4', 'w', 'e'],
    '4': ['3', '5', 'e', 'r'],
    '5': ['4', '6', 'r', 't'],
    '6': ['5', '7', 't', 'y'],
    '7': ['6', '8', 'y', 'u'],
    '8': ['7', '9', 'u', 'i'],
    '9': ['8', '0', 'i', 'o'],
    '0': ['9', 'p', 'o', '-'],
    '_': ['-', 'p', '0'],
}


# ============================================================================
# 1. Temporal Distribution Module
# ============================================================================

def sample_ex_gaussian(
    mean: float,
    sigma: float,
    tau: float,
    min_floor: float = 0.11,
    fatigue_scale: float = 1.0,
) -> float:
    """
    Sample a reaction delay from an Exponentially Modified Gaussian (Ex-Gaussian) distribution:
    X ~ Normal(mean, sigma) + Exponential(tau).

    Parameters:
    - mean: baseline Gaussian location (seconds).
    - sigma: Gaussian scale / standard deviation (seconds).
    - tau: Exponential relaxation rate / right tail skew (seconds).
    - min_floor: strict physiological neural clamp (seconds).
    - fatigue_scale: multiplier representing round / circadian cognitive fatigue.
    """
    eff_mean = mean * fatigue_scale
    eff_sigma = sigma * math.sqrt(fatigue_scale)
    eff_tau = tau * fatigue_scale

    val = float(np.random.normal(eff_mean, max(1e-4, eff_sigma)) + np.random.exponential(max(1e-4, eff_tau)))
    return max(min_floor, val)


def sample_contact_duration() -> float:
    """
    Sample physical contact duration (down-to-up) for keypress or mouse click.
    Human contact times typically range between 65ms and 135ms.
    """
    return float(np.clip(np.random.normal(0.088, 0.016), 0.065, 0.135))


def human_sleep(
    mean: float = 0.25,
    sigma: float = 0.04,
    tau: float = 0.06,
    min_floor: float = 0.11,
    fatigue_scale: float = 1.0,
) -> float:
    """
    Sleep for an Ex-Gaussian distributed duration with neural floor clamping.
    Returns the exact slept duration in seconds.
    """
    delay = sample_ex_gaussian(mean, sigma, tau, min_floor=min_floor, fatigue_scale=fatigue_scale)
    time.sleep(delay)
    return delay


# ============================================================================
# 2. Spatial Mouse Trajectory Module (WindMouse + Fitts's Law + Tremor)
# ============================================================================

def wind_mouse_trajectory(
    start_x: float,
    start_y: float,
    dest_x: float,
    dest_y: float,
    gravity: float = 9.0,
    wind: float = 3.5,
    max_step: float = 16.0,
    target_area: float = 14.0,
) -> List[Tuple[float, float]]:
    """
    Generate a bio-mechanically realistic cursor trajectory using the WindMouse algorithm,
    augmented with Fitts's Law-style velocity easing and per-point micro-jitter.

    Parameters:
    - start_x, start_y: origin coordinates.
    - dest_x, dest_y: target destination coordinates.
    - gravity: gravitational pull towards target.
    - wind: magnitude of random atmospheric lateral drift.
    - max_step: peak kinematic step threshold per cycle.
    - target_area: proximity radius where deceleration and gravitational damping engage.
    """
    points = [(float(start_x), float(start_y))]
    cx, cy = float(start_x), float(start_y)
    vx, vy = 0.0, 0.0
    wx, wy = 0.0, 0.0

    total_dist = max(math.hypot(dest_x - start_x, dest_y - start_y), 1.0)
    steps = 0
    max_iterations = 1000

    while steps < max_iterations:
        dist = math.hypot(dest_x - cx, dest_y - cy)
        if dist < 1.8:
            break

        w_mag = min(wind, dist)
        if dist >= target_area:
            wx = wx / math.sqrt(3.0) + (random.uniform(-1.0, 1.0) * w_mag) / math.sqrt(5.0)
            wy = wy / math.sqrt(3.0) + (random.uniform(-1.0, 1.0) * w_mag) / math.sqrt(5.0)
        else:
            wx /= math.sqrt(2.0)
            wy /= math.sqrt(2.0)
            if max_step < 3.0:
                max_step = random.uniform(2.0, 4.0)
            else:
                max_step /= math.sqrt(5.0)

        vx += wx + gravity * (dest_x - cx) / dist
        vy += wy + gravity * (dest_y - cy) / dist
        v_mag = math.hypot(vx, vy)

        # Fitts's Law asymmetric velocity profile:
        # 1. Rapid ballistic burst acceleration during first ~35% of the movement.
        # 2. Logarithmic deceleration and micro-correction during the remaining ~65%.
        progress = float(np.clip(1.0 - (dist / total_dist), 0.0, 1.0))
        if progress < 0.35:
            speed_mult = 0.45 + 1.55 * (progress / 0.35)
        else:
            rem = max(0.0, 1.0 - progress)
            speed_mult = 0.35 + 1.65 * (rem ** 0.8)

        cur_step_limit = max_step * max(0.3, speed_mult)
        if v_mag > cur_step_limit:
            clip = (cur_step_limit * 0.65) + random.uniform(0.0, cur_step_limit * 0.35)
            vx = (vx / v_mag) * clip
            vy = (vy / v_mag) * clip

        cx += vx
        cy += vy

        # Per-point Gaussian micro-jitter imitating hand tremor
        tremor_x = float(np.random.normal(0.0, 0.35))
        tremor_y = float(np.random.normal(0.0, 0.35))

        points.append((cx + tremor_x, cy + tremor_y))
        steps += 1

    points.append((float(dest_x), float(dest_y)))
    return points


def calculate_overshoot_point(
    start_x: float,
    start_y: float,
    dest_x: float,
    dest_y: float,
) -> Tuple[float, float]:
    """
    Calculate an intermediate target point that slightly overshoots the destination along
    the movement vector, modeling human ballistic overreach before fine corrective adjustment.
    """
    dx = dest_x - start_x
    dy = dest_y - start_y
    dist = math.hypot(dx, dy)
    if dist < 1.0:
        return dest_x, dest_y

    ux, uy = dx / dist, dy / dist
    px, py = -uy, ux  # Perpendicular unit vector

    # Overshoot magnitude: 6-15px along vector, with slight lateral deviation (-5 to +5px)
    d_over = random.uniform(6.0, 15.0)
    d_lat = random.uniform(-5.0, 5.0)

    ox = dest_x + ux * d_over + px * d_lat
    oy = dest_y + uy * d_over + py * d_lat
    return ox, oy


def human_move(
    page: Any,
    target_x: float,
    target_y: float,
    start_pos: Tuple[float, float],
    enable_overshoot: bool = True,
    fatigue_scale: float = 1.0,
) -> Tuple[float, float]:
    """
    Move the mouse cursor smoothly from start_pos to (target_x, target_y) using
    WindMouse kinematics, Fitts's Law easing, and probabilistic overshoot corrections.
    """
    start_x, start_y = start_pos

    total_dist = math.hypot(target_x - start_x, target_y - start_y)

    # 15-25% chance of ballistic overshoot on movements greater than 90 pixels
    should_overshoot = enable_overshoot and (total_dist > 90.0) and (random.random() < random.uniform(0.15, 0.25))

    if should_overshoot:
        ox, oy = calculate_overshoot_point(start_x, start_y, target_x, target_y)
        primary_path = wind_mouse_trajectory(start_x, start_y, ox, oy, gravity=8.5, wind=3.5, max_step=17.0)

        # Dispatch primary trajectory to overshoot point
        for pt in primary_path:
            page.mouse.move(pt[0], pt[1])
            # USB polling delay ~85-140Hz (7-12ms)
            time.sleep(random.uniform(0.007, 0.012))

        # Cognitive correction pause: visual feedback registration latency (35-75ms)
        time.sleep(sample_ex_gaussian(mean=0.045, sigma=0.01, tau=0.02, min_floor=0.035, fatigue_scale=fatigue_scale))

        # Corrective submovement from overshoot to true destination
        correction_path = wind_mouse_trajectory(ox, oy, target_x, target_y, gravity=11.0, wind=1.5, max_step=9.0, target_area=6.0)
        for pt in correction_path:
            page.mouse.move(pt[0], pt[1])
            time.sleep(random.uniform(0.007, 0.012))
    else:
        path = wind_mouse_trajectory(start_x, start_y, target_x, target_y, gravity=9.0, wind=3.0, max_step=16.0)
        for pt in path:
            page.mouse.move(pt[0], pt[1])
            time.sleep(random.uniform(0.007, 0.012))

    return target_x, target_y


# ============================================================================
# 3. Spatial Entropy & 2D Gaussian Targeting
# ============================================================================

def sample_bivariate_gaussian_box(
    box: Dict[str, float],
    pad_ratio: float = 0.15,
) -> Tuple[float, float]:
    """
    Sample target click coordinates from a 2D truncated normal distribution centered
    at the geometric midpoint of an element's bounding box.
    Standard deviations are set to sigma_x = width / 6, sigma_y = height / 6.
    Coordinates are clamped safely inside interactive bounds by pad_ratio.
    """
    w = box["width"]
    h = box["height"]
    cx = box["x"] + w / 2.0
    cy = box["y"] + h / 2.0

    sigma_x = max(1.0, w / 6.0)
    sigma_y = max(1.0, h / 6.0)

    # Sample bivariate Gaussian
    raw_x = float(np.random.normal(cx, sigma_x))
    raw_y = float(np.random.normal(cy, sigma_y))

    # Clamp safely inside interactive boundaries
    min_x = box["x"] + w * pad_ratio
    max_x = box["x"] + w * (1.0 - pad_ratio)
    min_y = box["y"] + h * pad_ratio
    max_y = box["y"] + h * (1.0 - pad_ratio)

    x = float(np.clip(raw_x, min_x, max_x))
    y = float(np.clip(raw_y, min_y, max_y))
    return x, y


def is_same_bounding_box(b1: Optional[Dict[str, float]], b2: Optional[Dict[str, float]], tolerance: float = 4.0) -> bool:
    """Determine whether two bounding boxes represent the same UI element region."""
    if not b1 or not b2:
        return False
    return (
        abs(b1["x"] - b2["x"]) <= tolerance
        and abs(b1["y"] - b2["y"]) <= tolerance
        and abs(b1["width"] - b2["width"]) <= tolerance
        and abs(b1["height"] - b2["height"]) <= tolerance
    )


# ============================================================================
# 4. Keystroke Dynamics Module
# ============================================================================

def key_spatial_distance(c1: str, c2: str) -> float:
    """Calculate Euclidean distance between two keys on the standard QWERTY layout."""
    p1 = QWERTY_COORDS.get(c1.lower(), (4.5, 2.0))
    p2 = QWERTY_COORDS.get(c2.lower(), (4.5, 2.0))
    return math.hypot(p2[0] - p1[0], p2[1] - p1[1])


def human_type_string(
    page: Any,
    text: str,
    typo_prob: float = 0.04,
    fatigue_scale: float = 1.0,
) -> None:
    """
    Type a string into the focused element simulating human motor biometrics:
    - Variable Inter-Keystroke Intervals (IKIs) based on QWERTY finger flight distance.
    - Discrete key.down() and key.up() separated by physical contact hold durations.
    - Low-probability typo injection: hits an adjacent QWERTY key, realizes error,
      pauses, presses Backspace, hesitates, and enters the correct character.
    """
    for i, char in enumerate(text):
        prev_char = text[i - 1] if i > 0 else ' '
        dist = key_spatial_distance(prev_char, char)

        # Modulate Inter-Keystroke Interval (flight time) by physical distance
        base_mean = 0.075 + 0.016 * min(dist, 5.0)
        base_tau = 0.030 + 0.008 * min(dist, 5.0)
        iki = sample_ex_gaussian(mean=base_mean, sigma=0.018, tau=base_tau, min_floor=0.050, fatigue_scale=fatigue_scale)
        time.sleep(iki)

        # Stochastic typo simulation
        lower_char = char.lower()
        if random.random() < typo_prob and lower_char in ADJACENT_KEYS:
            typo_neighbor = random.choice(ADJACENT_KEYS[lower_char])
            typo_char = typo_neighbor.upper() if char.isupper() else typo_neighbor

            # 1. Type wrong key
            page.keyboard.down(typo_char)
            time.sleep(sample_contact_duration())
            page.keyboard.up(typo_char)

            # 2. Cognitive realization latency (human notices wrong character on screen)
            human_sleep(mean=0.20, sigma=0.03, tau=0.06, min_floor=0.14, fatigue_scale=fatigue_scale)

            # 3. Correct with Backspace
            page.keyboard.down("Backspace")
            time.sleep(sample_contact_duration())
            page.keyboard.up("Backspace")

            # 4. Post-correction hesitation
            human_sleep(mean=0.12, sigma=0.02, tau=0.04, min_floor=0.08, fatigue_scale=fatigue_scale)

        # Type the intended character
        page.keyboard.down(char)
        time.sleep(sample_contact_duration())
        page.keyboard.up(char)


def human_key_tap(
    page: Any,
    key: str,
    hesitation: bool = False,
    fatigue_scale: float = 1.0,
) -> None:
    """
    Perform a biometric keypress using distinct down() and up() events
    separated by realistic contact duration, with optional pre-press cognitive hesitation.
    """
    if hesitation:
        # Cognitive decision hesitation prior to triggering directional turn
        time.sleep(sample_ex_gaussian(mean=0.08, sigma=0.02, tau=0.03, min_floor=0.055, fatigue_scale=fatigue_scale))

    page.keyboard.down(key)
    time.sleep(sample_contact_duration())
    page.keyboard.up(key)


# ============================================================================
# 5. Macro-Behavior & Ambient Idle Drift
# ============================================================================

def generate_idle_drift_points(
    start_x: float,
    start_y: float,
    max_offset: float = 18.0,
    bounds: Tuple[int, int] = DEFAULT_WINDOW_SIZE,
) -> List[Tuple[float, float]]:
    """Generate a subtle, curved ambient mouse wandering path to simulate resting hand tremor."""
    dx = float(np.random.normal(0.0, max_offset / 2.5))
    dy = float(np.random.normal(0.0, max_offset / 2.5))
    tx = float(np.clip(start_x + dx, 25.0, bounds[0] - 25.0))
    ty = float(np.clip(start_y + dy, 25.0, bounds[1] - 25.0))

    dist = math.hypot(tx - start_x, ty - start_y)
    num_steps = max(6, int(dist * 2.2))
    t = np.linspace(0.0, 1.0, num_steps)

    perp_x = -(ty - start_y) / (dist + 1e-6)
    perp_y = (tx - start_x) / (dist + 1e-6)
    curve_amp = random.uniform(-2.0, 2.0)

    pts = []
    for s in t:
        bump = math.sin(s * math.pi) * curve_amp
        px = start_x + s * (tx - start_x) + perp_x * bump + float(np.random.normal(0.0, 0.25))
        py = start_y + s * (ty - start_y) + perp_y * bump + float(np.random.normal(0.0, 0.25))
        pts.append((px, py))
    pts.append((tx, ty))
    return pts


# ============================================================================
# 6. Biometric Compatibility Middleware Class (Humanizer)
# ============================================================================

class Humanizer:
    """
    Session-bound biometric compatibility middleware for Playwright page interactions.
    Meant for one page driven from one thread; the lock only keeps cursor state consistent.
    Encapsulates state for:
      - Continuous mouse tracking (current_x, current_y)
      - Consecutive click continuity and spatial inertia
      - Session duration and linear fatigue drift
      - Lobby stuck state watchdog
    """

    def __init__(
        self,
        page: Optional[Any] = None,
        session_start: Optional[float] = None,
        window_size: Tuple[int, int] = DEFAULT_WINDOW_SIZE,
        fatigue_horizon: float = 300.0,
    ):
        self.page = page
        self.session_start = session_start or time.time()
        self.window_size = window_size
        self.fatigue_horizon = max(1.0, fatigue_horizon)
        self._lock = threading.Lock()

        # Mouse spatial state
        self.current_x: Optional[float] = None
        self.current_y: Optional[float] = None

        # Click continuity state (for consecutive clicks on the same button)
        self.last_click_box: Optional[Dict[str, float]] = None
        self.last_click_coord: Optional[Tuple[float, float]] = None

        # Stuck watchdog state
        self.stuck_counter: int = 0
        self.stuck_start_time: Optional[float] = None

    def get_fatigue_scale(self) -> float:
        """
        Compute active session fatigue factor.
        Linearly increases mean reaction times by up to 25% and expands variance
        over `fatigue_horizon` seconds (normally the session length).
        """
        elapsed = time.time() - self.session_start
        # Scales from 1.0 at the session start to 1.25 at fatigue_horizon
        return 1.0 + 0.25 * min(1.0, max(0.0, elapsed / self.fatigue_horizon))

    def _ensure_cursor_pos(self) -> Tuple[float, float]:
        """Ensure valid initial cursor coordinates within page viewport."""
        if self.current_x is None or self.current_y is None:
            self.current_x = self.window_size[0] / 2.0 + random.uniform(-50.0, 50.0)
            self.current_y = self.window_size[1] / 2.0 + random.uniform(-40.0, 40.0)
        return self.current_x, self.current_y

    def sleep(
        self,
        mean: float = 0.25,
        sigma: float = 0.04,
        tau: float = 0.06,
        min_floor: float = 0.11,
    ) -> float:
        """Sleep with Ex-Gaussian right-skewed cognitive distribution and fatigue scaling."""
        return human_sleep(mean, sigma, tau, min_floor=min_floor, fatigue_scale=self.get_fatigue_scale())

    def move(
        self,
        target_x: float,
        target_y: float,
        enable_overshoot: bool = True,
    ) -> Tuple[float, float]:
        """
        Move the cursor to (target_x, target_y) using WindMouse physics,
        Fitts's Law easing, and muscular tremor.
        """
        with self._lock:
            start_pos = self._ensure_cursor_pos()
            fatigue = self.get_fatigue_scale()
            final_x, final_y = human_move(
                self.page,
                target_x,
                target_y,
                start_pos=start_pos,
                enable_overshoot=enable_overshoot,
                fatigue_scale=fatigue,
            )
            self.current_x = final_x
            self.current_y = final_y
            return final_x, final_y

    def click_locator(
        self,
        locator: Any,
        timeout: float = 4000,
    ) -> Optional[Tuple[float, float]]:
        """
        Click a Playwright Locator with full biometric fidelity:
        - Bivariate truncated Gaussian coordinate sampling inside bounding box.
        - Consecutive click continuity: maintains 1-3px click inertia if re-clicking the same box.
        - WindMouse movement trajectory to coordinates.
        - Pre-click dwell, split mouse.down(), contact hold (65-135ms), mouse.up(), and post-click release.
        """
        try:
            box = locator.bounding_box(timeout=timeout)
        except Exception:
            box = None

        if not box:
            # Fallback if bounding box cannot be resolved
            try:
                locator.click(timeout=timeout)
            except Exception:
                pass
            return None

        with self._lock:
            # Check consecutive click continuity
            if is_same_bounding_box(box, self.last_click_box) and self.last_click_coord:
                # Maintain click inertia: tight 1-3px Gaussian micro-jitter around previous anchor
                raw_x = self.last_click_coord[0] + float(np.random.normal(0.0, 1.2))
                raw_y = self.last_click_coord[1] + float(np.random.normal(0.0, 1.2))
                # Ensure it remains safely clamped within box
                min_x = box["x"] + box["width"] * 0.12
                max_x = box["x"] + box["width"] * 0.88
                min_y = box["y"] + box["height"] * 0.12
                max_y = box["y"] + box["height"] * 0.88
                target_x = float(np.clip(raw_x, min_x, max_x))
                target_y = float(np.clip(raw_y, min_y, max_y))
            else:
                target_x, target_y = sample_bivariate_gaussian_box(box)

            start_pos = self._ensure_cursor_pos()
            fatigue = self.get_fatigue_scale()

            # Execute physics move
            final_x, final_y = human_move(
                self.page,
                target_x,
                target_y,
                start_pos=start_pos,
                enable_overshoot=True,
                fatigue_scale=fatigue,
            )
            self.current_x = final_x
            self.current_y = final_y
            self.last_click_box = box
            self.last_click_coord = (final_x, final_y)

            # Pre-click motor dwell (40-80ms)
            time.sleep(sample_ex_gaussian(mean=0.05, sigma=0.01, tau=0.02, min_floor=0.038, fatigue_scale=fatigue))

            # Split mouse event dispatching
            self.page.mouse.down()
            time.sleep(sample_contact_duration())
            self.page.mouse.up()

            # Post-click release dwell (50-100ms)
            time.sleep(sample_ex_gaussian(mean=0.065, sigma=0.015, tau=0.025, min_floor=0.045, fatigue_scale=fatigue))

            return final_x, final_y

    def click_coords(
        self,
        x: float,
        y: float,
        enable_overshoot: bool = True,
    ) -> Tuple[float, float]:
        """Click arbitrary screen coordinates with physics movement and realistic contact duration."""
        with self._lock:
            start_pos = self._ensure_cursor_pos()
            fatigue = self.get_fatigue_scale()
            final_x, final_y = human_move(
                self.page,
                x,
                y,
                start_pos=start_pos,
                enable_overshoot=enable_overshoot,
                fatigue_scale=fatigue,
            )
            self.current_x = final_x
            self.current_y = final_y

            # Pre-click dwell
            time.sleep(sample_ex_gaussian(mean=0.05, sigma=0.01, tau=0.02, min_floor=0.038, fatigue_scale=fatigue))

            # Mouse down, contact hold, mouse up
            self.page.mouse.down()
            time.sleep(sample_contact_duration())
            self.page.mouse.up()

            # Post-click release
            time.sleep(sample_ex_gaussian(mean=0.065, sigma=0.015, tau=0.025, min_floor=0.045, fatigue_scale=fatigue))

            return final_x, final_y

    def press_key(self, key: str, hesitation: bool = False) -> None:
        """Press a keyboard key with discrete down/up events, contact duration, and optional hesitation."""
        human_key_tap(self.page, key, hesitation=hesitation, fatigue_scale=self.get_fatigue_scale())

    def type_text(self, text: str, typo_prob: float = 0.04) -> None:
        """Type text into focused element with natural keystroke dynamics and typo simulation."""
        human_type_string(self.page, text, typo_prob=typo_prob, fatigue_scale=self.get_fatigue_scale())

    def idle_drift(self, duration: float = 0.7, max_offset: float = 18.0) -> None:
        """Perform subtle ambient cursor drift to simulate user resting hand during waiting periods."""
        with self._lock:
            start_x, start_y = self._ensure_cursor_pos()
            pts = generate_idle_drift_points(start_x, start_y, max_offset=max_offset, bounds=self.window_size)
            if not pts:
                return

            step_delay = max(0.008, duration / len(pts))
            for px, py in pts:
                try:
                    self.page.mouse.move(px, py)
                except Exception:
                    break
                time.sleep(step_delay)

            self.current_x = pts[-1][0]
            self.current_y = pts[-1][1]

    def check_watchdog(
        self,
        is_stuck: bool,
        max_iterations: int = 15,
        max_seconds: float = 30.0,
    ) -> bool:
        """
        Track consecutive stuck iterations / duration.
        Returns True if the bot has remained trapped beyond thresholds.
        """
        with self._lock:
            if is_stuck:
                self.stuck_counter += 1
                if self.stuck_start_time is None:
                    self.stuck_start_time = time.time()
                elapsed = time.time() - self.stuck_start_time
                if self.stuck_counter >= max_iterations or elapsed >= max_seconds:
                    return True
                return False
            else:
                self.stuck_counter = 0
                self.stuck_start_time = None
                return False

    def reset_watchdog(self) -> None:
        """Reset stuck watchdog counters."""
        with self._lock:
            self.stuck_counter = 0
            self.stuck_start_time = None
