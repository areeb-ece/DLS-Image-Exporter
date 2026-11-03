#!/usr/bin/env python3
"""
DLS Image Exporter — batch-render Digital Logic Sim (v2.x) chips to images.

Two render modes:
  circuit   The inside of each chip, drawn like the in-game editor view.
  expanded  Same, but every custom sub-chip is opened up in place, recursively.
            Each one becomes a bordered frame (titled with the chip name)
            that holds its own inner circuit, so you can see all the parts
            a component is built from.

Both modes use the auto layout by default: the parts are re-placed in columns by
signal flow and the wires re-routed so they're easy to follow. --layout original
keeps your own placement and wire bends instead.

It reads the game's save files directly (it doesn't touch them), and it copies
the game's layout rules (pin spacing, chip sizes, built-in chips, wire
geometry) from the public DLS source so that wires meet pins where they do
in the game.

Usage examples:
  python3 dls_export.py                      # every project, every chip, both modes
  python3 dls_export.py --list               # show projects and chips
  python3 dls_export.py -p ALU               # only the ALU project
  python3 dls_export.py -p ALU -c ALU "FULL ADDER" --mode expanded --format both
  python3 dls_export.py -p ALU --mode expanded --depth 1      # open only one level
  python3 dls_export.py -p ALU --mode expanded --collapse AND,OR,XOR
"""

import argparse
import colorsys
import json
import math
import os
import re
import sys

try:
    import cairo
except ImportError:  # pragma: no cover
    sys.exit("pycairo is required:  pip install pycairo   (or your distro's python-cairo package)")

# ---------------------------------------------------------------------------
# Constants mirrored from DLS (Assets/Scripts/Graphics/DrawSettings.cs)
# ---------------------------------------------------------------------------
GRID = 0.125
PIN_H1, PIN_H4, PIN_H8 = 0.185, 0.3, 0.43
PIN_R = PIN_H1 / 2
FONT_CHIP = 0.25
FONT_PIN = 0.2
PIN_INSET = 0.015
OUTLINE = 0.05
WIRE_T = 0.025
DEVPIN_R = 0.2
DEVPIN_OUTLINE = 0.0175
DEVPIN_HANDLE_W = DEVPIN_R * 0.64
MB_SQ = 0.21
LABEL_PAD = (0.15, 0.1)

STATE_LOW = [(0.2, 0.1, 0.1), (0.28, 0.15, 0.01), (0.26, 0.2, 0.07), (0.1, 0.2, 0.1),
             (0.1, 0.14, 0.35), (0.19, 0.12, 0.28), (0.25, 0.1, 0.25), (0.35, 0.35, 0.35)]
STATE_HIGH = [(0.95, 0.3, 0.31), (0.92, 0.44, 0.12), (0.98, 0.76, 0.26), (0.25, 0.66, 0.31),
              (0.2, 0.5, 1.0), (0.6, 0.4, 0.98), (0.84, 0.33, 0.9), (0.9, 0.9, 0.9)]
BG = (66 / 255, 66 / 255, 69 / 255)
GRID_COL = (49 / 255, 49 / 255, 51 / 255)
DEVPIN_HANDLE_COL = (0.31, 0.31, 0.31)

# Expanded-view frame styling (not from DLS; this is the exporter's own styling)
FRAME_PAD = 0.35
FRAME_TITLE_H = 0.42
FRAME_BORDER = 0.06
FRAME_GAP = 0.5  # extra breathing room around expanded frames

EPS = 1e-4
FONT_FACE = "monospace"

# ---------------------------------------------------------------------------
# Colour helpers (mirrors Seb.Helpers.ColHelper)
# ---------------------------------------------------------------------------


def tweak_hsv(c, ds=0.0, dv=0.0):
    h, s, v = colorsys.rgb_to_hsv(*c[:3])
    s = min(1, max(0, s + ds))
    v = min(1, max(0, v + dv))
    return colorsys.hsv_to_rgb(h, s, v)


def darken(c, a, desat=0.0):
    return tweak_hsv(c, -desat, -a)


def brighten(c, a, sat=0.0):
    return tweak_hsv(c, sat, a)


def luminance(c):
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]


def saturation(c):
    return colorsys.rgb_to_hsv(*c[:3])[1]


def lerp_col(a, b, t):
    t = min(1, max(0, t))
    return tuple(a[i] + (b[i] - a[i]) * t for i in range(3))


def chip_outline_col(c):
    return brighten(c, 0.15) if max(c[:3]) < 0.075 else darken(c, 0.15)


def chip_name_col(c):
    black = luminance(c) > 0.57
    main = (0, 0, 0) if black else (1, 1, 1)
    low_sat = darken(c, 0.5) if black else brighten(c, 0.25)
    return lerp_col(low_sat, main, saturation(c) / 0.1)


# ---------------------------------------------------------------------------
# Built-in chips (mirrors Assets/Scripts/Game/Project/BuiltinChipCreator.cs)
# ---------------------------------------------------------------------------
TYPE_CUSTOM = 0
NAME_CENTRE, NAME_TOP, NAME_HIDDEN = 0, 1, 2


def _pin(name, pid, bits=1):
    return {"Name": name, "ID": pid, "BitCount": bits, "Colour": 0, "Position": {"x": 0, "y": 0}}


def _snap_even(v):
    i = round(v / GRID)
    if i & 1:
        i += 1
    return i * GRID


def _snapped_width(w):
    return _snap_even(w) - (OUTLINE - 2 * PIN_INSET)


def default_pin_layout(bitcounts):
    """Returns (min chip height, [pin grid y]) — SubChipInstance.CalculateDefaultPinLayout."""
    gy = 0
    ys = []
    for b in bitcounts:
        h = 2 if b == 1 else 3 if b == 4 else 4
        ys.append(gy - h / 2)
        gy -= h
    return abs(gy) * GRID, ys


def min_height(*pin_lists):
    return max([default_pin_layout([p["BitCount"] for p in pins])[0] for pins in pin_lists if pins] + [0])


def _builtin(name, btype, size, col, ins, outs, name_loc=NAME_CENTRE):
    return {"Name": name, "ChipType": btype, "Builtin": True, "NameLocation": name_loc,
            "Size": {"x": size[0], "y": size[1]}, "Colour": {"r": col[0], "g": col[1], "b": col[2], "a": 1},
            "InputPins": ins or [], "OutputPins": outs or [], "SubChips": [], "Wires": []}


def create_builtins():
    dark = (0.1, 0.1, 0.1)
    b = {}

    def add(d):
        b[d["Name"]] = d

    add(_builtin("NAND", "Nand", (_snapped_width(GRID * 8), GRID * 4), (0.73, 0.26, 0.26),
                 [_pin("IN B", 0), _pin("IN A", 1)], [_pin("OUT", 2)]))
    add(_builtin("3-STATE BUFFER", "TriStateBuffer", (_snapped_width(1.5), GRID * 5), dark,
                 [_pin("IN", 0), _pin("ENABLE", 1)], [_pin("OUT", 2)]))
    add(_builtin("CLOCK", "Clock", (1.0, GRID * 3), dark, None, [_pin("CLK", 0)]))
    add(_builtin("PULSE", "Pulse", (1.0, GRID * 3), dark, [_pin("IN", 0)], [_pin("PULSE", 1)]))
    add(_builtin("KEY", "Key", (GRID * 3, GRID * 3), dark, None, [_pin("OUT", 0)], NAME_HIDDEN))

    ram_in = [_pin("ADDRESS", 0, 8), _pin("DATA", 1, 8), _pin("WRITE", 2), _pin("RESET", 3), _pin("CLOCK", 4)]
    ram_out = [_pin("OUT", 5, 8)]
    add(_builtin("dev.RAM-8", "dev_Ram_8Bit", (GRID * 10, min_height(ram_in, ram_out)), (0.85, 0.45, 0.3), ram_in, ram_out))
    rom_in = [_pin("ADDRESS", 0, 8)]
    rom_out = [_pin("OUT B", 1, 8), _pin("OUT A", 2, 8)]
    add(_builtin("ROM 256×16", "Rom_256x16", (GRID * 12, min_height(rom_in, rom_out)), (0.25, 0.35, 0.5), rom_in, rom_out))

    def conv(name, bin_, bout, nin, nout):
        def pname(i, n, is_in):
            letter = "" if n == 1 else " " + chr(ord("A") + n - i - 1)
            return ("IN" if is_in else "OUT") + letter
        ins = [_pin(pname(i, nin, True), i, bin_) for i in range(nin)]
        outs = [_pin(pname(i, nout, False), nin + i, bout) for i in range(nout)]
        add(_builtin(name, "BitConversion", (GRID * 9, min_height(ins, outs)), dark, ins, outs))

    conv("4-1BIT", 4, 1, 1, 4)
    conv("8-4BIT", 8, 4, 1, 2)
    conv("8-1BIT", 8, 1, 1, 8)
    conv("1-8BIT", 1, 8, 8, 1)
    conv("1-4BIT", 1, 4, 4, 1)
    conv("4-8BIT", 4, 8, 2, 1)

    seg_in = [_pin(n, i) for i, n in enumerate(["A", "B", "C", "D", "E", "F", "G", "COL"])]
    add(_builtin("7-SEGMENT", "SevenSegmentDisplay", (GRID * 10, min_height(seg_in)), dark, seg_in, None, NAME_HIDDEN))
    rgb_in = [_pin("ADDRESS", 0, 8), _pin("RED", 1, 4), _pin("GREEN", 2, 4), _pin("BLUE", 3, 4),
              _pin("RESET", 4), _pin("WRITE", 5), _pin("REFRESH", 6), _pin("CLOCK", 7)]
    rgb_out = [_pin("R OUT", 8, 4), _pin("G OUT", 9, 4), _pin("B OUT", 10, 4)]
    add(_builtin("RGB DISPLAY", "DisplayRGB", (GRID * 21, GRID * 21), dark, rgb_in, rgb_out, NAME_HIDDEN))
    dot_in = [_pin("ADDRESS", 0, 8), _pin("PIXEL IN", 1), _pin("RESET", 2), _pin("WRITE", 3),
              _pin("REFRESH", 4), _pin("CLOCK", 5)]
    h = min_height(dot_in)
    add(_builtin("DOT DISPLAY", "DisplayDot", (h, h), dark, dot_in, [_pin("PIXEL OUT", 6)], NAME_HIDDEN))
    led_in = [_pin("IN", 0)]
    h = min_height(led_in)
    add(_builtin("LED", "DisplayLED", (h, h), dark, led_in, None, NAME_HIDDEN))
    add(_builtin("BUZZER", "Buzzer", (_snapped_width(GRID * 9), min_height([_pin("P", 1, 8), _pin("V", 0, 4)])),
                 (0, 0, 0), [_pin("PITCH", 1, 8), _pin("VOLUME", 0, 4)], None))

    for bits, hgt in ((1, 2), (4, 3), (8, 4)):
        size = (GRID * 2, GRID * hgt)
        name = f"BUS-{bits}"
        add(_builtin(name, "Bus", size, dark, [_pin(name + " (Hidden)", 0, bits)], [_pin(name, 1, bits)], NAME_HIDDEN))
        add(_builtin(f"BUS-TERMINUS-{bits}", "BusTerminus", size, dark, [_pin(name, 0, bits)], None, NAME_HIDDEN))
    return b


BUILTINS = create_builtins()


def placeholder(name):
    return _builtin(name, "Missing", (1.0, 0.5), (0.3, 0.3, 0.3), [], [])


