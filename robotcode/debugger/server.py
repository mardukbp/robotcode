import asyncio
import os
from typing import Any, Literal, Optional, Union

from ..jsonrpc2.protocol import rpc_method
from ..jsonrpc2.server import JsonRPCServer, JsonRpcServerMode, TcpParams
from ..utils.logging import LoggingDescriptor
from .dap_types import (
    ConfigurationDoneArguments,
    ContinueArguments,
    ContinueResponseBody,
    DisconnectArguments,
    EvaluateArgumentContext,
    EvaluateArguments,
    EvaluateResponseBody,
    Event,
    ExitedEvent,
    ExitedEventBody,
    InitializedEvent,
    NextArguments,
    PauseArguments,
    ScopesArguments,
    ScopesResponseBody,
    SetBreakpointsArguments,
    SetBreakpointsResponseBody,
    SetExceptionBreakpointsArguments,
    SetExceptionBreakpointsResponseBody,
    SetVariableArguments,
    SetVariableResponseBody,
    StackTraceArguments,
    StackTraceResponseBody,
    StepInArguments,
    StepOutArguments,
    TerminateArguments,
    TerminatedEvent,
    ThreadsResponseBody,
    ValueFormat,
    VariablesArguments,
    VariablesResponseBody,
)
from .debugger import Debugger
from .protocol import DebugAdapterProtocol

TCP_DEFAULT_PORT = 6612


