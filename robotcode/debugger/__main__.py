import argparse
import asyncio
import logging
import logging.config
import os
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import TYPE_CHECKING, Any, List, Optional, cast

__file__ = os.path.abspath(__file__)
if __file__.endswith((".pyc", ".pyo")):
    __file__ = __file__[:-1]

if __name__ == "__main__" and __package__ is None or __package__ == "":

    file = Path(__file__).resolve()
    parent, top = file.parent, file.parents[2]

    if str(top) not in sys.path:
        sys.path.append(str(top))

    try:
        sys.path.remove(str(parent))
    except ValueError:  # Already removed
        pass

    __package__ = "robotcode.debugger"

from .._version import __version__
from ..utils.logging import LoggingDescriptor

TRACE = logging.DEBUG - 6
logging.addLevelName(TRACE, "TRACE")
LoggingDescriptor.set_call_tracing_default_level(TRACE)

_logger = LoggingDescriptor(name=__package__)

if TYPE_CHECKING:
    from .server import LaucherServer

server_lock = threading.RLock()
_server: Optional["LaucherServer"] = None


def get_server() -> Optional["LaucherServer"]:
    with server_lock:
        return _server


def set_server(value: "LaucherServer") -> None:
    with server_lock:
        global _server
        _server = value


@_logger.call
async def wait_for_server(timeout: float = 5) -> "LaucherServer":
    async def wait() -> None:
        while get_server() is None:
            await asyncio.sleep(0.05)

    await asyncio.wait_for(wait(), timeout)

    result = get_server()
    assert result is not None
    return result


@_logger.call
def run_server(port: int, loop: asyncio.AbstractEventLoop) -> None:
    from ..jsonrpc2.server import TcpParams
    from .server import LaucherServer

    asyncio.set_event_loop(loop)

    with LaucherServer(tcp_params=TcpParams("127.0.0.1", port)) as server:
        set_server(cast(LaucherServer, server))
        try:
            server.run()
        except asyncio.CancelledError:
            pass
        except (SystemExit, KeyboardInterrupt):
            raise
        except BaseException as e:
            _logger.exception(e)


DEFAULT_TIMEOUT = 5.0