# ---------------------------------------------------------------------------
# Save data
# ---------------------------------------------------------------------------


def default_saves_dir():
    home = os.path.expanduser("~")
    candidates = [
        os.path.join(home, ".config/unity3d/SebastianLague/Digital-Logic-Sim/Projects"),
        os.path.join(home, "AppData/LocalLow/SebastianLague/Digital-Logic-Sim/Projects"),
        os.path.join(home, "Library/Application Support/com.SebastianLague.Digital-Logic-Sim/Projects"),
        os.path.join(home, "Library/Application Support/SebastianLague/Digital-Logic-Sim/Projects"),
    ]
    for c in candidates:
        if os.path.isdir(c):
            return c
    return candidates[0]


class Library:
    def __init__(self, project_dir):
        self.project_dir = project_dir
        self.custom = {}
        chips_dir = os.path.join(project_dir, "Chips")
        if os.path.isdir(chips_dir):
            for fn in sorted(os.listdir(chips_dir)):
                if not fn.endswith(".json"):
                    continue
                try:
                    with open(os.path.join(chips_dir, fn), encoding="utf-8-sig") as f:
                        d = json.load(f)
                    self.custom[d.get("Name") or fn[:-5]] = d
                except (OSError, ValueError) as e:
                    print(f"  ! could not read {fn}: {e}", file=sys.stderr)
        self.order = list(self.custom)
        try:
            with open(os.path.join(project_dir, "ProjectDescription.json"), encoding="utf-8-sig") as f:
                listed = json.load(f).get("AllCustomChipNames") or []
            self.order = [n for n in listed if n in self.custom] + [n for n in self.custom if n not in listed]
        except (OSError, ValueError):
            pass

    def get(self, name):
        if name in self.custom:
            return self.custom[name]
        return BUILTINS.get(name)


def is_custom(desc):
    return not desc.get("Builtin") and desc.get("ChipType", 0) == TYPE_CUSTOM


def col_of(desc):
    c = desc.get("Colour") or {}
    return (c.get("r", 0.3), c.get("g", 0.3), c.get("b", 0.3))


def vec(d):
    return (d["x"], d["y"]) if d else (0.0, 0.0)


# ---------------------------------------------------------------------------
# Text measuring
# ---------------------------------------------------------------------------
_MEASURE_CTX = cairo.Context(cairo.ImageSurface(cairo.FORMAT_ARGB32, 1, 1))


def set_font(ctx):
    ctx.select_font_face(FONT_FACE, cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)


def text_width(s, fs):
    set_font(_MEASURE_CTX)
    _MEASURE_CTX.set_font_size(100)
    return max((_MEASURE_CTX.text_extents(l).x_advance for l in s.split("\n")), default=0) * fs / 100


def multi_line_name(name):
    """SubChipInstance.CreateMultiLineName (without the padding tricks)."""
    if len(name) <= 6 or " " not in name:
        return name
    best, best_pen = name, float("inf")
    for i, ch in enumerate(name):
        if ch == " ":
            a, b = name[:i].strip(), name[i:].strip()
            pen = abs(len(a) - len(b))
            if pen < best_pen:
                best, best_pen = a + "\n" + b, pen
    return best


# ---------------------------------------------------------------------------
# Scene geometry
# ---------------------------------------------------------------------------


class Pin:
    __slots__ = ("pos", "orig", "bits", "is_source", "colour", "name", "face")

    def __init__(self, pos, orig, bits, is_source, colour, name, face):
        self.pos, self.orig, self.bits, self.is_source = pos, orig, bits, is_source
        self.colour, self.name, self.face = colour, name, face


class Chip:
    def __init__(self):
        self.desc = None
        self.name = ""
        self.label = ""
        self.center = (0, 0)
        self.size = (0, 0)
        self.pins = []
        self.inner = None  # Scene, when expanded
        self.inner_offset = (0, 0)
        self.depth = 0


class DevPin:
    def __init__(self, desc, is_input, handle, orig_handle):
        self.name = desc.get("Name", "")
        self.id = desc["ID"]
        self.bits = desc.get("BitCount", 1)
        self.colour = desc.get("Colour", 0) or 0
        self.is_input = is_input
        self.face = 1 if is_input else -1
        self.handle = handle
        dist = GRID * (6 if self.bits in (1, 4) else 9)
        pin_pos = (handle[0] + self.face * dist, handle[1])
        orig_pin = (orig_handle[0] + self.face * dist, orig_handle[1])
        self.pin = Pin(pin_pos, orig_pin, self.bits, is_input, self.colour, self.name, self.face)
        if self.bits == 1:
            self.grid_dim = (1, 1)
            self.grid_size = (DEVPIN_R * 2 + DEVPIN_OUTLINE * 2,) * 2
        else:
            self.grid_dim = (2, 2) if self.bits == 4 else (4, 2)
            self.grid_size = (self.grid_dim[0] * MB_SQ + DEVPIN_OUTLINE, self.grid_dim[1] * MB_SQ + DEVPIN_OUTLINE)
        self.state_pos = (handle[0] + self.face * (DEVPIN_HANDLE_W / 2 + self.grid_size[0] / 2 + 0.065), handle[1])


class Wire:
    def __init__(self, points, bits, colour, junction, depth, net=None, solid=False):
        self.points, self.bits, self.colour, self.junction, self.depth = points, bits, colour, junction, depth
        self.solid = solid
        self.net = net


class Link:
    """Dashed line inside an expanded frame: frame edge pin -> the inner IN/OUT pin it feeds."""

    def __init__(self, a, b, colour=None, bits=1):
        self.a, self.b, self.colour, self.bits = a, b, colour, bits


class Scene:
    def __init__(self, desc):
        self.desc = desc
        self.chips = []
        self.devpins = []
        self.devpins_by_id = {}
        self.wires = []
        self.links = []
        self.bbox = [0, 0, 0, 0]


class AxisMap:
    """Monotone piecewise-linear stretch of one axis.

    Each interval (lo, hi, required_len) belongs to one sub-chip. Overlapping intervals
    are merged, and each merged group is scaled by the largest stretch any member needs.
    Everything outside a group keeps scale 1. Because the map is monotone and separable,
    chips that didn't overlap before still don't, and horizontal or vertical wire
    segments stay horizontal or vertical.
    """

    def __init__(self, intervals):
        groups = []
        for lo, hi, req in sorted(intervals):
            if hi - lo <= EPS:
                continue
            k = max(1.0, req / (hi - lo))
            if groups and lo < groups[-1][1] - EPS:
                g = groups[-1]
                g[1] = max(g[1], hi)
                g[2] = max(g[2], k)
            else:
                groups.append([lo, hi, k])
        self.groups = [g for g in groups if g[2] > 1 + EPS]

    def __call__(self, x):
        off = 0.0
        for lo, hi, k in self.groups:
            if x <= lo:
                break
            if x < hi:
                return x + off + (k - 1) * (x - lo)
            off += (k - 1) * (hi - lo)
        return x + off


def pin_layout_y(bitcounts, size_y):
    n = len(bitcounts)
    if n == 0:
        return []
    if n == 1:
        return [0.0]
    chip_h, gys = default_pin_layout(bitcounts)
    ys = [size_y / 2 + gy * GRID for gy in gys]
    rem = size_y - chip_h
    if rem > 0:
        sp = rem / (n - 1)
        ys = [y - sp * i for i, y in enumerate(ys)]
    return ys


def pin_x_offset(size_x, face):
    return (size_x / 2 + OUTLINE / 2 - PIN_INSET) * face


def closest_on_segment(p, a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    sq = dx * dx + dy * dy
    if sq == 0:
        return a
    t = max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / sq))
    return (a[0] + dx * t, a[1] + dy * t)


def closest_on_wire(pts, p):
    best, bd = pts[0], float("inf")
    for i in range(len(pts) - 1):
        q = closest_on_segment(p, pts[i], pts[i + 1])
        d = (q[0] - p[0]) ** 2 + (q[1] - p[1]) ** 2
        if d < bd:
            best, bd = q, d
    return best


class Options:
    def __init__(self, expand=False, depth=None, collapse=(), dev_pin_names=True, chip_pin_names=False, lit=False,
                 aspect=1.8, dissolve_converters=True):
        self.aspect = aspect
        self.dissolve_converters = dissolve_converters
        self.expand = expand
        self.depth = depth
        self.collapse = set(collapse)
        self.dev_pin_names = dev_pin_names
        self.chip_pin_names = chip_pin_names
        self.lit = lit


