import logging
import re

import click
from PIL import Image

from niimprint import BluetoothTransport, PrinterClient, SerialTransport

# model: (max image width in px, max density, print task)
MODELS = {
    "b1": (384, 5, "b1"),
    "b18": (384, 3, "legacy"),
    "b21": (384, 5, "legacy"),
    "d11": (96, 3, "legacy"),
    "d110": (96, 3, "legacy"),
    "d11_h": (144, 5, "v4"),
}


@click.command("print")
@click.option(
    "-m",
    "--model",
    type=click.Choice(list(MODELS), False),
    default="b21",
    show_default=True,
    help="Niimbot printer model",
)
@click.option(
    "-c",
    "--conn",
    type=click.Choice(["usb", "bluetooth"]),
    default="usb",
    show_default=True,
    help="Connection type",
)
@click.option(
    "-a",
    "--addr",
    help="Bluetooth MAC address OR serial device path",
)
@click.option(
    "-d",
    "--density",
    type=click.IntRange(1, 5),
    default=5,
    show_default=True,
    help="Print density",
)
@click.option(
    "-r",
    "--rotate",
    type=click.Choice(["0", "90", "180", "270"]),
    default="0",
    show_default=True,
    help="Image rotation (clockwise)",
)
@click.option(
    "-i",
    "--image",
    type=click.Path(exists=True),
    required=True,
    help="Image path",
)
@click.option(
    "-v",
    "--verbose",
    is_flag=True,
    help="Enable verbose logging",
)
def print_cmd(model, conn, addr, density, rotate, image, verbose):
    logging.basicConfig(
        level="DEBUG" if verbose else "INFO",
        format="%(levelname)s | %(module)s:%(funcName)s:%(lineno)d - %(message)s",
    )

    if conn == "bluetooth":
        assert addr is not None, "--addr argument required for bluetooth connection"
        addr = addr.upper()
        assert re.fullmatch(r"([0-9A-F]{2}:){5}([0-9A-F]{2})", addr), "Bad MAC address"
        transport = BluetoothTransport(addr)
    if conn == "usb":
        port = addr if addr is not None else "auto"
        transport = SerialTransport(port=port)

    max_width_px, max_density, task = MODELS[model]
    if density > max_density:
        logging.warning(f"{model.upper()} only supports density up to {max_density}")
        density = max_density

    image = Image.open(image)
    if rotate != "0":
        # PIL library rotates counter clockwise, so we need to multiply by -1
        image = image.rotate(-int(rotate), expand=True)
    assert image.width <= max_width_px, f"Image width too big for {model.upper()}"

    printer = PrinterClient(transport)
    printer.print_image(image, density=density, task=task)


if __name__ == "__main__":
    print_cmd()
