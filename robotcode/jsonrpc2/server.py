import abc
import asyncio
import io
import sys
from enum import Enum
from types import TracebackType
from typing import (
    BinaryIO,
    Callable,
    Generic,
    Literal,
    NamedTuple,
    Optional,
    Type,
    TypeVar,
    cast,
)

from ..utils.logging import LoggingDescriptor
from .protocol import JsonRPCException

__all__ = ["StdOutTransportAdapter", "JsonRpcServerMode", "TcpParams", "JsonRPCServer"]

TProtocol = TypeVar("TProtocol", bound=asyncio.Protocol)


class StdOutTransportAdapter(asyncio.Transport):
    def __init__(self, rfile: BinaryIO, wfile: BinaryIO) -> None:
        super().__init__()
        self.rfile = rfile
        self.wfile = wfile

    def close(self) -> None:
        self.rfile.close()
        self.wfile.close()

    def write(self, data: bytes) -> None:
        self.wfile.write(data)
        self.wfile.flush()


class JsonRpcServerMode(Enum):
    STDIO = "stdio"
    TCP = "tcp"


class TcpParams(NamedTuple):
    host: Optional[str] = None
    port: int = 0


class JsonRPCServer(Generic[TProtocol], abc.ABC):
    _logger = LoggingDescriptor()

    def __init__(
        self,
        mode: JsonRpcServerMode = JsonRpcServerMode.STDIO,
        tcp_params: TcpParams = TcpParams(None, 0),
    ):
        self.mode = mode
        self.tcp_params = tcp_params

        self._run_func: Optional[Callable[[], None]] = None
        self._server: Optional[asyncio.AbstractServer] = None

        self._stdio_stop_event: Optional[asyncio.Event] = None

        self.loop = asyncio.get_event_loop()

    def __del__(self) -> None:
        self.close()

    @_logger.call
    def start(self) -> None:
        if self.mode == JsonRpcServerMode.STDIO:
            self.start_stdio()
        elif self.mode == JsonRpcServerMode.TCP:
            self.start_tcp(self.tcp_params.host, self.tcp_params.port)
        else:
            raise JsonRPCException(f"Unknown server mode {self.mode}")

    @_logger.call
    def close(self) -> None:
        if self._stdio_stop_event is not None:
            self._stdio_stop_event.set()

        if self._server and self._server.is_serving:
            self._server.close()

    def __enter__(self) -> "JsonRPCServer[TProtocol]":
        self.start()
        return self

    def __exit__(
        self,
        exception_type: Optional[Type[BaseException]],
        exception_value: Optional[BaseException],
        traceback: Optional[TracebackType],
    ) -> Literal[False]:
        self.close()
        return False

    @abc.abstractmethod
    def create_protocol(self) -> TProtocol:
        ...

    def shutdown_protocol(self, protocol: TProtocol) -> None:
        if self.mode == JsonRpcServerMode.STDIO and self._stdio_stop_event is not None:
            self._stdio_stop_event.set()

    @_logger.call
    def start_stdio(self) -> None:
        self.mode = JsonRpcServerMode.STDIO

        transport = StdOutTransportAdapter(sys.__stdin__.buffer, sys.__stdout__.buffer)

        protocol = self.create_protocol()

        def run_io_nonblocking() -> None:
            self._stdio_stop_event = asyncio.Event()

            async def aio_readline(rfile: BinaryIO, protocol: asyncio.Protocol) -> None:
                protocol.connection_made(transport)

                while self._stdio_stop_event is not None and not self._stdio_stop_event.is_set() and not rfile.closed:
                    data = await self.loop.run_in_executor(None, cast(io.BufferedReader, rfile).read1, 1000)
                    protocol.data_received(data)

            self.loop.run_until_complete(aio_readline(sys.__stdin__.buffer, protocol))

        self._run_func = run_io_nonblocking

    @_logger.call
    def start_tcp(self, host: Optional[str] = None, port: int = 0) -> None:
        self.mode = JsonRpcServerMode.TCP

        self._server = self.loop.run_until_complete(
            self.loop.create_server(lambda: self.create_protocol(), host, port, reuse_address=True)
        )

        self._run_func = self.loop.run_forever

    @_logger.call
    def run(self) -> None:
        if self._run_func is None:
            self._logger.warning("server is not started.")
            return
        self._run_func()