def build_scene(lib, desc, opts, depth=0, stack=frozenset()):
    sc = Scene(desc)
    stack = stack | {desc.get("Name")}

    # ---- 1. sub-chips: decide which are expanded, and how big their frames are ----
    entries = []
    for s in desc.get("SubChips") or []:
        cd = lib.get(s["Name"]) or placeholder(s["Name"])
        osize = vec(cd["Size"])
        inner = None
        fsize = osize
        can_expand = (opts.expand and is_custom(cd) and (cd.get("SubChips") or cd.get("Wires"))
                      and (opts.depth is None or depth < opts.depth)
                      and s["Name"] not in opts.collapse and s["Name"] not in stack)
        if can_expand:
            inner = build_scene(lib, cd, opts, depth + 1, stack)
            bw = inner.bbox[2] - inner.bbox[0]
            bh = inner.bbox[3] - inner.bbox[1]
            fsize = (max(osize[0], bw + 2 * FRAME_PAD), max(osize[1], bh + 2 * FRAME_PAD + FRAME_TITLE_H))
        entries.append((s, cd, osize, fsize, inner))

    # ---- 2. stretch the coordinate space so the expanded frames fit ----
    xi, yi = [], []
    for s, cd, osize, fsize, inner in entries:
        x, y = vec(s["Position"])
        gap = FRAME_GAP if inner else 0
        xi.append((x - osize[0] / 2, x + osize[0] / 2, fsize[0] + gap))
        yi.append((y - osize[1] / 2, y + osize[1] / 2, fsize[1] + gap))
    fx, fy = AxisMap(xi), AxisMap(yi)

    def fmap(p):
        return (fx(p[0]), fy(p[1]))

    pins = {}  # (owner id, pin id) -> Pin

    for s, cd, osize, fsize, inner in entries:
        c = Chip()
        c.desc, c.name, c.label, c.depth = cd, s["Name"], s.get("Label") or "", depth
        opos = vec(s["Position"])
        c.center = fmap(opos)
        c.size = fsize
        c.inner = inner
        out_cols = {ci["PinID"]: ci["PinColour"] for ci in (s.get("OutputPinColourInfo") or [])}

        if inner:
            # Content area sits under the title band; inner scene is centred in it
            ccx, ccy = c.center[0], c.center[1] - FRAME_TITLE_H / 2
            bx = (inner.bbox[0] + inner.bbox[2]) / 2
            by = (inner.bbox[1] + inner.bbox[3]) / 2
            c.inner_offset = (ccx - bx, ccy - by)

        for is_input, plist in ((True, cd.get("InputPins") or []), (False, cd.get("OutputPins") or [])):
            face = -1 if is_input else 1
            oys = pin_layout_y([p["BitCount"] for p in plist], osize[1])
            nys = pin_layout_y([p["BitCount"] for p in plist], fsize[1])
            for p, oy, ny in zip(plist, oys, nys):
                if inner:
                    dp = inner.devpins_by_id.get(p["ID"])
                    if dp is not None:
                        ny = dp.pin.pos[1] + c.inner_offset[1] - c.center[1]
                        edge = (c.center[0] + pin_x_offset(fsize[0], face), c.center[1] + ny)
                        handle_edge = dp.handle[0] - dp.face * DEVPIN_HANDLE_W / 2 + c.inner_offset[0]
                        sc.links.append(Link(edge, (handle_edge, edge[1])))
                pos = (c.center[0] + pin_x_offset(fsize[0], face), c.center[1] + ny)
                orig = (opos[0] + pin_x_offset(osize[0], face), opos[1] + oy)
                colour = 0 if is_input else out_cols.get(p["ID"], p.get("Colour", 0) or 0)
                pin = Pin(pos, orig, p["BitCount"], not is_input, colour, p.get("Name", ""), face)
                c.pins.append(pin)
                pins[(s["ID"], p["ID"])] = pin
        sc.chips.append(c)

    # ---- 3. the chip's own IN/OUT pins ----
    for is_input, plist in ((True, desc.get("InputPins") or []), (False, desc.get("OutputPins") or [])):
        for p in plist:
            oh = vec(p.get("Position"))
            dp = DevPin(p, is_input, fmap(oh), oh)
            sc.devpins.append(dp)
            sc.devpins_by_id[dp.id] = dp
            pins[(dp.id, 0)] = dp.pin

    # ---- 4. wires ----
    wdescs = desc.get("Wires") or []
    memo_o, memo_n, busy = {}, {}, set()

    def lookup(addr):
        return pins.get((addr["PinOwnerID"], addr["PinID"]))

    def geom(i, new):
        memo = memo_n if new else memo_o
        if i in memo:
            return memo[i]
        if (i, new) in busy or not (0 <= i < len(wdescs)):
            return None
        busy.add((i, new))
        memo[i] = _geom(i, new)
        busy.discard((i, new))
        return memo[i]

    def _geom(i, new):
        w = wdescs[i]
        src, tgt = lookup(w["SourcePinAddress"]), lookup(w["TargetPinAddress"])
        if src is None or tgt is None:
            return None
        ctype = w.get("ConnectionType", 0)
        saved = [vec(p) for p in (w.get("Points") or [])]
        if len(saved) < 2:
            saved = [(0, 0), (0, 0)]
        cw_idx = w.get("ConnectedWireIndex", -1)
        seg = w.get("ConnectedWireSegmentIndex", 0)

        if not new:
            pts = list(saved)
            pts[0] = src.orig
            pts[-1] = tgt.orig
            for end, kind in ((0, 1), (-1, 2)):
                if ctype == kind:
                    other = geom(cw_idx, False)
                    if other and 0 <= seg < len(other) - 1:
                        pts[end] = closest_on_segment(saved[end], other[seg], other[seg + 1])
                    elif other:
                        pts[end] = closest_on_wire(other, saved[end])
            return pts

        orig = geom(i, False)
        if orig is None:
            return None
        pts = [fmap(p) for p in orig]
        pts[0] = src.pos
        pts[-1] = tgt.pos
        for end, kind in ((0, 1), (-1, 2)):
            if ctype == kind:
                other = geom(cw_idx, True)
                if other:
                    pts[end] = closest_on_wire(other, fmap(orig[end]))

        # Keep wire stubs straight: if the first/last segment was horizontal or vertical before
        # the stretch, nudge the neighbouring bend point so it still is.
        def straight(e, nb):
            if abs(orig[e][1] - orig[nb][1]) < EPS:
                pts[nb] = (pts[nb][0], pts[e][1])
            elif abs(orig[e][0] - orig[nb][0]) < EPS:
                pts[nb] = (pts[e][0], pts[nb][1])

        if len(pts) > 2:
            straight(0, 1)
            straight(-1, -2)
        else:
            a, b = pts
            if abs(orig[0][1] - orig[1][1]) < EPS and abs(a[1] - b[1]) > EPS:
                mx = (a[0] + b[0]) / 2
                pts = [a, (mx, a[1]), (mx, b[1]), b]
            elif abs(orig[0][0] - orig[1][0]) < EPS and abs(a[0] - b[0]) > EPS:
                my = (a[1] + b[1]) / 2
                pts = [a, (a[0], my), (b[0], my), b]
        return pts

    def rec_depth(i, seen=()):
        w = wdescs[i]
        j = w.get("ConnectedWireIndex", -1)
        if w.get("ConnectionType", 0) == 0 or j < 0 or j in seen or j >= len(wdescs):
            return 0
        return 1 + rec_depth(j, seen + (i,))

    for i, w in enumerate(wdescs):
        pts = geom(i, True)
        if pts is None:
            continue
        src = lookup(w["SourcePinAddress"])
        ctype = w.get("ConnectionType", 0)
        junction = pts[0] if ctype == 1 else pts[-1] if ctype == 2 else None
        sc.wires.append(Wire(pts, src.bits, src.colour, junction, rec_depth(i)))
    sc.wires.sort(key=lambda w: w.depth)

    sc.bbox = scene_bbox(sc, opts)
    return sc


def scene_bbox(sc, opts):
    xs, ys = [], []

    def add(x0, y0, x1, y1):
        xs.extend((x0, x1))
        ys.extend((y0, y1))

    for c in sc.chips:
        hw = c.size[0] / 2 + OUTLINE / 2 + PIN_R
        hh = c.size[1] / 2 + OUTLINE / 2
        add(c.center[0] - hw, c.center[1] - hh, c.center[0] + hw, c.center[1] + hh)
        if c.label.strip():
            w = text_width(c.label, FONT_PIN) + LABEL_PAD[0]
            add(c.center[0] - w / 2, c.center[1] - hh - 0.2 - 0.2, c.center[0] + w / 2, c.center[1] - hh)
        if opts.chip_pin_names:
            for p in c.pins:
                if p.name.strip():
                    w = text_width(p.name, FONT_PIN) + LABEL_PAD[0] + PIN_R + 0.05
                    add(min(p.pos[0], p.pos[0] + p.face * w), p.pos[1], max(p.pos[0], p.pos[0] + p.face * w), p.pos[1])
    for d in sc.devpins:
        x0 = d.handle[0] - d.face * DEVPIN_HANDLE_W / 2
        x1 = d.pin.pos[0] + d.face * PIN_R
        x2 = d.state_pos[0] + d.face * d.grid_size[0] / 2
        add(min(x0, x1, x2), d.handle[1] - d.grid_size[1] / 2, max(x0, x1, x2), d.handle[1] + d.grid_size[1] / 2)
        if opts.dev_pin_names and d.name.strip():
            centre, w = devpin_label_placement(d)
            add(centre[0] - w / 2, centre[1], centre[0] + w / 2, centre[1])
    for w in sc.wires:
        for p in w.points:
            add(p[0] - WIRE_T * w.bits, p[1] - WIRE_T * w.bits, p[0] + WIRE_T * w.bits, p[1] + WIRE_T * w.bits)
    if not xs:
        return [-1, -0.5, 1, 0.5]
    return [min(xs), min(ys), max(xs), max(ys)]


# ---------------------------------------------------------------------------
# Drawing (world space, y up)
# ---------------------------------------------------------------------------


def rgb(ctx, c, a=1.0):
    ctx.set_source_rgba(c[0], c[1], c[2], a)


def quad(ctx, centre, size, col, a=1.0):
    rgb(ctx, col, a)
    ctx.rectangle(centre[0] - size[0] / 2, centre[1] - size[1] / 2, size[0], size[1])
    ctx.fill()


def point(ctx, p, r, col):
    rgb(ctx, col)
    ctx.arc(p[0], p[1], r, 0, 2 * math.pi)
    ctx.fill()


def polyline(ctx, pts, width, col, a=1.0, cap=cairo.LINE_CAP_ROUND):
    rgb(ctx, col, a)
    ctx.set_line_width(width)
    ctx.set_line_cap(cap)
    ctx.set_line_join(cairo.LINE_JOIN_ROUND)
    ctx.move_to(*pts[0])
    for p in pts[1:]:
        ctx.line_to(*p)
    ctx.stroke()


def draw_text(ctx, s, fs, pos, col, halign="center", valign="center", line_spacing=1.0):
    lines = s.split("\n")
    ctx.save()
    ctx.translate(pos[0], pos[1])
    ctx.scale(1, -1)
    set_font(ctx)
    ctx.set_font_size(fs)
    cap = fs * 0.72
    lh = fs * 1.15 * line_spacing
    total = cap + lh * (len(lines) - 1)
    y0 = (-total / 2 + cap) if valign == "center" else cap
    rgb(ctx, col)
    for i, line in enumerate(lines):
        w = ctx.text_extents(line).x_advance
        x = -w / 2 if halign == "center" else 0 if halign == "left" else -w
        ctx.move_to(x, y0 + i * lh)
        ctx.show_text(line)
    ctx.restore()


def text_label(ctx, text, centre):
    w = text_width(text, FONT_PIN) + LABEL_PAD[0]
    h = FONT_PIN * 0.75 + LABEL_PAD[1]
    quad(ctx, centre, (w, h), (0, 0, 0), 0.7)
    draw_text(ctx, text, FONT_PIN, centre, (1, 1, 1))


def pin_label(ctx, pin):
    if not pin.name.strip():
        return
    w = text_width(pin.name, FONT_PIN) + LABEL_PAD[0]
    centre = (pin.pos[0] + pin.face * (w / 2 + PIN_R + 0.05), pin.pos[1])
    text_label(ctx, pin.name, centre)


_RENDER = {"split_buses": False}  # set per export, so pins match the wire style


def draw_pin(ctx, pin):
    if pin.bits == 1:
        point(ctx, pin.pos, PIN_R, (0, 0, 0))
    else:
        h = PIN_H4 if pin.bits == 4 else PIN_H8
        if _RENDER["split_buses"]:
            h = max(h, pin.bits * LANE)  # tall enough to cover every bit's wire
        quad(ctx, pin.pos, (PIN_R * 2 * 0.95, h), (0, 0, 0))


def wire_col(colour, opts, bit_index=0, bits=1):
    idx = min(max(int(colour), 0), 7)
    c = (STATE_HIGH if opts.lit else STATE_LOW)[idx]
    if bits != 1 and bit_index % 2 == 0:
        c = tuple(min(1, v + 0.01) for v in c)
    return c


def _norm(v):
    m = math.hypot(*v)
    return (v[0] / m, v[1] / m) if m > 1e-9 else (0.0, 0.0)


def _flip(a, b):
    return -1 if a[0] * b[0] + a[1] * b[1] < -0.75 else 1


def _line_intersect(a1, a2, b1, b2):
    d = (a1[0] - a2[0]) * (b1[1] - b2[1]) - (a1[1] - a2[1]) * (b1[0] - b2[0])
    if abs(d) < 1e-12:
        return None
    t = ((a1[0] - b1[0]) * (b1[1] - b2[1]) - (a1[1] - b1[1]) * (b1[0] - b2[0])) / d
    return (a1[0] + t * (a2[0] - a1[0]), a1[1] + t * (a2[1] - a1[1]))


