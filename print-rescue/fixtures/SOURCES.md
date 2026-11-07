# print-rescue test fixtures

All fixtures come from the 2026-11-07 incident (an MCU USB link stall that
shut Klipper down during a print, see
[docs/print-rescue-incident-2026-11-07.md](../../docs/print-rescue-incident-2026-11-07.md)).
They were cut from captures taken on the printer's host and scrubbed before
they were added here. Numbers (times, sequence counters, byte offsets,
coordinates) are unchanged.

| File | Origin | What was kept | Scrubbing |
|------|--------|---------------|-----------|
| `klippy-excerpt.log` | `docker logs --timestamps klipper` (Klippy stdout), 08:40-09:30 UTC | The last 200 `Stats` lines before the shutdown (09:06:30-09:09:49 UTC) and the whole shutdown dump through `Exiting SD card print (position 5404182)`, i.e. lines 1504-2151 of the capture. Includes `Timeout with MCU`, `Transition to shutdown state`, the serial queue dumps, the first `Stats` line after the shutdown (`print_time=8766.891`), `gcode state: ...`, `Virtual sdcard (5403128)` and `Upcoming (5404152)`. | The host's name appeared as part of the MCU temperature sensor name; that sensor is renamed `PRINTER_MCU` everywhere (Stats lines and the dumped client requests). No paths, serial numbers or addresses were present in this range. |
| `kernel-usb.log` | `journalctl -k` lines for the USB serial adapter, host local time (UTC+1) | The CH340 (`ch341-uart`) attach lines and the two `usb_serial_generic_read_bulk_callback - urb stopped: -32` lines (07:51:48 and 10:09:45 local). | Host name replaced with `pi`, the tty device name with `<tty>`. The Pico MMU's USB lines were dropped because they carry its serial number. |
| `history-job.json` | Moonraker `/server/history/list` | Only the interrupted job (`status: klippy_shutdown`): file name, times, durations, filament used, and the metadata fields `size`, `modified`, slicer, layer count, heights. | User field, UUID, filament names and thumbnails dropped. |
| `gcode-excerpt.gcode.gz` | The print file (OrcaSlicer 2.4.2, 7 975 383 bytes, 306 226 lines, 255 layers, M83, one tool) | 5 703 original lines, gzip-compressed: lines 1-13 (header) and 83-171 (`EXCLUDE_OBJECT_DEFINE`, `M83`, `SET_PRESSURE_ADVANCE`, `T0`, first `SET_VELOCITY_LIMIT`, the first-layer skirt that sets the total extrusion box); every state line between 172 and 201299 (`;LAYER_CHANGE`, `;Z:`, `M104/M109/M140/M190/M106/M107`, `SET_*`, `T<n>`, `EXCLUDE_OBJECT*`, `M220/M221/M204`, `G90/G91`, `M82/M83`, `G17-G19`, and every move with a `Z` word); and the full region 201300-202700 around the cut (layer 158 starts at line 201325, the last dispatched line is 202396). Thumbnails and the trailing slicer configuration block are not included. | None needed: the kept lines contain only G-code and slicer comments (checked with the same pattern as below). |
| `gcode-excerpt.map.json` | Generated with the excerpt | `runs`: `[first original line, line count, byte offset of that line in the original file]` for each block of consecutive original lines. `gcode_scan.iter_excerpt` rebuilds the original line numbers and byte offsets from it, so tests use the real numbers (5404182 -> line 202397, 5398158 -> line 202133). `source_size` is the original file size, used for the R07 check. | - |

The excerpt was checked against the full original file: every kept line has
the same text, line number and byte offset, and the planner result on the
excerpt (stop window, resume line, overlap, part box X/Y 29.8-188.4, top
20.6 mm) is identical to the result on the full file.

What the excerpt cannot reproduce: positions between the sparse state lines
are stale, so motion before line 201300 is not timed meaningfully, and the
part box of layers 2-157 comes only from the skirt. For this part the skirt
encloses every later extrusion, so the box equals the full-file box; tests
that need the box rely on that.

The privacy check described in `docs/print-rescue.md` ("Fixtures and
privacy") was run on this directory and on the decompressed excerpt after
scrubbing and finds nothing.

## Incident 3 (the `inc3-*` files)

From the live rescue of 2026-11-07 (18:59, a vase printed in OrcaSlicer 2.4.2,
22 647 469 bytes, 500 layers, 53.0 mm tall at the stop, part box X/Y
71.1-148.9). Same stop cause as incident 1: `Lost communication with MCU`
with `urb stopped: -32` in the same minute. Numbers are unchanged.

| File | Origin | What was kept | Scrubbing |
|------|--------|---------------|-----------|
| `inc3-klippy-excerpt.log` | Klippy stdout of the host, lines 1000-1640 of the capture | The last 188 `Stats` lines before the shutdown (with `sd_pos`, `print_time`, `buffer_time`), the shutdown dump, `gcode state: ...`, `Virtual sdcard (8771029)`, `Upcoming (8772053)`, `Exiting SD card print (position 8772080)` and a few `Stats` lines after it. | None needed: checked with the pattern in `docs/print-rescue.md` (no addresses, paths, serial numbers). The temperature sensor names are generic. |
| `inc3-gcode-excerpt.gcode.gz` + `.map.json` | The print file | Every state line (anything that is not a `G0-G3` move or a comment), `;LAYER_CHANGE` and `;Z:` for all 500 layers, every move with a `Z` word, the moves that set the extrusion box (and the move before each), and the full region 390300-391700 around the cut. The thumbnail block and the slicer comment block are not kept. The map keeps the original line numbers and byte offsets, so the exit position 8772080 is the end of line 391419. | None needed: only G-code and slicer comments. |
| `inc3-history-job.json` | Moonraker job history | Only the interrupted job: file name, times, durations, filament, `size`, `modified`, slicer, layer count, heights. | User field, UUID, filament names and thumbnails dropped. |
| `inc3-queue.json` | Moonraker job queue | An empty, paused queue. | - |
| `inc3-kernel-usb.log` | `journalctl -k` | The one `urb stopped: -32` line (18:59:43 local). | Host name replaced with `pi`, the tty name with `<tty>`. |
| `inc3-printer-cfg.cfg` | The live `printer.cfg` | The sections the profile fingerprint reads (`stepper_x/y/z`, `printer`, `bltouch`, `safe_z_home`, `bed_mesh`) and a SAVE_CONFIG block with `z_offset = 2.960` (the repository file has 3.035). | Everything else dropped (it holds the MCU serial path). |

The planner result on the excerpt (stop window, resume line 390866, overlap
3.55 s / 104.8 mm, part box, pin at boot) is identical to the result on the
full file. What the excerpt cannot reproduce: positions between the sparse
state lines are stale, so motion before line 390300 is not timed meaningfully,
the slicer header is absent (tests for the header use synthetic lines), and
`e_total` is not the real net filament.