@_logger.call
async def run_robot(
    port: int,
    args: List[str],
    no_debug: bool = False,
    wait_for_client: bool = False,
    wait_for_client_timeout: float = DEFAULT_TIMEOUT,
    configuration_done_timeout: float = DEFAULT_TIMEOUT,
    debugpy: bool = False,
    wait_for_debugpy_client: bool = False,
    debugpy_port: int = 5678,
    output_messages: bool = False,
    output_log: bool = False,
    group_output: bool = False,
    stop_on_entry: bool = False,
) -> Any:
    import robot

    from ..utils.debugpy import enable_debugpy, wait_for_debugpy_connected
    from ..utils.net import check_free_port
    from .dap_types import Event
    from .debugger import Debugger

    @_logger.call
    async def start_debugpy_async() -> None:
        if debugpy:
            port = check_free_port(debugpy_port)
            if enable_debugpy(port):
                if await asyncio.wrap_future(
                    asyncio.run_coroutine_threadsafe(
                        server.protocol.wait_for_client(wait_for_client_timeout), loop=loop
                    )
                ):
                    await asyncio.wrap_future(
                        asyncio.run_coroutine_threadsafe(
                            server.protocol.send_event_async(Event(event="debugpyStarted", body={"port": port})),
                            loop=loop,
                        )
                    )
                    if wait_for_debugpy_client:
                        wait_for_debugpy_connected()

    loop = asyncio.new_event_loop()

    thread = threading.Thread(name="RobotCode Debugger", target=run_server, args=(port, loop))
    thread.daemon = True
    thread.start()

    server = await wait_for_server()

    try:
        if wait_for_client:
            try:
                await asyncio.wrap_future(
                    asyncio.run_coroutine_threadsafe(
                        server.protocol.wait_for_client(wait_for_client_timeout), loop=loop
                    )
                )
            except (asyncio.CancelledError, SystemExit, KeyboardInterrupt):
                pass
            except asyncio.TimeoutError:
                raise ConnectionError("No incomming connection from a debugger client.")

        await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(server.protocol.initialized(), loop=loop))

        if wait_for_client:
            try:
                await asyncio.wrap_future(
                    asyncio.run_coroutine_threadsafe(
                        server.protocol.wait_for_configuration_done(configuration_done_timeout), loop=loop
                    )
                )
            except (asyncio.CancelledError, SystemExit, KeyboardInterrupt):
                pass
            except asyncio.TimeoutError:
                raise ConnectionError("Timeout to get configuration from client.")

        await start_debugpy_async()

        args = [
            "--listener",
            f"robotcode.debugger.listeners.ListenerV2:no_debug={repr(no_debug)}",
            "--listener",
            "robotcode.debugger.listeners.ListenerV3",
            *args,
        ]

        Debugger.instance().stop_on_entry = stop_on_entry
        Debugger.instance().output_messages = output_messages
        Debugger.instance().output_log = output_log
        Debugger.instance().group_output = group_output
        Debugger.instance().no_debug = no_debug
        Debugger.instance().set_main_thread(threading.current_thread())
        Debugger.instance().start()

        exit_code = robot.run_cli(args, False)

        if server.protocol.connected:
            await asyncio.wrap_future(
                asyncio.run_coroutine_threadsafe(
                    server.protocol.send_event_async(
                        Event(
                            event="robotExited",
                            body={
                                "reportFile": Debugger.instance().robot_report_file,
                                "logFile": Debugger.instance().robot_log_file,
                                "outputFile": Debugger.instance().robot_output_file,
                                "exitCode": exit_code,
                            },
                        )
                    ),
                    loop=loop,
                )
            )

        if server.protocol.connected:
            await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(server.protocol.exit(exit_code), loop=loop))

        return exit_code
    except asyncio.CancelledError:
        pass
    except BaseException as e:
        _logger.exception(e, level=logging.INFO)
        raise
    finally:
        if server.protocol.connected:
            await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(server.protocol.terminate(), loop=loop))

        async def server_close() -> None:
            server.close()

        await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(server_close(), loop=loop))

        loop.call_soon_threadsafe(loop.stop)

        async def wait_loop_is_not_running() -> None:
            while loop.is_running():
                await asyncio.sleep(0.05)

        try:
            await asyncio.wait_for(wait_loop_is_not_running(), timeout=5)
        except asyncio.TimeoutError:
            _logger.warning("debug loop is running")
            sys.exit(-1)