def multibit_layout(pts, nbits, thick):
    """WireLayoutHelper.CreateMultiBitWireLayout."""
    T = 0.925
    n = len(pts)
    out = [[(0.0, 0.0)] * n for _ in range(nbits)]
    dir_prev, sign = (0.0, 0.0), 1
    for i in range(n - 1):
        a, b = pts[i], pts[i + 1]
        d = _norm((b[0] - a[0], b[1] - a[1]))
        perp = (-d[1], d[0])
        if i > 0:
            sign *= _flip(d, dir_prev)
        for bi in range(nbits):
            off = (bi - (nbits - 1) / 2) * thick * 2 * T
            o = (perp[0] * off, perp[1] * off)
            pa = (a[0] + o[0], a[1] + o[1]) if i == 0 else out[bi][i]
            pb = (b[0] + o[0] * sign, b[1] + o[1] * sign)
            if i + 1 < n - 1:
                c = pts[i + 2]
                if (c[0] - b[0]) ** 2 + (c[1] - b[1]) ** 2 > 0.001:
                    dn = _norm((c[0] - b[0], c[1] - b[1]))
                    on = (-dn[1] * off, dn[0] * off)
                    s2 = sign * _flip(d, dn)
                    pn = (c[0] + on[0] * s2, c[1] + on[1] * s2)
                    if abs(d[0] * dn[0] + d[1] * dn[1]) < 0.995:
                        hit = _line_intersect(pa, pb, pn, (pn[0] + dn[0], pn[1] + dn[1]))
                        if hit:
                            pb = hit
            out[bi][i] = pa
            out[bi][i + 1] = pb
        dir_prev = d
    return out


WIRE_CASING = (0.06, 0.06, 0.07)


def draw_wire(ctx, w, opts):
    if getattr(opts, "split_buses", False):
        # Auto layout: every bit is its own wire with a thin dark edge, buses spread slightly
        # apart, so a bus reads as separate wires and each bit can peel off on its own. A bus
        # whose bits all go to the same place is one solid band with a dark centre line instead.
        if w.solid and w.bits > 1:
            polyline(ctx, w.points, BUS_W + 0.03, WIRE_CASING)
            polyline(ctx, w.points, BUS_W, wire_col(w.colour, opts))
            polyline(ctx, w.points, 0.025, darken(wire_col(w.colour, opts), 1.8))
            return
        lanes = [w.points] if w.bits == 1 else multibit_layout(w.points, w.bits, LANE / (2 * 0.925))
        for bi, bpts in enumerate(lanes):
            polyline(ctx, bpts, WIRE_T * 2 + 0.022, WIRE_CASING)
            polyline(ctx, bpts, WIRE_T * 2, wire_col(w.colour, opts))
        return
    if w.bits == 1:
        col = wire_col(w.colour, opts)
        polyline(ctx, w.points, WIRE_T * 2, col)
        if w.junction:
            point(ctx, w.junction, 0.06, col)
    else:
        for bi, bpts in enumerate(multibit_layout(w.points, w.bits, WIRE_T)):
            polyline(ctx, bpts, WIRE_T * 2, wire_col(w.colour, opts, bi, w.bits))


def draw_leaf_chip(ctx, c):
    d = c.desc
    col = col_of(d)
    for p in c.pins:
        if d.get("ChipType") == "Bus" and not p.is_source:
            continue  # bus origin hides its input pin
        draw_pin(ctx, p)
    quad(ctx, c.center, (c.size[0] + OUTLINE, c.size[1] + OUTLINE), chip_outline_col(col))
    quad(ctx, c.center, c.size, col)

    ctype = d.get("ChipType")
    if ctype == "DisplayLED":
        dw = c.size[1] - GRID * 0.5
        quad(ctx, c.center, (dw, dw), (0, 0, 0))
        point(ctx, c.center, dw * 0.38, STATE_LOW[0])
    elif ctype in ("SevenSegmentDisplay", "DisplayRGB", "DisplayDot"):
        dw = min(c.size) - GRID * 2
        quad(ctx, c.center, (dw, dw), (0, 0, 0))

    loc = d.get("NameLocation", NAME_CENTRE)
    if ctype == "Key":
        draw_text(ctx, "K", FONT_CHIP, c.center, chip_name_col(col))
    elif loc != NAME_HIDDEN:
        name = d["Name"]
        if text_width(name, FONT_CHIP) >= c.size[0] - PIN_R * 2.5:
            name = multi_line_name(name)
        if loc == NAME_TOP:
            draw_text(ctx, name, FONT_CHIP, (c.center[0], c.center[1] + c.size[1] / 2 - GRID / 2), chip_name_col(col),
                      valign="top", line_spacing=0.75)
        else:
            draw_text(ctx, name, FONT_CHIP, c.center, chip_name_col(col), line_spacing=0.75)


def frame_colours(col, depth):
    border = col if max(col) > 0.25 else brighten(col, 0.3)
    fill = lerp_col(BG, col, 0.10)
    fill = darken(fill, min(0.12, 0.035 * (depth + 1)))
    return border, fill


def draw_frame(ctx, c, opts, title):
    col = col_of(c.desc)
    border, fill = frame_colours(col, c.depth)
    x0, y0 = c.center[0] - c.size[0] / 2, c.center[1] - c.size[1] / 2
    for p in c.pins:
        draw_pin(ctx, p)
    rgb(ctx, fill)
    ctx.rectangle(x0, y0, c.size[0], c.size[1])
    ctx.fill()
    # title band
    th = getattr(c, "title_h", FRAME_TITLE_H)
    rgb(ctx, border)
    ctx.rectangle(x0, y0 + c.size[1] - th, c.size[0], th)
    ctx.fill()
    # border
    rgb(ctx, border)
    ctx.set_line_width(FRAME_BORDER)
    ctx.set_line_join(cairo.LINE_JOIN_MITER)
    ctx.rectangle(x0, y0, c.size[0], c.size[1])
    ctx.stroke()
    txt_col = (0, 0, 0) if luminance(border) > 0.57 else (1, 1, 1)
    fs = min(FONT_CHIP * 0.95, th * 0.62)
    avail = c.size[0] - 0.3
    if text_width(title, fs) > avail and avail > 0:
        fs = max(0.08, fs * avail / text_width(title, fs))
    draw_text(ctx, title, fs, (x0 + 0.15, y0 + c.size[1] - th / 2), txt_col, halign="left")


def render_scene(ctx, sc, opts):
    for w in sc.wires:
        draw_wire(ctx, w, opts)
    for p, bits, col, *solid in getattr(sc, "dots", []):
        if bits == 1:
            point(ctx, p, 0.06, wire_col(col, opts))
        else:  # bus branch: a small dark-edged bar across the bus, not a blob over its bits
            bw = bundle_width(bits, bool(solid and solid[0]))
            quad(ctx, p, (0.13, bw + 0.05), WIRE_CASING)
            quad(ctx, p, (0.08, bw), wire_col(col, opts))

    for c in sc.chips:
        if c.inner:
            draw_frame(ctx, c, opts, c.name + (f"  ·  {c.label}" if c.label.strip() else ""))
            ctx.save()
            ctx.translate(*c.inner_offset)
            render_scene(ctx, c.inner, opts)
            ctx.restore()
        else:
            draw_leaf_chip(ctx, c)

    # Frame pin -> inner IN/OUT pin. Auto layout: solid wire; original layout: dashed hint line.
    for ln in sc.links:
        if ln.colour is not None:
            # square ends, so a thick bus doesn't spill over the pin's name tag
            if getattr(opts, "split_buses", False):
                draw_wire(ctx, Wire([ln.a, ln.b], ln.bits, ln.colour, None, 0, solid=getattr(ln, "solid", False)), opts)
            else:
                polyline(ctx, [ln.a, ln.b], bundle_width(ln.bits), wire_col(ln.colour, opts), cap=cairo.LINE_CAP_BUTT)
    ctx.save()
    ctx.set_dash([0.06, 0.05])
    for ln in sc.links:
        if ln.colour is None:
            polyline(ctx, [ln.a, ln.b], 0.025, (0.85, 0.85, 0.85), 0.55)
    ctx.restore()

    for p in getattr(sc, "ports", []):
        draw_port(ctx, p, opts)
    for centre, text, col in getattr(sc, "tags", []):
        text_label(ctx, text, centre)

    for d in sc.devpins:
        draw_devpin(ctx, d, opts)

    for c in sc.chips:
        if opts.chip_pin_names and not c.inner:
            for p in c.pins:
                pin_label(ctx, p)
        if c.label.strip() and not c.inner:
            text_label(ctx, c.label, (c.center[0], c.center[1] - c.size[1] / 2 - 0.2))
    if opts.dev_pin_names:
        for d in sc.devpins:
            if d.name.strip():
                text_label(ctx, d.name, devpin_label_placement(d)[0])


def devpin_label_placement(d):
    """Unlike the game (which draws it toward the circuit), put the IN/OUT pin name behind the
    handle, on the outer side, so it never covers wires or chips."""
    w = text_width(d.name, FONT_PIN) + LABEL_PAD[0]
    back = d.handle[0] - d.face * (DEVPIN_HANDLE_W / 2 + 0.06 + w / 2)
    return (back, d.handle[1]), w


def draw_devpin(ctx, d, opts):
    polyline(ctx, [d.state_pos, d.pin.pos], 0.03, (0, 0, 0))
    idx = min(max(int(d.colour), 0), 7)
    state = (STATE_HIGH if opts.lit else STATE_LOW)[idx]
    if d.bits == 1:
        point(ctx, d.state_pos, DEVPIN_R + DEVPIN_OUTLINE, darken(state, 1.25))
        point(ctx, d.state_pos, DEVPIN_R, state)
    else:
        quad(ctx, d.state_pos, d.grid_size, (0, 0, 0))
        inner = (d.grid_size[0] - DEVPIN_OUTLINE, d.grid_size[1] - DEVPIN_OUTLINE)
        tl = (d.state_pos[0] - inner[0] / 2, d.state_pos[1] + inner[1] / 2)
        sq = MB_SQ * 0.9
        for y in range(d.grid_dim[1]):
            for x in range(d.grid_dim[0]):
                quad(ctx, (tl[0] + MB_SQ * (x + 0.5), tl[1] - MB_SQ * (y + 0.5)), (sq, sq), state)
    draw_pin(ctx, d.pin)
    quad(ctx, d.handle, (DEVPIN_HANDLE_W, d.grid_size[1]), DEVPIN_HANDLE_COL)


def wrap_in_root_frame(sc, desc):
    """For the expanded view: put the top-level chip inside its own frame, so the outermost border is the chip."""
    root = Scene(desc)
    c = Chip()
    c.desc, c.name, c.depth = desc, desc.get("Name", ""), -1
    bw, bh = sc.bbox[2] - sc.bbox[0], sc.bbox[3] - sc.bbox[1]
    c.size = (bw + 2 * FRAME_PAD, bh + 2 * FRAME_PAD + FRAME_TITLE_H)
    c.center = ((sc.bbox[0] + sc.bbox[2]) / 2, (sc.bbox[1] + sc.bbox[3]) / 2 + FRAME_TITLE_H / 2)
    c.inner = sc
    c.inner_offset = (0, 0)
    root.chips.append(c)
    hw, hh = c.size[0] / 2 + FRAME_BORDER, c.size[1] / 2 + FRAME_BORDER
    root.bbox = [c.center[0] - hw, c.center[1] - hh, c.center[0] + hw, c.center[1] + hh]
    return root


def count_frames(sc):
    return sum(1 + count_frames(c.inner) for c in sc.chips if c.inner)


