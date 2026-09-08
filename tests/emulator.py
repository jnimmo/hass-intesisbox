"""Minimal WMP device emulator used by the transport tests.

Unlike the emulator that used to sit inside the component package, this one
never starts a server on import, and it can be told to misbehave the way real
devices do, starting with writing its replies a byte at a time.
"""

from __future__ import annotations

import asyncio
import re

DEFAULT_STATE = {
    "MODE": "AUTO",
    "SETPTEMP": "210",
    "ONOFF": "OFF",
    "FANSP": "AUTO",
    "AMBTEMP": "180",
    "VANEUD": "AUTO",
    "VANELR": "AUTO",
    "ERRSTATUS": "OK",
    "ERRCODE": "",
}

LIMITS = {
    "FANSP": "[AUTO,1,2,3,4]",
    "VANEUD": "[AUTO,1,2,3,SWING]",
    "VANELR": "[AUTO,1,2,3,SWING]",
    "SETPTEMP": "[160,300]",
    "MODE": "[AUTO,HEAT,DRY,COOL,FAN]",
}

# Functions a client may SET. Writing anything else gets ERR, as on a real unit.
RW_FUNCTIONS = {"ONOFF", "MODE", "SETPTEMP", "FANSP", "VANEUD", "VANELR"}

ID_GEN1 = "ID:IS-IR-WMP-1,001DC9A2C911,192.168.100.246,ASCII,v1.0.2,-44"
ID_V6 = "ID:INWMPUNI001I000,001DC9A2C911,192.168.100.246,v1.0.1,-44,WMP_A2C911,N,6"


class Emulator(asyncio.Protocol):
    """A WMP device that can be made to behave badly on purpose."""

    #: Write responses one byte at a time, forcing the client to reassemble.
    tear_frames = False
    #: Which ID banner to report.
    id_banner = ID_GEN1
    #: Accept the connection and answer nothing at all.
    silent = False
    #: Live connections, so a test can drop them.
    connections: list[Emulator] = []

    def __init__(self) -> None:
        """Start with a fresh copy of the default device state."""
        self.state = dict(DEFAULT_STATE)
        self.buffer = b""

    @classmethod
    def reset(cls) -> None:
        """Return the class-level knobs to their defaults."""
        cls.tear_frames = False
        cls.id_banner = ID_GEN1
        cls.silent = False
        cls.connections = []

    @classmethod
    def drop_all(cls) -> None:
        """Close every live connection, as the device's watchdog would."""
        for conn in list(cls.connections):
            conn.transport.close()
        cls.connections = []

    def connection_made(self, transport):
        """Register the connection and start its writer."""
        self.transport = transport
        self._outbox: asyncio.Queue[bytes] = asyncio.Queue()
        self._writer = asyncio.get_running_loop().create_task(self._drain())
        Emulator.connections.append(self)

    def connection_lost(self, exc):
        """Stop the writer and forget the connection."""
        self._writer.cancel()
        if self in Emulator.connections:
            Emulator.connections.remove(self)

    def send(self, text: str) -> None:
        """Queue a response for the writer."""
        self._outbox.put_nowait(text.encode("ascii"))

    async def _drain(self) -> None:
        """Write queued responses, a byte per loop turn when tearing frames.

        Pausing between bytes is what makes the tearing real: written back to
        back on loopback they coalesce in the client's receive buffer and
        arrive as one chunk after all.
        """
        while True:
            data = await self._outbox.get()
            if not Emulator.tear_frames:
                self.transport.write(data)
                continue
            for index in range(len(data)):
                self.transport.write(data[index : index + 1])
                await asyncio.sleep(0.001)

    def data_received(self, data: bytes) -> None:
        """Reassemble lines and dispatch them.

        Deliberately framed by a different mechanism than the client's
        data_received: a bug copied into both sides would make the client's
        torn-frame handling look correct when it is not.
        """
        self.buffer += data
        while True:
            match = re.search(rb"[\r\n]", self.buffer)
            if match is None:
                break
            raw = self.buffer[: match.start()]
            self.buffer = self.buffer[match.end() :]
            line = raw.decode("ascii").strip()
            if line:
                self.handle(line)

    def handle(self, line: str) -> None:
        """Respond to one command."""
        if Emulator.silent:
            return
        head = line.split(",")[0]

        if line == "ID":
            self.send(f"{Emulator.id_banner}\r\n")
        elif line == "PING":
            self.send("ACK\r\n")
        elif line.startswith("LIMITS:"):
            function = line.split(":", 1)[1]
            if function in LIMITS:
                self.send(f"LIMITS:{function},{LIMITS[function]}\r\n")
        elif head == "GET":
            function = line.split(":", 1)[1]
            if function == "*":
                for key, value in self.state.items():
                    self.send(f"CHN,1:{key},{value}\r\n")
            elif function in self.state:
                self.send(f"CHN,1:{function},{self.state[function]}\r\n")
            else:
                self.send("ERR\r\n")
        elif head == "SET":
            payload = line.split(":", 1)[1]
            function, value = payload.split(",", 1)
            if function not in RW_FUNCTIONS:
                self.send("ERR\r\n")
                return
            self.send("ACK\r\n")
            if self.state.get(function) != value:
                self.state[function] = value
                self.send(f"CHN,1:{function},{value}\r\n")


async def start(host: str = "127.0.0.1", port: int = 0):
    """Start an emulator server and return it."""
    loop = asyncio.get_running_loop()
    Emulator.reset()
    return await loop.create_server(Emulator, host, port)


if __name__ == "__main__":

    async def _main() -> None:
        server = await start("0.0.0.0", 3310)
        await server.serve_forever()

    asyncio.run(_main())
