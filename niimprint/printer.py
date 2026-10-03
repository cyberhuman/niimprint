import abc
import enum
import logging
import math
import socket
import struct
import time

import serial
from PIL import Image, ImageOps
from serial.tools.list_ports import comports as list_comports

from niimprint.packet import NiimbotPacket


class InfoEnum(enum.IntEnum):
    DENSITY = 1
    PRINTSPEED = 2
    LABELTYPE = 3
    LANGUAGETYPE = 6
    AUTOSHUTDOWNTIME = 7
    DEVICETYPE = 8
    SOFTVERSION = 9
    BATTERY = 10
    DEVICESERIAL = 11
    HARDVERSION = 12


class RequestCodeEnum(enum.IntEnum):
    GET_INFO = 64  # 0x40
    GET_RFID = 26  # 0x1A
    HEARTBEAT = 220  # 0xDC
    SET_LABEL_TYPE = 35  # 0x23
    SET_LABEL_DENSITY = 33  # 0x21
    START_PRINT = 1  # 0x01
    END_PRINT = 243  # 0xF3
    START_PAGE_PRINT = 3  # 0x03
    END_PAGE_PRINT = 227  # 0xE3
    ALLOW_PRINT_CLEAR = 32  # 0x20
    SET_DIMENSION = 19  # 0x13
    SET_QUANTITY = 21  # 0x15
    GET_PRINT_STATUS = 163  # 0xA3


def _packet_to_int(x):
    return int.from_bytes(x.data, "big")


class BaseTransport(metaclass=abc.ABCMeta):
    @abc.abstractmethod
    def read(self, length: int) -> bytes:
        raise NotImplementedError

    @abc.abstractmethod
    def write(self, data: bytes):
        raise NotImplementedError


class BluetoothTransport(BaseTransport):
    def __init__(self, address: str):
        self._sock = socket.socket(
            socket.AF_BLUETOOTH,
            socket.SOCK_STREAM,
            socket.BTPROTO_RFCOMM,
        )
        self._sock.connect((address, 1))

    def read(self, length: int) -> bytes:
        return self._sock.recv(length)

    def write(self, data: bytes):
        return self._sock.send(data)


class SerialTransport(BaseTransport):
    def __init__(self, port: str = "auto"):
        port = port if port != "auto" else self._detect_port()
        self._serial = serial.Serial(port=port, baudrate=115200, timeout=0.5)

    def _detect_port(self):
        all_ports = list(list_comports())
        if len(all_ports) == 0:
            raise RuntimeError("No serial ports detected")
        if len(all_ports) > 1:
            msg = "Too many serial ports, please select specific one:"
            for port, desc, hwid in all_ports:
                msg += f"\n- {port} : {desc} [{hwid}]"
            raise RuntimeError(msg)
        return all_ports[0][0]

    def read(self, length: int) -> bytes:
        return self._serial.read(length)

    def write(self, data: bytes):
        return self._serial.write(data)