# ---------------------------------------------------------------------------
# Auto layout: re-places the parts in columns (layered / Sugiyama-style) and routes the wires
# horizontally and vertically through the channels between the columns.
# ---------------------------------------------------------------------------
AUTO_PAD = 0.22
AUTO_TITLE_H = 0.36
NODE_GAP = 0.3
DUMMY_GAP = 0.1
CHANNEL_MARGIN = 0.16
MIN_CHANNEL = 0.36
TRACK_SEP = 0.12
LANE = 0.06  # spacing of the bits in a bus (a little wider than the game's, so each bit stands apart)
FOLD_MIN_W = 14.0
BAND_GAP = 0.8


BUS_W = 0.16  # a bus whose bits all travel together: drawn as one solid band


def bundle_width(bits, solid=False):
    if bits == 1:
        return WIRE_T * 2
    return BUS_W if solid else bits * LANE


def track_width(bits, solid=False):
    return max(0.13, bundle_width(bits, solid) + 0.09)


class Port:
    """Compact stand-in for an IN/OUT pin inside an opened (expanded) chip."""

    def __init__(self, name, x0, x1, y, bits, colour, is_input, show_name):
        self.name, self.x0, self.x1, self.y = name, x0, x1, y
        self.bits, self.colour, self.is_input, self.show_name = bits, colour, is_input, show_name

    @property
    def pin_pos(self):
        return (self.x1, self.y) if self.is_input else (self.x0, self.y)

    @property
    def outer_pos(self):
        return (self.x0, self.y) if self.is_input else (self.x1, self.y)


class _Elem:
    __slots__ = ("kind", "w", "h", "payload", "layer", "y", "pins", "net", "bits", "orig", "idx", "atts", "uid")

    def __init__(self, kind, w, h, payload=None):
        self.kind, self.w, self.h, self.payload = kind, w, h, payload
        self.layer, self.y, self.idx = 0, 0.0, 0
        self.pins = {}  # pin key -> (side 'in'/'out', x_rel, y_off_from_top, bits, colour, name)
        self.net, self.bits, self.orig = None, 1, (0.0, 0.0)
        self.atts = []  # (group list, own attachment)


class _Att:
    __slots__ = ("elem", "side", "key", "net", "lane", "fan")

    def __init__(self, elem, side, key, net):
        self.elem, self.side, self.key, self.net = elem, side, key, net
        self.lane = 0.0  # offset from the pin centre when a bus fans out into separate wires
        self.fan = 0.0  # height of the whole fan at this pin (0 when the wire isn't part of one)

    def yoff(self):
        return self.elem.pins[self.key][2] if self.key is not None else self.elem.h / 2

    def frac(self):
        return self.yoff() / self.elem.h if self.elem.h > 0 else 0.5

    def y(self):
        return self.elem.y + self.yoff() + self.lane


def _isotonic_place(elems, desired, weights, gap_fn):
    """Place elems top-to-bottom in their given order as close as possible to the desired tops
    (weighted least squares), while keeping them from overlapping. Pool-adjacent-violators."""
    n = len(elems)
    off = [0.0] * n
    for i in range(1, n):
        off[i] = off[i - 1] + elems[i - 1].h + gap_fn(elems[i - 1], elems[i])
    blocks = []  # [value, weight, count]
    for i in range(n):
        blocks.append([desired[i] - off[i], weights[i], 1])
        while len(blocks) > 1 and blocks[-2][0] > blocks[-1][0]:
            v2, w2, c2 = blocks.pop()
            v1, w1, c1 = blocks.pop()
            blocks.append([(v1 * w1 + v2 * w2) / (w1 + w2), w1 + w2, c1 + c2])
    i = 0
    for v, w, c in blocks:
        for _ in range(c):
            elems[i].y = v + off[i]
            i += 1


def _gap(a, b):
    return DUMMY_GAP if a.kind == "dummy" or b.kind == "dummy" else NODE_GAP