class LauncherServerProtocol(DebugAdapterProtocol):
    _logger = LoggingDescriptor()

    def __init__(self) -> None:
        super().__init__()
        self._loop = asyncio.get_event_loop()

        self._initialized = False
        self._connected_event = asyncio.Event()
        self._connected = False
        self._sigint_signaled = False

        self._exited_lock = asyncio.Lock()
        self._exited = False

        self._terminated_lock = asyncio.Lock()
        self._terminated = False

        self._received_configuration_done_event = asyncio.Event()
        self._received_configuration_done = False

        Debugger.instance().send_event.add(self.on_debugger_send_event)

    def on_debugger_send_event(self, sender: Any, event: Event) -> None:
        self._loop.call_soon_threadsafe(self.send_event, event)

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    async def exited(self) -> bool:
        async with self._exited_lock:
            return self._exited

    @property
    async def terminated(self) -> bool:
        async with self._terminated_lock:
            return self._terminated

    @_logger.call
    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        if self.connected:
            raise ConnectionError("Protocol already connected, only one conntection allowed.")

        super().connection_made(transport)

        if self.read_transport is not None and self.write_transport is not None:
            self._connected = True
            self._connected_event.set()

    @_logger.call
    def connection_lost(self, exc: Optional[BaseException]) -> None:
        super().connection_lost(exc)

        self._connected = False

    @_logger.call
    async def wait_for_client(self, timeout: float = 5) -> bool:
        await asyncio.wait_for(self._connected_event.wait(), timeout)

        return self._connected

    @_logger.call
    async def initialized(self) -> None:
        await self.send_event_async(InitializedEvent())

    @_logger.call
    async def exit(self, exit_code: int) -> None:
        async with self._exited_lock:
            await self.send_event_async(ExitedEvent(body=ExitedEventBody(exit_code=exit_code)))
            self._exited = True

    @_logger.call
    async def terminate(self) -> None:
        async with self._terminated_lock:
            await self.send_event_async(TerminatedEvent())
            self._terminated = True

    @rpc_method(name="terminate", param_type=TerminateArguments)
    async def _terminate(self, arguments: Optional[TerminateArguments] = None) -> None:
        import signal

        if not self._sigint_signaled:
            self._logger.info("Send SIGINT to process")
            signal.raise_signal(signal.SIGINT)
            self._sigint_signaled = True
        else:
            self._logger.info("Send SIGTERM to process")
            signal.raise_signal(signal.SIGTERM)

        Debugger.instance().stop()

    @rpc_method(name="disconnect", param_type=DisconnectArguments)
    async def _disconnect(self, arguments: Optional[DisconnectArguments] = None) -> None:
        if not (await self.exited) or not (await self.terminated):
            if arguments is None or arguments.terminate_debuggee is None or arguments.terminate_debuggee:
                os._exit(-1)

    @rpc_method(name="setBreakpoints", param_type=SetBreakpointsArguments)
    async def _set_breakpoints(self, arguments: SetBreakpointsArguments) -> SetBreakpointsResponseBody:
        return SetBreakpointsResponseBody(
            breakpoints=Debugger.instance().set_breakpoints(
                arguments.source, arguments.breakpoints, arguments.lines, arguments.source_modified
            )
        )

    @_logger.call
    @rpc_method(name="configurationDone", param_type=ConfigurationDoneArguments)
    async def _configuration_done(self, arguments: Optional[ConfigurationDoneArguments] = None) -> None:
        self._received_configuration_done = True
        self._received_configuration_done_event.set()

    @_logger.call
    async def wait_for_configuration_done(self, timeout: float = 5) -> bool:
        await asyncio.wait_for(self._received_configuration_done_event.wait(), timeout)

        return self._received_configuration_done

    @rpc_method(name="continue", param_type=ContinueArguments)
    async def _continue(self, arguments: ContinueArguments) -> ContinueResponseBody:
        Debugger.instance().continue_thread(arguments.thread_id)
        return ContinueResponseBody(all_threads_continued=True)

    @rpc_method(name="pause", param_type=PauseArguments)
    async def _pause(self, arguments: PauseArguments) -> None:
        Debugger.instance().pause_thread(arguments.thread_id)

    @rpc_method(name="next", param_type=NextArguments)
    async def _next(self, arguments: NextArguments) -> None:
        Debugger.instance().next(arguments.thread_id, arguments.granularity)

    @rpc_method(name="stepIn", param_type=StepInArguments)
    async def _step_in(self, arguments: StepInArguments) -> None:
        Debugger.instance().step_in(arguments.thread_id, arguments.target_id, arguments.granularity)

    @rpc_method(name="stepOut", param_type=StepOutArguments)
    async def _step_out(self, arguments: StepOutArguments) -> None:
        Debugger.instance().step_out(arguments.thread_id, arguments.granularity)

    @rpc_method(name="threads")
    async def _threads(self) -> ThreadsResponseBody:
        return ThreadsResponseBody(threads=Debugger.instance().get_threads())

    @rpc_method(name="stackTrace", param_type=StackTraceArguments)
    async def _stack_trace(self, arguments: StackTraceArguments) -> StackTraceResponseBody:
        result = Debugger.instance().get_stack_trace(
            arguments.thread_id, arguments.start_frame, arguments.levels, arguments.format
        )
        return StackTraceResponseBody(stack_frames=result.stack_frames, total_frames=result.total_frames)

    @rpc_method(name="scopes", param_type=ScopesArguments)
    async def _scopes(self, arguments: ScopesArguments) -> ScopesResponseBody:
        return ScopesResponseBody(scopes=Debugger.instance().get_scopes(arguments.frame_id))

    @rpc_method(name="variables", param_type=VariablesArguments)
    async def _variables(
        self,
        arguments: VariablesArguments,
        variables_reference: int,
        filter: Optional[Literal["indexed", "named"]] = None,
        start: Optional[int] = None,
        count: Optional[int] = None,
        format: Optional[ValueFormat] = None,
    ) -> VariablesResponseBody:
        return VariablesResponseBody(
            variables=Debugger.instance().get_variables(variables_reference, filter, start, count, format)
        )

    @rpc_method(name="evaluate", param_type=EvaluateArguments)
    async def _evaluate(
        self,
        arguments: ScopesArguments,
        expression: str,
        frame_id: Optional[int] = None,
        context: Union[EvaluateArgumentContext, str, None] = None,
        format: Optional[ValueFormat] = None,
    ) -> EvaluateResponseBody:
        result = Debugger.instance().evaluate(expression, frame_id, context, format)
        return EvaluateResponseBody(
            result=result.result,
            type=result.type,
            presentation_hint=result.presentation_hint,
            variables_reference=result.variables_reference,
            named_variables=result.named_variables,
            indexed_variables=result.indexed_variables,
            memory_reference=result.memory_reference,
        )

    @rpc_method(name="setVariable", param_type=SetVariableArguments)
    async def _set_variable(
        self,
        arguments: SetVariableArguments,
        variables_reference: int,
        name: str,
        value: str,
        format: Optional[ValueFormat] = None,
    ) -> SetVariableResponseBody:
        result = Debugger.instance().set_variable(variables_reference, name, value, format)
        return SetVariableResponseBody(
            value=result.value,
            type=result.type,
            variables_reference=result.variables_reference,
            named_variables=result.named_variables,
            indexed_variables=result.indexed_variables,
        )

    @rpc_method(name="setExceptionBreakpoints", param_type=SetExceptionBreakpointsArguments)
    async def _set_exception_breakpoints(
        self, arguments: SetExceptionBreakpointsArguments
    ) -> Optional[SetExceptionBreakpointsResponseBody]:
        result = Debugger.instance().set_exception_breakpoints(
            arguments.filters, arguments.filter_options, arguments.exception_options
        )
        return SetExceptionBreakpointsResponseBody(breakpoints=result) if result else None


class LaucherServer(JsonRPCServer[LauncherServerProtocol]):
    def __init__(
        self,
        tcp_params: TcpParams = TcpParams(None, TCP_DEFAULT_PORT),
    ):
        super().__init__(
            mode=JsonRpcServerMode.TCP,
            tcp_params=tcp_params,
        )
        self.protocol = LauncherServerProtocol()

    def create_protocol(self) -> LauncherServerProtocol:
        return self.protocol
