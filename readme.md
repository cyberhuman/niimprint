# `niimprint` &mdash; Niimbot Printer Client

**Fork changelog & differences from original version:**

- Tested on Niimbot B1, B18, B21, D11, D110 and Python 3.11
- Added D11_H support (300 dpi, 144 px head, newer print task) &mdash; see below
- Added transport abstraction: switch between bluetooth and USB (serial)
- Disabled checksum calculation for image encoding (works fine without it so far)
- Switched to [click](https://click.palletsprojects.com/) CLI library instead of argparse
- Integrated [pyproject.toml](https://pip.pypa.io/en/stable/reference/build-system/pyproject-toml/) and [poetry](https://python-poetry.org)
- Integrated [pre-commit](https://pre-commit.com/) and [ruff](https://docs.astral.sh/ruff/), re-formatted all files
- Miscellaneous refactoring / file renaming / etc.

## Installation

Recommended method is to use [poetry](https://python-poetry.org) and install with `poetry install`. However `requirements.txt` is also provided for convenience. Project is tested on Python 3.11, but should work on other versions.

## Usage

```
$ python niimprint --help

Usage: niimprint [OPTIONS]

Options:
  -m, --model [b1|b18|b21|d11|d110|d11_h]  Niimbot printer model  [default: b21]
  -c, --conn [usb|bluetooth]   Connection type  [default: usb]
  -a, --addr TEXT              Bluetooth MAC address OR serial device path
  -d, --density INTEGER RANGE  Print density  [default: 5; 1<=x<=5]
  -r, --rotate [0|90|180|270]  Image rotation (clockwise)  [default: 0]
  -i, --image PATH             Image path  [required]
  -v, --verbose                Enable verbose logging
  --help                       Show this message and exit.
```

### Image orientation:

Generally, the image comes out of the printer with the same orientation you see it on your screen. You can have your input image rotated as you like, but adjust its orientation by passing `-r <...>` flag. See the image below for clarification.

[![](examples/image_orientation.png)]()

<!-- Excalidraw link: https://excalidraw.com/#json=vYHMBohMn5GeB-5M6SNch,TsxRmh_WKUfzYjL183FGfg -->

### Image resolution:

As far as we've tested, Niimbot printers have **8 pixels per mm** (~203 dpi) resolution. The CLI prints the image you provided as-is, without any checks of the actual label size, so be careful. However the script will check if the image width is too big for selected printer. The maximum width in pixels is usually slightly less than specified maximum width in mm:

- **B21, B1, B18**: max 384 pixels (almost equal to 50 mm * 8 px/mm = 400)
- **D11**: max 96 pixels (almost equal to 15 mm * 8 px/mm = 120)
- **D11_H**: max 144 pixels &mdash; this model is **300 dpi (11.8 px/mm)**, so a 25x10 mm label is 295x118 px, and the 144 px head covers 12.2 mm of the tape

### B1

A B1 on firmware 15.x (protocol 3) also prints a blank label with the legacy task: it acks everything, feeds, but the early PrintEnd aborts the job. `--model b1` therefore uses the "b1" task from [iscarelli/niimbot-web-bluetooth](https://github.com/iscarelli/niimbot-web-bluetooth): the app's connect handshake, 7-byte PrintStart, PageStart, 6-byte SetPageSize, rows with pixel counts paced at 10 ms, and polling the printed-page counter before PrintEnd.

### D11_H

The D11_H is not a D11 with a different sticker: it prints at 300 dpi and speaks the newer print task of the 300 dpi models (D110_M, B1 Pro, B21 Pro). Driven with the legacy task it acks everything and then prints a cut-off or blank label, because it only starts printing after acknowledging the page end and the legacy task sends "print end" 0.3 s later. `--model d11_h` selects the newer task: 9-byte start, 13-byte page size, rows with pixel counts and run lengths, and polling the printed-page counter before ending the job. Sequence and geometry follow [niimbluelib](https://github.com/MultiMote/niimbluelib) (`D110MV4PrintTask`) and [iscarelli/niimbot-web-bluetooth](https://github.com/iscarelli/niimbot-web-bluetooth) (`docs/protocol-v4.md`, validated on a D11_H); the printer reports its 144 px head in the type-3 heartbeat reply.

### USB connection:

For USB connection, you can omit the `--addr` argument and let the script auto-detect the serial port. However, it will fail if there're multiple available ports. On linux, serial ports can be found at `/dev/ttyUSB*`, `/dev/ttyACM*` or `/dev/serial/*`. On windows, they will be named like `COM1`, `COM2` etc. Check the device manager to choose the correct one.

### Bluetooth connection:

It seems like B21, B1 and D11_H (and maybe other models?) have two bluetooth adresses. They have the same last 3 bytes, but the first 3 are rotated (for example `AA:BB:CC:DD:EE:FF` and `CC:AA:BB:DD:EE:FF`). Connection works only if you disconnect from one and connect to the other. After connecting via bluetoothctl you may get `org.bluez.Error.NotAvailable br-connection-profile-unavailable` error, but printing works fine regardless.

To identify which address is the correct one, run `bluetoothctl info` on the address you want to check. The incorrect one might list `UUID: Generic Access Profile` and `UUID: Generic Attribute Profile`, while the correct one will list `UUID: Serial Port`.

## Examples

**B21, USB connection, 30x15 mm (240x120 px) label**

```
python niimprint -c usb -a /dev/ttyACM0 -r 90 -i examples/B21_30x15mm_240x120px.png
```

[![](examples/B21_30x15_result.png)]()

**B21, Bluetooth connection, 80x50 mm (640x384 px) label**

```
python niimprint -c bluetooth -a "E2:E1:08:03:09:87" -r 90 -i examples/B21_80x50mm_640x384px.png
```

[![](examples/B21_80x50_result.png)]()

**D11_H, Bluetooth connection, 25x10 mm (295x118 px) label**

```
python niimprint -m d11_h -c bluetooth -a "01:85:14:26:06:01" -r 90 -i examples/D11_H_25x10mm_295x118px.png
```

## Licence

[MIT](https://choosealicense.com/licenses/mit/). Originally developed by [kjy00302](https://github.com/kjy00302), forked & enhanced by [AndBondStyle](https://github.com/AndBondStyle)