def build_auto_scene(lib, desc, opts, depth=0, stack=frozenset(), cache=None, root=True):
    if cache is None:
        cache = {}
    stack = stack | {desc.get("Name")}
    sc = Scene(desc)
    sc.ports, sc.dots, sc.ports_by_id = [], [], {}
    elems = []
    pin_of = {}
    converters = {}  # sub-chip id -> description, for the bit splitters/mergers being dissolved

    # ---- elements: sub-chips (leaf or opened), and the chip's own IN/OUT pins ----
    for s in desc.get("SubChips") or []:
        cd = lib.get(s["Name"]) or placeholder(s["Name"])
        if opts.dissolve_converters and cd.get("ChipType") == "BitConversion":
            converters[s["ID"]] = cd
            continue
        osize = vec(cd["Size"])
        inner = None
        if (opts.expand and is_custom(cd) and (cd.get("SubChips") or cd.get("Wires"))
                and (opts.depth is None or depth < opts.depth)
                and s["Name"] not in opts.collapse and s["Name"] not in stack):
            ck = (s["Name"], None if opts.depth is None else opts.depth - depth - 1)
            if ck not in cache:
                cache[ck] = build_auto_scene(lib, cd, opts, depth + 1, stack, cache, root=False)
            inner = cache[ck]
        if inner:
            body = (inner.bbox[2] - inner.bbox[0] + 2 * AUTO_PAD,
                    inner.bbox[3] - inner.bbox[1] + 2 * AUTO_PAD + AUTO_TITLE_H)
        else:
            body = osize
        label = (s.get("Label") or "").strip()
        lab_h = 0.4 if (label and not inner) else 0.0
        e = _Elem("chip", body[0] + 2 * PIN_R, body[1] + OUTLINE + lab_h, (s, cd, body, inner))
        e.orig = vec(s["Position"])
        out_cols = {ci["PinID"]: ci["PinColour"] for ci in (s.get("OutputPinColourInfo") or [])}
        cy = OUTLINE / 2 + body[1] / 2
        for is_input, plist in ((True, cd.get("InputPins") or []), (False, cd.get("OutputPins") or [])):
            face = -1 if is_input else 1
            xr = PIN_R + body[0] / 2 + pin_x_offset(body[0], face)
            default = [cy - ly for ly in pin_layout_y([p["BitCount"] for p in plist], body[1])]
            for p, yo in zip(plist, default):
                if inner and p["ID"] in inner.ports_by_id:
                    yo = OUTLINE / 2 + AUTO_TITLE_H + AUTO_PAD + inner.bbox[3] - inner.ports_by_id[p["ID"]].y
                key = (s["ID"], p["ID"])
                colour = 0 if is_input else out_cols.get(p["ID"], p.get("Colour", 0) or 0)
                e.pins[key] = ("in" if is_input else "out", xr, yo, p["BitCount"], colour, p.get("Name", ""))
                pin_of[key] = e
        elems.append(e)

    for is_input, plist in ((True, desc.get("InputPins") or []), (False, desc.get("OutputPins") or [])):
        for p in plist:
            bits, name, colour = p.get("BitCount", 1), p.get("Name", ""), p.get("Colour", 0) or 0
            show = opts.dev_pin_names and bool(name.strip())
            lw = text_width(name, FONT_PIN) + LABEL_PAD[0] if show else 0.0
            if root:
                dist = GRID * (6 if bits in (1, 4) else 9)
                h = DEVPIN_R * 2 + DEVPIN_OUTLINE * 2 if bits == 1 else 2 * MB_SQ + DEVPIN_OUTLINE
                lead = (lw + 0.06) if show else 0.0
                if is_input:
                    hx = lead + DEVPIN_HANDLE_W / 2
                    px = hx + dist
                    w = px + PIN_R
                else:
                    px = PIN_R
                    hx = px + dist
                    w = hx + DEVPIN_HANDLE_W / 2 + lead
                e = _Elem("devpin", w, h, (p, is_input, hx))
            else:
                w = (lw + 0.3) if show else 0.2
                h = max(0.25, PIN_H1 if bits == 1 else PIN_H4 if bits == 4 else PIN_H8)
                px = w if is_input else 0.0
                e = _Elem("port", w, h, (p, is_input, show))
            e.orig = vec(p.get("Position"))
            key = (p["ID"], 0)
            e.pins[key] = ("out" if is_input else "in", px, h / 2, bits, colour, name)
            pin_of[key] = e
            elems.append(e)

    # ---- nets: (source pin, first bit, width) -> the pins it drives ----
    # Bit splitters and mergers are dissolved: every wire is traced back through them to the
    # pin that really drives it, so a bus fans straight out into its single-bit wires.
    def addr(a):
        return (a["PinOwnerID"], a["PinID"])

    conv_driver = {}
    for w in desc.get("Wires") or []:
        tk = addr(w["TargetPinAddress"])
        if tk[0] in converters:
            conv_driver[tk] = addr(w["SourcePinAddress"])

    def resolve(sk, lo, width, guard=0):
        """Which (pin, first bit, width) pieces really drive bits lo..lo+width of pin sk.
        Returns (pin, first bit, width, offset into the requested range)."""
        cd = converters.get(sk[0])
        if cd is None:
            return [(sk, lo, width, 0)]
        ins, outs = cd.get("InputPins") or [], cd.get("OutputPins") or []
        idx = next((i for i, p in enumerate(outs) if p["ID"] == sk[1]), None)
        if guard > 32 or idx is None or not ins:
            return []
        in_w, out_w = ins[0]["BitCount"], outs[0]["BitCount"]
        b0 = (len(outs) - 1 - idx) * out_w + lo  # first pin carries the highest bits
        b1 = b0 + width
        res = []
        for j, p in enumerate(ins):
            s0 = (len(ins) - 1 - j) * in_w
            a0, a1 = max(b0, s0), min(b1, s0 + in_w)
            src = conv_driver.get((sk[0], p["ID"]))
            if a0 < a1 and src is not None:
                for bk, blo, bw, off in resolve(src, a0 - s0, a1 - a0, guard + 1):
                    res.append((bk, blo, bw, off + a0 - b0))
        res.sort(key=lambda r: r[3])
        merged = []  # rejoin neighbouring bits of the same bus (e.g. a split followed by a merge)
        for r in res:
            m = merged[-1] if merged else None
            if m and m[0] == r[0] and m[1] + m[2] == r[1] and m[3] + m[2] == r[3]:
                merged[-1] = (m[0], m[1], m[2] + r[2], m[3])
            else:
                merged.append(r)
        return merged

    nets = {}
    tgt_bit = {}  # (net, target pin) -> lowest bit of the target pin that the net drives
    for w in desc.get("Wires") or []:
        sk, tk = addr(w["SourcePinAddress"]), addr(w["TargetPinAddress"])
        et = pin_of.get(tk)
        if et is None or et.pins[tk][0] != "in":
            continue
        for bk, blo, bw, off in resolve(sk, 0, et.pins[tk][3]):
            es = pin_of.get(bk)
            if es is None or es.pins[bk][0] != "out":
                continue
            ts = nets.setdefault((bk, blo, bw), [])
            if tk not in ts:
                ts.append(tk)
                tgt_bit[((bk, blo, bw), tk)] = off
    nets = {k: sorted(v, key=str) for k, v in nets.items()}

    # ---- layering ----
    for i, e in enumerate(elems):
        e.uid = i
    chips = [e for e in elems if e.kind == "chip"]
    inputs = [e for e in elems if e.kind != "chip" and next(iter(e.pins.values()))[0] == "out"]
    outputs = [e for e in elems if e.kind != "chip" and e not in inputs]
    edges = [(pin_of[n[0]], pin_of[t]) for n, ts in nets.items() for t in ts]
    chip_succ = {e.uid: [] for e in chips}
    for a, b in edges:
        if a.kind == "chip" and b.kind == "chip":
            chip_succ[a.uid].append(b)

    # break cycles (feedback loops) with a DFS, preferring left-to-right in the original layout
    state, back = {}, set()
    starts = sorted(chips, key=lambda e: (e.orig[0], -e.orig[1]))
    fed_by_input = {b.uid for a, b in edges if a.kind != "chip" and b.kind == "chip"}
    starts.sort(key=lambda e: e.uid not in fed_by_input)
    for root_e in starts:
        if root_e.uid in state:
            continue
        state[root_e.uid] = 1
        stk = [(root_e, iter(chip_succ[root_e.uid]))]
        while stk:
            u, it = stk[-1]
            nxt = next(it, None)
            if nxt is None:
                state[u.uid] = 2
                stk.pop()
                continue
            st = state.get(nxt.uid)
            if st == 1:
                back.add((u.uid, nxt.uid))
            elif st is None:
                state[nxt.uid] = 1
                stk.append((nxt, iter(chip_succ[nxt.uid])))

    dag_succ = {e.uid: [] for e in chips}
    dag_pred = {e.uid: [] for e in chips}
    for a in chips:
        for b in chip_succ[a.uid]:
            if (a.uid, b.uid) not in back:
                dag_succ[a.uid].append(b)
                dag_pred[b.uid].append(a)
    indeg = {e.uid: len(dag_pred[e.uid]) for e in chips}
    queue = [e for e in chips if indeg[e.uid] == 0]
    topo = []
    while queue:
        u = queue.pop()
        topo.append(u)
        for v in dag_succ[u.uid]:
            indeg[v.uid] -= 1
            if indeg[v.uid] == 0:
                queue.append(v)
    for e in chips:
        e.layer = 1
    for u in topo:
        for v in dag_succ[u.uid]:
            v.layer = max(v.layer, u.layer + 1)

    # Slide chips toward the side where most of their wires go (shorter wires, fewer pass-throughs)
    n_in = {e.uid: 0 for e in chips}
    n_out = {e.uid: 0 for e in chips}
    for a, b in edges:
        if a.kind == "chip" and b.kind == "chip" and (a.uid, b.uid) in back:
            continue
        if b.kind == "chip":
            n_in[b.uid] += 1
        if a.kind == "chip":
            n_out[a.uid] += 1
    max_layer = max((e.layer for e in chips), default=1)
    for _ in range(40):
        changed = False
        for e in chips:
            lo = max([p.layer for p in dag_pred[e.uid]] + [0]) + 1
            hi = min([s.layer for s in dag_succ[e.uid]] + [max_layer + 1]) - 1
            if n_out[e.uid] > n_in[e.uid] and e.layer < hi:
                e.layer += 1
                changed = True
            elif n_in[e.uid] > n_out[e.uid] and e.layer > lo:
                e.layer -= 1
                changed = True
        if not changed:
            break
    used = sorted({e.layer for e in chips})
    remap = {l: i + 1 for i, l in enumerate(used)}
    for e in chips:
        e.layer = remap[e.layer]
    last = len(used) + 1
    for e in inputs:
        e.layer = 0
    for e in outputs:
        e.layer = last
    n_layers = last + 1

    layers = [[] for _ in range(n_layers)]
    for e in elems:
        layers[e.layer].append(e)

    # ---- dummies (pass-throughs) and channel attachments ----
    groups = {}  # (channel, net) -> [ _Att ]

    def attach(c, net, elem, side, key):
        g = groups.setdefault((c, net), [])
        a = _Att(elem, side, key, net)
        g.append(a)
        elem.atts.append((g, a))

    def make_dummy(layer, net, bits, orig):
        d = _Elem("dummy", 0.0, bundle_width(bits) + 0.02)
        d.layer, d.net, d.bits, d.orig = layer, net, bits, orig
        layers[layer].append(d)
        return d

    for net, tks in nets.items():
        sk, bits = net[0], net[2]
        se = pin_of[sk]
        a = se.layer
        fwd = [t for t in tks if pin_of[t].layer > a]
        bwd = [t for t in tks if pin_of[t].layer <= a]
        attach(a, net, se, "L", sk)
        if fwd:
            for l in range(a + 1, max(pin_of[t].layer for t in fwd)):
                d = make_dummy(l, net, bits, se.orig)
                attach(l - 1, net, d, "R", None)
                attach(l, net, d, "L", None)
        if bwd:
            for l in range(min(pin_of[t].layer for t in bwd), a + 1):
                d = make_dummy(l, net, bits, se.orig)
                attach(l, net, d, "L", None)
                attach(l - 1, net, d, "R", None)
        for t in tks:
            te = pin_of[t]
            attach(te.layer - 1, net, te, "R", t)

    # ---- bus fan-outs: a multi-bit pin whose bits go to different places ----
    # Its wires leave (or arrive) as parallel lanes, one per bit in bit order (highest bit on
    # top, as the game lays out bus pins), and each lane peels off on its own.
    fan_groups = {}
    for (c, net), g in groups.items():
        for a in g:
            if a.key is not None:
                fan_groups.setdefault((c, a.side, a.key), []).append(a)
    fan_groups = {k: v for k, v in fan_groups.items()
                  if v[0].elem.pins[k[2]][3] > 1 and (len(v) > 1 or v[0].net[2] < v[0].elem.pins[k[2]][3])}
    for (c, side, key), v in fan_groups.items():
        pbits = v[0].elem.pins[key][3]
        for a in v:
            a.fan = pbits * LANE
            first = a.net[1] if side == "L" else tgt_bit.get((a.net, key), 0)
            centre_bit = first + (a.net[2] - 1) / 2
            a.lane = ((pbits - 1) / 2 - centre_bit) * LANE  # layout y runs down: high bits above
    fanned = {a.net for v in fan_groups.values() for a in v}

    def solid(net):
        return net[2] > 1 and net not in fanned

    for L in layers:
        for d in L:
            if d.kind == "dummy":
                d.h = bundle_width(d.bits, solid(d.net)) + 0.02

    # ---- ordering within each column (barycentre sweeps, keep the best) ----
    for L in layers:
        L.sort(key=lambda e: -e.orig[1])

    def reindex(L):
        for i, e in enumerate(L):
            e.idx = i

    for L in layers:
        reindex(L)

    def pos(att):
        return att.elem.idx + att.frac()

    def crossings():
        total = 0
        by_chan = {}
        for (c, net), g in groups.items():
            ls = [pos(x) for x in g if x.side == "L"]
            rs = [pos(x) for x in g if x.side == "R"]
            by_chan.setdefault(c, []).extend((net, l, r) for l in ls for r in rs)
        for segs in by_chan.values():
            for i in range(len(segs)):
                n1, l1, r1 = segs[i]
                for j in range(i + 1, len(segs)):
                    n2, l2, r2 = segs[j]
                    if n1 != n2 and (l1 - l2) * (r1 - r2) < 0:
                        total += 1
        return total

    best = [list(L) for L in layers]
    best_c = crossings()
    for it in range(12):
        order = range(n_layers) if it % 2 == 0 else range(n_layers - 1, -1, -1)
        for li in order:
            L = layers[li]
            keys = {}
            for e in L:
                vals = [pos(o) - own.frac() + 0.5 for g, own in e.atts for o in g if o.elem is not e]
                keys[id(e)] = (sum(vals) / len(vals)) if vals else e.idx
            L.sort(key=lambda e: keys[id(e)])
            reindex(L)
        c = crossings()
        if c < best_c:
            best_c, best = c, [list(L) for L in layers]
    layers[:] = best
    for L in layers:
        reindex(L)

    # ---- vertical positions: line pins up with what they connect to ----
    for L in layers:
        y = 0.0
        for e in L:
            e.y = y
            y += e.h + NODE_GAP
        shift = -y / 2
        for e in L:
            e.y += shift
    for it in range(30):
        order = range(n_layers) if it % 2 == 0 else range(n_layers - 1, -1, -1)
        for li in order:
            L = layers[li]
            if not L:
                continue
            desired, weights = [], []
            for e in L:
                vals = [o.y() - own.yoff() for g, own in e.atts for o in g if o.elem is not e]
                desired.append(sum(vals) / len(vals) if vals else e.y)
                weights.append(len(vals) if vals else 0.05)
            _isotonic_place(L, desired, weights, _gap)

    # ---- straighten: nudge parts so more wires run dead level (no small jogs) ----
    def level_pairs(e):
        """(own, other) for each plain point-to-point wire of e, the ones that can be straight."""
        return [(own, o) for g, own in e.atts if len(g) == 2 and not any(a.fan for a in g)
                for o in g if o is not own]

    def n_level(e):
        return sum(1 for own, o in level_pairs(e) if abs(own.y() - o.y()) < 1e-6)

    for it in range(6):
        moved = False
        order = range(n_layers) if it % 2 == 0 else range(n_layers - 1, -1, -1)
        for li in order:
            L = layers[li]
            for i, e in enumerate(L):
                lo = L[i - 1].y + L[i - 1].h + _gap(L[i - 1], e) if i > 0 else -float("inf")
                hi = L[i + 1].y - _gap(e, L[i + 1]) - e.h if i + 1 < len(L) else float("inf")
                y0, best = e.y, (n_level(e), 0.0)
                cands = {round(o.y() - (own.y() - e.y), 6) for own, o in level_pairs(e)}
                best_y = y0
                for y in cands:
                    if not (lo - 1e-9 <= y <= hi + 1e-9) or abs(y - y0) > 1.5:
                        continue
                    e.y = y
                    score = (n_level(e), -abs(y - y0))
                    if score > best:
                        best, best_y = score, y
                    e.y = y0
                if best_y != y0:
                    e.y, moved = best_y, True
        if not moved:
            break
    top = min((e.y for e in elems + [d for L in layers for d in L]), default=0.0)
    for L in layers:
        for e in L:
            e.y -= top

    # ---- channel routing: give every vertical wire run its own track ----
    chan_nets = {}
    for (c, net), g in groups.items():
        chan_nets.setdefault(c, []).append((net, g))
    tracks_of = {}  # (c, net) -> track index, or None when the wire is a straight line
    chan_track_w = {}
    for c, items in chan_nets.items():
        need = {}
        at_y = {}
        for net, g in items:
            ys = [a.y() for a in g]
            sides = {a.side for a in g}
            if len(g) == 2 and len(sides) == 2 and abs(ys[0] - ys[1]) < 1e-6 and not any(a.fan for a in g):
                tracks_of[(c, net)] = None
                continue
            # leave room for whichever fan lane each pin's wire ends up on
            need[net] = (min(a.y() - a.fan / 2 for a in g), max(a.y() + a.fan / 2 for a in g), net_bits(g))
            for a in g:
                at_y.setdefault(round(a.y(), 5), []).append((net, a.side))
        before = {n: set() for n in need}
        for lst in at_y.values():
            for n1, s1 in lst:
                for n2, s2 in lst:
                    if n1 != n2 and s1 == "L" and s2 == "R" and n1 in need and n2 in need:
                        before[n2].add(n1)  # n1's track must be left of n2's
        assigned = {}
        remaining = set(need)
        widths = []
        while remaining:
            avail = [n for n in remaining if before[n] <= set(assigned)]
            if not avail:  # cyclic constraint: just break it
                avail = [min(remaining, key=lambda n: len(before[n] - set(assigned)))]
            avail.sort(key=lambda n: need[n][0])
            t = len(widths)
            last_hi = -float("inf")
            w = 0.0
            for n in avail:
                lo, hi, bits = need[n]
                pad = TRACK_SEP + bundle_width(bits, solid(n)) / 2
                if lo > last_hi + pad:
                    assigned[n] = t
                    remaining.discard(n)
                    last_hi = hi + bundle_width(bits, solid(n)) / 2
                    w = max(w, track_width(bits, solid(n)))
            widths.append(w)
        for n, t in assigned.items():
            tracks_of[(c, n)] = t
        chan_track_w[c] = widths

    col_w = [max([e.w for e in L if e.kind != "dummy"] + [0.0]) for L in layers]
    chan_w = [max(MIN_CHANNEL, 2 * CHANNEL_MARGIN + sum(chan_track_w.get(c, []))) for c in range(n_layers - 1)]
    col_x = [0.0] * n_layers
    for i in range(1, n_layers):
        col_x[i] = col_x[i - 1] + col_w[i - 1] + chan_w[i - 1]

    # ---- wrap very wide layouts into stacked rows ("bands") ----
    total_w = col_x[-1] + col_w[-1]
    total_h = max((e.y + e.h for L in layers for e in L), default=0.0)
    band_of = [0] * n_layers
    cuts = []
    if opts.aspect and n_layers > 3 and total_w > FOLD_MIN_W and total_h > 0 and total_w / total_h > opts.aspect * 1.5:
        # Every wire crossing a row break has to loop around through the gap between rows, which
        # costs space and clarity, so a wrap only wins when it improves the shape by enough.
        n_in_chan, w_in_chan = {}, {}
        for (c, net) in groups:
            n_in_chan[c] = n_in_chan.get(c, 0) + 1
            w_in_chan[c] = w_in_chan.get(c, 0.0) + track_width(net[2], solid(net))

        def pick_cuts(k):
            used = set()
            for j in range(1, k):
                target = total_w * j / k
                cands = [c for c in range(1, n_layers - 2) if c not in used] or [c for c in range(n_layers - 1) if c not in used]
                if not cands:
                    break
                used.add(min(cands, key=lambda c: abs(col_x[c] + col_w[c] + chan_w[c] / 2 - target) / total_w * 10
                                                  + 0.12 * n_in_chan.get(c, 0)))
            return sorted(used)

        best_cuts, best_score = [], abs(math.log(total_w / total_h / opts.aspect))
        for k in range(2, min(12, n_layers - 2) + 1):
            ck = pick_cuts(k)
            if len(ck) != k - 1:
                break
            wk = total_w / k + 1.5 + 2 * max(w_in_chan.get(c, 0.0) for c in ck)
            hk = k * total_h + sum(BAND_GAP + w_in_chan.get(c, 0.0) for c in ck)
            n_cross = sum(n_in_chan.get(c, 0) for c in ck)
            # share of the picture the looping wires would take up (each runs about a row's width)
            loop_share = sum(w_in_chan.get(c, 0.0) for c in ck) / (k * total_h)
            score = abs(math.log(wk / hk / opts.aspect)) + 5.0 * loop_share + 0.02 * n_cross
            if score < best_score:
                best_cuts, best_score = ck, score
        cuts = best_cuts
    b = 0
    for li in range(n_layers):
        band_of[li] = b
        if b < len(cuts) and li == cuts[b]:
            b += 1
    n_bands = len(cuts) + 1
    cut_set = set(cuts)

    # Row breaks: wires crossing one are routed whole. They go down a track at the right edge of
    # their row, back left along their own lane in the gap between rows, and down a track into
    # the next row. Taking the lowest wire first for the innermost exit track, the top gap lane and
    # the leftmost entry track keeps the crossing wires from overlapping or crossing each other.
    cut_exit, cut_entry, cut_gap = {}, {}, {}  # (channel, net) -> offset inside that region
    exit_w, entry_w, gap_h = {}, {}, {}
    cut_groups = {}
    for (c, net), g in groups.items():
        if c in cut_set:
            cut_groups.setdefault(c, []).append((net, g))

    def stack_tracks(c, seq, store):
        pos = CHANNEL_MARGIN
        for n, g in seq:
            tw = track_width(n[2], solid(n))
            store[(c, n)] = pos + tw / 2
            pos += tw
        return pos + CHANNEL_MARGIN

    for c, items in cut_groups.items():
        lefts = sorted([it for it in items if all(a.side == "L" for a in it[1])], key=lambda it: min(a.y() for a in it[1]))
        rights = sorted([it for it in items if all(a.side == "R" for a in it[1])], key=lambda it: min(a.y() for a in it[1]))
        cross = [it for it in items if it not in lefts and it not in rights]
        cross.sort(key=lambda it: -min(a.y() for a in it[1] if a.side == "L"))
        exit_w[c] = max(MIN_CHANNEL, stack_tracks(c, lefts + cross, cut_exit))
        entry_w[c] = max(MIN_CHANNEL, stack_tracks(c, cross + rights, cut_entry))
        gap_h[c] = max(BAND_GAP, stack_tracks(c, cross, cut_gap))

    xshift, yshift = [0.0] * n_bands, [0.0] * n_bands
    gap_top = {}
    cum = 0.0
    for bi in range(n_bands):
        lay = [li for li in range(n_layers) if band_of[li] == bi]
        xshift[bi] = -col_x[lay[0]] + (entry_w.get(cuts[bi - 1], MIN_CHANNEL) if bi > 0 else 0.0)
        es = [e for li in lay for e in layers[li]]
        ymin = min((e.y for e in es), default=0.0)
        ymax = max((e.y + e.h for e in es), default=0.0)
        yshift[bi] = cum - ymin
        cum += ymax - ymin
        if bi < len(cuts):
            gap_top[cuts[bi]] = cum
            cum += gap_h.get(cuts[bi], BAND_GAP)

    def X(layer, x):
        return x + xshift[band_of[layer]]

    def Y(layer, y):
        return -(y + yshift[band_of[layer]])

    def track_x(c, t):
        ws = chan_track_w[c]
        return X(c, col_x[c] + col_w[c] + CHANNEL_MARGIN + sum(ws[:t]) + ws[t] / 2)

    def att_xy(c, a):
        layer = a.elem.layer
        y = Y(layer, a.y())
        if a.key is not None:
            return (X(layer, col_x[layer] + a.elem.pins[a.key][1]), y)
        return (X(layer, col_x[layer] + col_w[layer]), y) if a.side == "L" else (X(layer, col_x[layer]), y)

    # ---- emit the scene (world coordinates, y up) ----
    sc.tags = []
    for L in layers:
        for e in L:
            x0 = X(e.layer, col_x[e.layer])
            if e.kind == "dummy":
                y = Y(e.layer, e.y + e.h / 2)
                col = pin_colour_of(pin_of, e.net)
                sc.wires.append(Wire([(x0, y), (x0 + col_w[e.layer], y)], e.bits, col, None, 0, e.net))
            elif e.kind == "chip":
                s, cd, body, inner = e.payload
                c = Chip()
                c.desc, c.name, c.label, c.depth = cd, s["Name"], s.get("Label") or "", depth
                c.center = (x0 + PIN_R + body[0] / 2, Y(e.layer, e.y + OUTLINE / 2 + body[1] / 2))
                c.size = body
                c.inner = inner
                c.title_h = AUTO_TITLE_H
                for key, (side, xr, yo, bits, colour, name) in e.pins.items():
                    pp = (x0 + xr, Y(e.layer, e.y + yo))
                    c.pins.append(Pin(pp, pp, bits, side == "out", colour, name, 1 if side == "out" else -1))
                if inner:
                    left = c.center[0] - body[0] / 2
                    top_y = c.center[1] + body[1] / 2
                    c.inner_offset = (left + AUTO_PAD - inner.bbox[0], top_y - AUTO_TITLE_H - AUTO_PAD - inner.bbox[3])
                    for pin, p in zip(c.pins, list(cd.get("InputPins") or []) + list(cd.get("OutputPins") or [])):
                        port = inner.ports_by_id.get(p["ID"])
                        if port:
                            ox, oy = port.outer_pos
                            ln = Link(pin.pos, (ox + c.inner_offset[0], oy + c.inner_offset[1]),
                                      colour=pin.colour if pin.is_source else port.colour, bits=pin.bits)
                            ln.solid = port.solid
                            sc.links.append(ln)
                sc.chips.append(c)
            elif e.kind == "devpin":
                p, is_input, hx = e.payload
                handle = (x0 + hx, Y(e.layer, e.y + e.h / 2))
                dp = DevPin(p, is_input, handle, handle)
                sc.devpins.append(dp)
                sc.devpins_by_id[dp.id] = dp
            else:  # port
                p, is_input, show = e.payload
                port = Port(p.get("Name", ""), x0, x0 + e.w, Y(e.layer, e.y + e.h / 2), p.get("BitCount", 1),
                            p.get("Colour", 0) or 0, is_input, show)
                port.solid = port.bits > 1 and all(solid(own.net) for _, own in e.atts)
                sc.ports.append(port)
                sc.ports_by_id[p["ID"]] = port

    pin_turns = {}  # pin key -> [(x where its wire turns, net, pin position)], for bus fan-outs

    def note_turns(atts_pts, tx):
        for a, p in atts_pts:
            if a.key is not None:
                pin_turns.setdefault(a.key, []).append((tx, a.net, p))

    def trunk(tx, atts_pts, bits, col, through=None):
        """A vertical run at tx joining horizontal stubs from each attachment; `through` is an
        extra y it has to reach (where it turns into a row gap)."""
        ys = [p[1] for _, p in atts_pts] + ([through] if through is not None else [])
        ymin, ymax = min(ys), max(ys)
        if ymax - ymin > 1e-9:
            sc.wires.append(Wire([(tx, ymin), (tx, ymax)], bits, col, None, 0, atts_pts[0][0].net))
        count = {}
        for y in ys:
            count[round(y, 5)] = count.get(round(y, 5), 0) + 1
        for _, (x, y) in atts_pts:
            sc.wires.append(Wire(_dedupe([(x, y), (tx, y)]), bits, col, None, 0, atts_pts[0][0].net))
            if ymin + 1e-6 < y < ymax - 1e-6 or count[round(y, 5)] > 1:
                sc.dots.append(((tx, y), bits, col, solid(atts_pts[0][0].net)))
        note_turns(atts_pts, tx)

    for (c, net), g in groups.items():
        bits = net[2]
        col = pin_colour_of(pin_of, net)
        ap = [(a, att_xy(c, a)) for a in g]
        if c in cut_set:
            left = [(a, p) for a, p in ap if a.side == "L"]
            right = [(a, p) for a, p in ap if a.side == "R"]
            xt = X(c, col_x[c] + col_w[c]) + cut_exit[(c, net)] if left else None
            xs = X(c + 1, col_x[c + 1]) - entry_w[c] + cut_entry[(c, net)] if right else None
            if left and right:
                gy = -(gap_top[c] + cut_gap[(c, net)])
                if len(left) == 1 and len(right) == 1:
                    (xl, yl), (xr, yr) = left[0][1], right[0][1]
                    sc.wires.append(Wire(_dedupe([(xl, yl), (xt, yl), (xt, gy), (xs, gy), (xs, yr), (xr, yr)]),
                                         bits, col, None, 0, net))
                    note_turns(left, xt)
                    note_turns(right, xs)
                else:
                    trunk(xt, left, bits, col, through=gy)
                    sc.wires.append(Wire([(xt, gy), (xs, gy)], bits, col, None, 0, net))
                    trunk(xs, right, bits, col, through=gy)
            elif left:
                trunk(xt, left, bits, col)
            else:
                trunk(xs, right, bits, col)
            continue
        t = tracks_of.get((c, net))
        if t is None:
            sc.wires.append(Wire([p for _, p in ap], bits, col, None, 0, net))
            for (a, p), (_, q) in ((ap[0], ap[1]), (ap[1], ap[0])):
                note_turns([(a, p)], q[0])
            continue
        tx = track_x(c, t)
        if len(ap) == 2:
            (x1, y1), (x2, y2) = ap[0][1], ap[1][1]
            sc.wires.append(Wire(_dedupe([(x1, y1), (tx, y1), (tx, y2), (x2, y2)]), bits, col, None, 0, net))
            note_turns(ap, tx)
            continue
        trunk(tx, ap, bits, col)


    sc.wires = join_wire_pieces(sc.wires)
    for w in sc.wires:
        w.solid = w.net is not None and solid(w.net)
    sc.bbox = scene_bbox(sc, opts)
    extra = [(p.x0, p.y) for p in sc.ports] + [(p.x1, p.y) for p in sc.ports]
    if extra:
        xs = [p[0] for p in extra]
        ys = [p[1] for p in extra]
        sc.bbox = [min(sc.bbox[0], min(xs)), min(sc.bbox[1], min(ys) - 0.15),
                   max(sc.bbox[2], max(xs)), max(sc.bbox[3], max(ys) + 0.15)]
    return sc