class PrinterClient:
    def __init__(self, transport):
        self._transport = transport
        self._packetbuf = bytearray()

    def print_image(self, image: Image, density: int = 3, task: str = "legacy"):
        if task in ("v4", "b1"):
            self._print_image_polled(image, density, task)
            return
        self.set_label_density(density)
        self.set_label_type(1)
        self.start_print()
        # self.allow_print_clear()  # Something unsupported in protocol decoding (B21)
        self.start_page_print()
        self.set_dimension(image.height, image.width)
        # self.set_quantity(1)  # Same thing (B21)
        for pkt in self._encode_image(image):
            self._send(pkt)
        self.end_page_print()
        time.sleep(0.3)  # FIXME: Check get_print_status()
        while not self.end_print():
            time.sleep(0.1)

    def _print_image_polled(self, image: Image, density: int, task: str):
        """Print tasks that need the printed-page counter polled before PrintEnd.

        "v4": the 300 dpi models (D11_H, D110_M, B1 Pro, B21 Pro), per
        niimbluelib's D110MV4PrintTask: 9-byte PrintStart, one-way PrintStatus
        instead of PageStart, 13-byte SetPageSize.
        "b1": the protocol-3 B1 line, per iscarelli/niimbot-web-bluetooth
        (docs/protocol-v4.md): the app's connect handshake first, 7-byte
        PrintStart, PageStart, 6-byte SetPageSize, rows paced at ~10 ms.

        Both: rows carry their black pixel count and a repeat count, blank rows
        go as PrintEmptyRow, and PrintEnd is only sent once the printer reports
        the page as printed. These printers start printing after they ack
        PageEnd; the legacy task's PrintEnd 0.3 s later aborts the job and the
        label comes out blank or cut off.
        """
        if task == "b1":
            self._handshake_b1()
        self.set_label_density(density)
        self.set_label_type(1)
        if task == "v4":
            self.start_print_v4(1)
            status = NiimbotPacket(RequestCodeEnum.GET_PRINT_STATUS, b"\x01")
            self._send(status)  # one-way, no reply awaited
            time.sleep(0.03)
            self.set_page_size_v4(image.height, image.width, 1)
        else:
            self.start_print_b1(1)
            self.start_page_print()
            self.set_page_size_b1(image.height, image.width, 1)
        for pkt in self._encode_image_v4(image):
            self._send(pkt)
            if task == "b1":
                time.sleep(0.01)  # the B1 drops rows on an unpaced burst
        self._send(NiimbotPacket(RequestCodeEnum.END_PAGE_PRINT, b"\x01"))
        # Some printers (D11_H) park the PageEnd ack until they receive another
        # packet, so keep polling status until the page counter reports the page.
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            self._send(NiimbotPacket(RequestCodeEnum.GET_PRINT_STATUS, b"\x01"))
            time.sleep(0.15)
            for packet in self._recv():
                if packet.type == 219:
                    raise ValueError(f"printer error {packet.data.hex()}")
                if packet.type == 0xB3 and len(packet.data) >= 4:
                    page = int.from_bytes(packet.data[:2], "big")
                    progress, feed = packet.data[2], packet.data[3]
                    logging.debug(f"page {page} print {progress}% feed {feed}%")
                    if page >= 1:
                        self.end_print()
                        return
        raise TimeoutError("printer never reported the page as printed")

    def _handshake_b1(self):
        # Without the app's connect sequence a protocol-3 B1 acks every command
        # but never prints (iscarelli/niimbot-web-bluetooth, "b1 post-connect
        # handshake"). Replies that are not supported are ignored.
        for reqcode, data, respoffset in (
            (0xA5, b"\x01", 16),  # PrinterStatusData -> 0xB5
            *(
                (RequestCodeEnum.GET_INFO, bytes((k,)), k)
                for k in (8, 11, 13, 10, 7, 3, 12, 9)
            ),
            (RequestCodeEnum.HEARTBEAT, b"\x04", -3),  # Advanced2 -> 0xD9
        ):
            try:
                self._transceive(reqcode, data, respoffset)
            except NotImplementedError:
                pass

    def _encode_image(self, image: Image):
        img = ImageOps.invert(image.convert("L")).convert("1")
        for y in range(img.height):
            line_data = self._encode_row(img, y)
            counts = (0, 0, 0)  # It seems like you can always send zeros
            header = struct.pack(">H3BB", y, *counts, 1)
            pkt = NiimbotPacket(0x85, header + line_data)
            yield pkt

    def _encode_image_v4(self, image: Image):
        img = ImageOps.invert(image.convert("L")).convert("1")
        rows = []  # [first row, data or None if blank, repeat count]
        for y in range(img.height):
            line_data = self._encode_row(img, y)
            if not any(line_data):
                line_data = None
            if rows and rows[-1][1] == line_data and rows[-1][2] < 200:
                rows[-1][2] += 1
            else:
                rows.append([y, line_data, 1])
        for y, line_data, repeat in rows:
            if line_data is None:
                yield NiimbotPacket(0x84, struct.pack(">HB", y, repeat))
            else:
                total = sum(bin(b).count("1") for b in line_data)
                header = struct.pack(">HBBBB", y, 0, total & 0xFF, total >> 8, repeat)
                yield NiimbotPacket(0x85, header + line_data)

    @staticmethod
    def _encode_row(img: Image, y: int) -> bytes:
        width_bytes = math.ceil(img.width / 8)
        line_data = [img.getpixel((x, y)) for x in range(img.width)]
        line_data = "".join("0" if pix == 0 else "1" for pix in line_data)
        line_data = line_data.ljust(width_bytes * 8, "0")  # MSB-first, pad right
        return int(line_data, 2).to_bytes(width_bytes, "big")

    def _recv(self):
        packets = []
        self._packetbuf.extend(self._transport.read(1024))
        while len(self._packetbuf) > 4:
            pkt_len = self._packetbuf[3] + 7
            if len(self._packetbuf) >= pkt_len:
                packet = NiimbotPacket.from_bytes(self._packetbuf[:pkt_len])
                self._log_buffer("recv", packet.to_bytes())
                packets.append(packet)
                del self._packetbuf[:pkt_len]
        return packets

    def _send(self, packet):
        self._transport.write(packet.to_bytes())

    def _log_buffer(self, prefix: str, buff: bytes):
        msg = ":".join(f"{i:#04x}"[-2:] for i in buff)
        logging.debug(f"{prefix}: {msg}")

    def _transceive(self, reqcode, data, respoffset=1):
        respcode = respoffset + reqcode
        packet = NiimbotPacket(reqcode, data)
        self._log_buffer("send", packet.to_bytes())
        self._send(packet)
        resp = None
        for _ in range(6):
            for packet in self._recv():
                if packet.type == 219:
                    raise ValueError
                elif packet.type == 0:
                    raise NotImplementedError
                elif packet.type == respcode:
                    resp = packet
            if resp:
                return resp
            time.sleep(0.1)
        return resp

    def get_info(self, key):
        if packet := self._transceive(RequestCodeEnum.GET_INFO, bytes((key,)), key):
            match key:
                case InfoEnum.DEVICESERIAL:
                    return packet.data.hex()
                case InfoEnum.SOFTVERSION:
                    return _packet_to_int(packet) / 100
                case InfoEnum.HARDVERSION:
                    return _packet_to_int(packet) / 100
                case _:
                    return _packet_to_int(packet)
        else:
            return None

    def get_rfid(self):
        packet = self._transceive(RequestCodeEnum.GET_RFID, b"\x01")
        data = packet.data

        if data[0] == 0:
            return None
        uuid = data[0:8].hex()
        idx = 8

        barcode_len = data[idx]
        idx += 1
        barcode = data[idx : idx + barcode_len].decode()

        idx += barcode_len
        serial_len = data[idx]
        idx += 1
        serial = data[idx : idx + serial_len].decode()

        idx += serial_len
        total_len, used_len, type_ = struct.unpack(">HHB", data[idx : idx + 5])
        idx += 5
        info = {
            "uuid": uuid,
            "barcode": barcode,
            "serial": serial,
            "used_len": used_len,
            "total_len": total_len,
            "type": type_,
        }
        if len(data) >= idx + 2:  # newer firmware (e.g. B1) appends the roll capacity
            info["capacity"] = struct.unpack(">H", data[idx : idx + 2])[0]
        return info

    def heartbeat(self):
        packet = self._transceive(RequestCodeEnum.HEARTBEAT, b"\x01")
        closingstate = None
        powerlevel = None
        paperstate = None
        rfidreadstate = None

        match len(packet.data):
            case 20:
                paperstate = packet.data[18]
                rfidreadstate = packet.data[19]
            case 13:
                closingstate = packet.data[9]
                powerlevel = packet.data[10]
                paperstate = packet.data[11]
                rfidreadstate = packet.data[12]
            case 19:
                closingstate = packet.data[15]
                powerlevel = packet.data[16]
                paperstate = packet.data[17]
                rfidreadstate = packet.data[18]
            case 10:
                closingstate = packet.data[8]
                powerlevel = packet.data[9]
                rfidreadstate = packet.data[8]
            case 9:
                closingstate = packet.data[8]

        return {
            "closingstate": closingstate,
            "powerlevel": powerlevel,
            "paperstate": paperstate,
            "rfidreadstate": rfidreadstate,
        }

    def set_label_type(self, n):
        assert 1 <= n <= 3
        packet = self._transceive(RequestCodeEnum.SET_LABEL_TYPE, bytes((n,)), 16)
        return bool(packet.data[0])

    def set_label_density(self, n):
        assert 1 <= n <= 5  # B21 has 5 levels, not sure for D11
        packet = self._transceive(RequestCodeEnum.SET_LABEL_DENSITY, bytes((n,)), 16)
        return bool(packet.data[0])

    def start_print(self):
        packet = self._transceive(RequestCodeEnum.START_PRINT, b"\x01")
        return bool(packet.data[0])

    def start_print_b1(self, pages):
        data = struct.pack(">HBBBBB", pages, 0, 0, 0, 0, 0)
        packet = self._transceive(RequestCodeEnum.START_PRINT, data)
        return bool(packet.data[0])

    def start_print_v4(self, pages, speed=1):
        data = struct.pack(">HBBBBBBB", pages, 0, 0, 0, 0, 0, speed, 0)
        packet = self._transceive(RequestCodeEnum.START_PRINT, data)
        return bool(packet.data[0])

    def end_print(self):
        packet = self._transceive(RequestCodeEnum.END_PRINT, b"\x01")
        return bool(packet.data[0])

    def start_page_print(self):
        packet = self._transceive(RequestCodeEnum.START_PAGE_PRINT, b"\x01")
        return bool(packet.data[0])

    def end_page_print(self):
        packet = self._transceive(RequestCodeEnum.END_PAGE_PRINT, b"\x01")
        return bool(packet.data[0])

    def allow_print_clear(self):
        packet = self._transceive(RequestCodeEnum.ALLOW_PRINT_CLEAR, b"\x01", 16)
        return bool(packet.data[0])

    def set_dimension(self, w, h):
        packet = self._transceive(
            RequestCodeEnum.SET_DIMENSION, struct.pack(">HH", w, h)
        )
        return bool(packet.data[0])

    def set_page_size_b1(self, rows, cols, copies=1):
        data = struct.pack(">HHH", rows, cols, copies)
        packet = self._transceive(RequestCodeEnum.SET_DIMENSION, data)
        return bool(packet.data[0])

    def set_page_size_v4(self, rows, cols, copies=1):
        data = struct.pack(">HHH", rows, cols, copies) + bytes(7)
        packet = self._transceive(RequestCodeEnum.SET_DIMENSION, data)
        return bool(packet.data[0])

    def set_quantity(self, n):
        packet = self._transceive(RequestCodeEnum.SET_QUANTITY, struct.pack(">H", n))
        return bool(packet.data[0])

    def get_print_status(self):
        packet = self._transceive(RequestCodeEnum.GET_PRINT_STATUS, b"\x01", 16)
        page, progress1, progress2 = struct.unpack(">HBB", packet.data[:4])
        return {"page": page, "progress1": progress1, "progress2": progress2}