def get_log_handler(logfile: str) -> logging.FileHandler:
    log_fn = Path(logfile)
    roll_over = log_fn.exists()

    handler = RotatingFileHandler(log_fn, backupCount=5)
    formatter = logging.Formatter(
        fmt="[%(levelname)-7s] %(asctime)s (%(name)s) %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    handler.setFormatter(formatter)

    if roll_over:
        handler.doRollover()

    return handler


def main() -> None:
    parser = argparse.ArgumentParser(
        description="RobotCode Debugger",
        prog=__package__,
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    parser.add_argument("--version", action="store_true", help="shows the version and exits")
    parser.add_argument("-p", "--port", default=6612, help="server listen port (tcp)", type=int)
    parser.add_argument("-w", "--wait-for-client", action="store_true", help="waits for an debug client to connect")
    parser.add_argument(
        "-t",
        "--wait-for-client-timeout",
        default=DEFAULT_TIMEOUT,
        type=float,
        metavar="TIMEOUT",
        help="timeout to wait for an debug client to connect",
    )
    parser.add_argument(
        "-c",
        "--configuration-done-timeout",
        default=DEFAULT_TIMEOUT,
        type=float,
        metavar="TIMEOUT",
        help="timeout to wait for a configuration from client",
    )
    parser.add_argument("--log", action="store_true", help="enable logging")
    parser.add_argument("--log-debugger", action="store_true", help="show debugger log messages")
    parser.add_argument("-n", "--no-debug", action="store_true", help="disable debugging")
    parser.add_argument("--debug-asyncio", action="store_true", help="enable async io debugging messages")
    parser.add_argument("--log-asyncio", action="store_true", help="show asyncio log messages")
    parser.add_argument("--log-colored", action="store_true", help="colored output for logs")
    parser.add_argument("--log-config", default=None, help="reads logging configuration from file", metavar="FILE")
    parser.add_argument("--log-file", default=None, help="enables logging to file", metavar="FILE")
    parser.add_argument("--log-level", default="WARNING", help="sets the overall log level", metavar="LEVEL")
    parser.add_argument("--call-tracing", action="store_true", help="enables log tracing of method calls")
    parser.add_argument(
        "--call-tracing-default-level", default="TRACE", help="sets the default level for call tracing", metavar="LEVEL"
    )
    parser.add_argument("-d", "--debugpy", action="store_true", help="starts a debugpy session")
    parser.add_argument(
        "-dp", "--debugpy-port", default=6613, help="sets the port for debugpy session", type=int, metavar="PORT"
    )
    parser.add_argument(
        "-dw", "--debugpy-wait-for-client", action="store_true", help="waits for debugpy client to connect"
    )
    parser.add_argument(
        "-om", "--output-messages", action="store_true", help="Send output messages from robotframework to client."
    )
    parser.add_argument(
        "-ol", "--output-log", action="store_true", help="Send log messages from robotframework to client."
    )
    parser.add_argument(
        "-og", "--group-output", action="store_true", help="Fold messages/log from robotframework to client."
    )
    parser.add_argument("-soe", "--stop-on-entry", action="store_true", help="Stops on entry.")

    parser.add_argument("--", help="RobotFramework arguments. (see robot --help)", dest="robot args", nargs="*")

    sys_args = sys.argv[1:]

    split_index = sys_args.index("--") if "--" in sys_args else -1

    my_args = sys_args[:split_index] if split_index >= 0 else sys_args
    robot_args = sys_args[split_index + 1 :] if split_index >= 0 else []  # noqa: E203

    args = parser.parse_args(my_args)

    if args.version:
        print(__version__)
        return

    if split_index == -1:
        parser.print_help()
        return

    if args.call_tracing:
        LoggingDescriptor.set_call_tracing(True)
    if args.call_tracing_default_level:
        LoggingDescriptor.set_call_tracing_default_level(
            logging._checkLevel(args.call_tracing_default_level)  # type: ignore
        )

    if args.debug_asyncio:
        os.environ["PYTHONASYNCIODEBUG"] = "1"
        logging.getLogger("asyncio").level = logging.DEBUG
    else:
        logging.getLogger("asyncio").level = logging.CRITICAL

    if args.log_config is not None:
        if not os.path.exists(args.log_config):
            raise FileNotFoundError(f"Log-config file '{args.log_config}' not exists.")

        logging.config.fileConfig(args.log_config, disable_existing_loggers=True)
    else:
        log_level = logging._checkLevel(args.log_level) if args.log else logging.WARNING  # type: ignore

        log_initialized = False
        if args.log_colored:
            try:
                import coloredlogs

                coloredlogs.install(level=log_level)
                log_initialized = True
            except ImportError:
                pass

        if not log_initialized:
            logging.basicConfig(level=log_level)

        if args.log_file is not None:
            _logger.logger.addHandler(get_log_handler(args.log_file))

        if not args.log_asyncio:
            logging.getLogger("asyncio").propagate = False

        if not args.log_debugger:
            logging.getLogger("robotcode.debugger").propagate = False
            logging.getLogger("robotcode.debugger").disabled = True

    _logger.info(f"starting {__package__} version={__version__}")
    _logger.debug(f"args={args}")

    asyncio.run(
        run_robot(
            args.port,
            robot_args,
            args.no_debug,
            args.wait_for_client,
            args.wait_for_client_timeout,
            args.configuration_done_timeout,
            args.debugpy,
            args.debugpy_wait_for_client,
            args.debugpy_port,
            args.output_messages,
            args.output_log,
            args.group_output,
            args.stop_on_entry,
        )
    )


if __name__ == "__main__":
    main()