def join_wire_pieces(wires):
    """Join a net's wire pieces that meet end to end (with no branch there) into one line, so
    each bit is drawn as a single continuous wire with clean corners."""
    out, by_net = [], {}
    for w in wires:
        if w.net is None:
            out.append(w)
        else:
            by_net.setdefault(w.net, []).append(w)

    def k(p):
        return (round(p[0], 5), round(p[1], 5))

    for net, ws in by_net.items():
        polys = [list(w.points) for w in ws]
        while True:
            ends, inner = {}, set()
            for pl in polys:
                for p in (pl[0], pl[-1]):
                    ends[k(p)] = ends.get(k(p), 0) + 1
                inner.update(k(p) for p in pl[1:-1])
            done = False
            for i, a in enumerate(polys):
                for j, b in enumerate(polys):
                    if i == j:
                        continue
                    for aa in (a, a[::-1]):
                        for bb in (b, b[::-1]):
                            kk = k(aa[-1])
                            if kk == k(bb[0]) and ends[kk] == 2 and kk not in inner:
                                polys[i] = aa + bb[1:]
                                del polys[j]
                                done = True
                                break
                        if done:
                            break
                    if done:
                        break
                if done:
                    break
            if not done:
                break
        for pl in polys:
            out.append(Wire(_dedupe(pl), ws[0].bits, ws[0].colour, None, 0, net))
    return out


