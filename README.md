# DLS Image Exporter

Batch-export the chips you've built in [Digital Logic Sim](https://github.com/SebLague/Digital-Logic-Sim)
(v2.x) as images: every chip in every project with one command, either as you laid it out
or opened up all the way down to its NAND gates.

![The FULL ADDER chip, expanded with the auto layout](docs/full-adder-expanded.png)

It's a single Python script that reads your save files directly (read-only), so it works
with the regular game build and needs no mods. Requires Python 3 and `pycairo`.

Saves are auto-detected from
`~/.config/unity3d/SebastianLague/Digital-Logic-Sim/Projects` on Linux and the usual
Windows/macOS locations; point `--saves` somewhere else if needed.

## Modes

| Mode       | Output folder                  | What it shows |
|------------|--------------------------------|---------------|
| `circuit`  | `exports/<project>/circuit/`   | The inside of each chip |
| `expanded` | `exports/<project>/expanded/`  | Every custom sub-chip opened up in place, recursively. Each one becomes a bordered frame, titled with the chip's name, holding its own inner circuit |

## Layouts

| Layout     | What it does |
|------------|--------------|
| `auto` (default) | Rearranges the parts and re-routes the wires so the circuit is easy to follow, and uses far less space, so gates are readable without zooming in |
| `original` | Keeps your placement and your wire bends. In expanded mode, the space around each opened chip is stretched to make room, so straight wires stay straight |

The auto layout places each chip's parts in columns by signal flow (inputs on the left,
outputs on the right), with feedback loops such as latches routed back around. Parts are
ordered to reduce wire crossings and lined up with whatever drives them, so most wires
run straight. Wires are routed horizontally and vertically through the gaps between
columns, each vertical run on its own track, and dots mark where a wire branches. After
placing the parts, it nudges them wherever there's room so that more wires run dead level
instead of taking small jogs.
Bit splitters and mergers (`8-1BIT`, `1-8BIT` and the 4-bit ones) are left out. A bus
whose bits all go to the same place is drawn as one thick wire with a dark centre line.
Where a bus splits up, it's drawn as one wire per bit, close together and in bit order
(highest bit on top, as the game lays out bus pins), and each bit peels off on its own
toward where it goes, crossing the others where it has to. Single-bit wires join a bus at a multi-bit pin in the same
bit order. Each wire is traced through the splitters to the pin that really drives it.

Layouts much wider than the target shape (`--aspect`, default 1.8) can wrap into rows,
and every wire is still drawn whole: a wire crossing a row break runs down the right edge
of its row, back along its own lane in the gap between rows, and into the next row from
the left. Because those looping wires cost space and clarity, a layout only wraps when
the better shape outweighs them, so a small chip whose wires mostly cross the break
(like an 8-bit ripple-carry adder) stays in one long row.

## Common commands

```sh
python3 dls_export.py                         # all projects, all chips, both modes (PNG)
python3 dls_export.py --list                  # list projects and chips
python3 dls_export.py -p ALU                  # one project
python3 dls_export.py -p ALU -c ALU "FULL ADDER"           # specific chips
python3 dls_export.py -p ALU --mode expanded --format svg   # vector output
python3 dls_export.py --layout original      # keep your own layout
python3 dls_export.py --mode expanded --depth 1            # open only one level
python3 dls_export.py --mode expanded --collapse AND,OR,NOT   # keep these chips closed
python3 dls_export.py --lit                   # bright wire colours (easier to see)
```

Other flags: `--aspect` (row wrapping for the auto layout; `0` turns it off),
`--keep-splitters` (auto layout: draw the splitter and merger chips after all), `--scale`
(pixels per unit, default 100), `--max-px` (PNG size cap, default 16000; larger images
are scaled down, so use `--format svg` for very big chips), `--grid`, `--no-pin-names`,
`--chip-pin-names`, `--out DIR`, `--font FAMILY`.

## Notes

- Chip sizes, pin spacing, the built-in chips and multi-bit wire drawing follow the
  public Digital Logic Sim source, so in the `original` layout wires meet pins where
  they do in the game.
- Wires are drawn in their "off" colours, since nothing is being simulated. Use
  `--lit` for the bright "on" colours.
- The chip's own IN/OUT pin names are drawn on the outer side of each pin, so they
  don't cover wires.

This is an independent tool, not affiliated with Sebastian Lague or Digital Logic Sim.