def net_bits(g):
    return g[0].net[2]


def pin_colour_of(pin_of, net):
    return pin_of[net[0]].pins[net[0]][4]


def _dedupe(pts):
    out = [pts[0]]
    for p in pts[1:]:
        if abs(p[0] - out[-1][0]) > 1e-9 or abs(p[1] - out[-1][1]) > 1e-9:
            out.append(p)
    if len(out) == 1:
        out.append(out[0])
    return out


def draw_port(ctx, p, opts):
    col = wire_col(p.colour, opts)
    draw_wire(ctx, Wire([(p.x0, p.y), (p.x1, p.y)], p.bits, p.colour, None, 0, solid=getattr(p, "solid", False)), opts)
    draw_pin(ctx, Pin(p.pin_pos, p.pin_pos, p.bits, p.is_input, p.colour, p.name, 1))
    if p.show_name and p.name.strip():
        w = text_width(p.name, FONT_PIN) + LABEL_PAD[0]
        cx = p.x0 + 0.04 + w / 2 if p.is_input else p.x1 - 0.04 - w / 2
        text_label(ctx, p.name, (cx, p.y))


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def export_scene(sc, opts, png_path=None, svg_path=None, ppu=100, max_px=16000, grid=False, margin=0.6):
    x0, y0, x1, y1 = sc.bbox
    x0 -= margin
    y0 -= margin
    x1 += margin
    y1 += margin
    bw, bh = x1 - x0, y1 - y0
    notes = []
    eff = ppu
    if max(bw, bh) * eff > max_px:
        eff = max_px / max(bw, bh)
        notes.append(f"downscaled to {eff:.0f} px/unit to stay under {max_px}px")

    def paint(surface, scale):
        _RENDER["split_buses"] = getattr(opts, "split_buses", False)
        ctx = cairo.Context(surface)
        rgb(ctx, BG)
        ctx.paint()
        ctx.translate(-x0 * scale, y1 * scale)
        ctx.scale(scale, -scale)
        if grid:
            ctx.set_line_width(max(0.0035, 1.0 / scale))
            rgb(ctx, GRID_COL)
            step = GRID if scale >= 60 else GRID * 4
            gx = math.floor(x0 / step) * step
            while gx <= x1:
                ctx.move_to(gx, y0)
                ctx.line_to(gx, y1)
                gx += step
            gy = math.floor(y0 / step) * step
            while gy <= y1:
                ctx.move_to(x0, gy)
                ctx.line_to(x1, gy)
                gy += step
            ctx.stroke()
        render_scene(ctx, sc, opts)

    size_px = (max(1, int(math.ceil(bw * eff))), max(1, int(math.ceil(bh * eff))))
    if png_path:
        surf = cairo.ImageSurface(cairo.FORMAT_RGB24, *size_px)
        paint(surf, eff)
        surf.write_to_png(png_path)
    if svg_path:
        svg_scale = 40  # SVG is vector; this only sets its nominal size
        surf = cairo.SVGSurface(svg_path, bw * svg_scale, bh * svg_scale)
        paint(surf, svg_scale)
        surf.finish()
    return size_px, notes


def safe_filename(name):
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip().rstrip(".")
    return s or "_"


def main(argv=None):
    ap = argparse.ArgumentParser(description="Batch-export Digital Logic Sim chips as images.",
                                 formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("--saves", default=default_saves_dir(), help="DLS Projects folder (auto-detected)")
    ap.add_argument("-p", "--project", nargs="*", help="project name(s) to export (default: all)")
    ap.add_argument("-c", "--chip", nargs="*", help="chip name(s) to export (default: all custom chips)")
    ap.add_argument("-m", "--mode", choices=["circuit", "expanded", "both"], default="both",
                    help="circuit = inside of each chip; expanded = sub-chips opened up recursively (default: both)")
    ap.add_argument("-f", "--format", choices=["png", "svg", "both"], default="png", help="output format (default: png)")
    ap.add_argument("-o", "--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "exports"),
                    help="output folder (default: ./exports next to this script)")
    ap.add_argument("--layout", choices=["auto", "original"], default=None,
                    help="auto = rearrange parts and re-route wires compactly; original = keep your layout "
                         "(default: auto)")
    ap.add_argument("--aspect", type=float, default=1.8,
                    help="auto layout: wrap layouts much wider than this width/height ratio into rows "
                         "(default 1.8; 0 = never wrap)")
    ap.add_argument("--keep-splitters", action="store_true",
                    help="auto layout: keep the 8-1BIT / 1-8BIT style splitter and merger chips instead of "
                         "fanning buses straight out into single-bit wires")
    ap.add_argument("--depth", type=int, default=None, help="expanded mode: how many levels to open (default: all)")
    ap.add_argument("--collapse", default="", help="expanded mode: comma-separated chip names to keep closed")
    ap.add_argument("--scale", type=float, default=100, help="pixels per world unit (default: 100)")
    ap.add_argument("--max-px", type=int, default=16000, help="max PNG width/height; larger images get downscaled (default: 16000)")
    ap.add_argument("--grid", action="store_true", help="draw the background grid")
    ap.add_argument("--lit", action="store_true", help="draw wires/pins in their bright 'on' colours (easier to see)")
    ap.add_argument("--no-pin-names", action="store_true", help="hide the names on the chip's own IN/OUT pins")
    ap.add_argument("--chip-pin-names", action="store_true", help="show pin names on every sub-chip")
    ap.add_argument("--font", default=None, help="font family for text (default: monospace)")
    ap.add_argument("--list", action="store_true", help="list projects and chips, then exit")
    args = ap.parse_args(argv)

    global FONT_FACE
    if args.font:
        FONT_FACE = args.font

    if not os.path.isdir(args.saves):
        sys.exit(f"Projects folder not found: {args.saves}\nUse --saves to point at it.")

    projects = sorted(p for p in os.listdir(args.saves) if os.path.isdir(os.path.join(args.saves, p)))
    if args.project:
        missing = [p for p in args.project if p not in projects]
        if missing:
            sys.exit(f"Unknown project(s): {', '.join(missing)}. Available: {', '.join(projects)}")
        projects = [p for p in projects if p in args.project]

    if args.list:
        for p in projects:
            lib = Library(os.path.join(args.saves, p))
            print(f"{p}: {len(lib.order)} chips")
            for n in lib.order:
                print(f"    {n}")
        return 0

    modes = ["circuit", "expanded"] if args.mode == "both" else [args.mode]
    collapse = [s.strip() for s in args.collapse.split(",") if s.strip()]
    total = 0
    for proj in projects:
        lib = Library(os.path.join(args.saves, proj))
        names = lib.order
        if args.chip:
            unknown = [c for c in args.chip if c not in lib.custom]
            if unknown and len(projects) == 1:
                print(f"  ! not found in {proj}: {', '.join(unknown)}", file=sys.stderr)
            names = [n for n in names if n in args.chip]
        if not names:
            continue
        print(f"[{proj}]")
        for mode in modes:
            out_dir = os.path.join(args.out, safe_filename(proj), mode)
            os.makedirs(out_dir, exist_ok=True)
            opts = Options(expand=(mode == "expanded"), depth=args.depth, collapse=collapse,
                           dev_pin_names=not args.no_pin_names, chip_pin_names=args.chip_pin_names, lit=args.lit,
                           aspect=args.aspect, dissolve_converters=not args.keep_splitters)
            for name in names:
                desc = lib.custom[name]
                auto_cache = {}
                try:
                    layout = args.layout or "auto"
                    opts.split_buses = layout == "auto"
                    sc = (build_auto_scene(lib, desc, opts, cache=auto_cache) if layout == "auto"
                          else build_scene(lib, desc, opts))
                    frames = 0
                    if mode == "expanded":
                        frames = count_frames(sc)
                        sc = wrap_in_root_frame(sc, desc)
                    base = os.path.join(out_dir, safe_filename(name))
                    png = base + ".png" if args.format in ("png", "both") else None
                    svg = base + ".svg" if args.format in ("svg", "both") else None
                    size, notes = export_scene(sc, opts, png, svg, args.scale, args.max_px, args.grid)
                    extra = f", {frames} sub-chips expanded" if mode == "expanded" else ""
                    note = f"  ({'; '.join(notes)})" if notes else ""
                    print(f"  {mode:8s} {name}  ->  {size[0]}x{size[1]}{extra}{note}")
                    total += 1
                except Exception as e:  # keep going on bad chips
                    print(f"  ! {mode} {name}: {type(e).__name__}: {e}", file=sys.stderr)
    print(f"Done: {total} image(s) written to {os.path.abspath(args.out)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
