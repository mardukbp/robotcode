from __future__ import annotations

import asyncio
import inspect
import json
import logging
import threading
from collections import OrderedDict
from typing import (
    Any,
    Callable,
    Dict,
    Iterator,
    List,
    Mapping,
    Optional,
    Tuple,
    Type,
    TypeVar,
    Union,
)

from ..jsonrpc2.protocol import (
    JsonRPCException,
    JsonRPCProtocolBase,
    SendedRequestEntry,
)
from ..utils.dataclasses import as_dict, as_json, from_dict
from ..utils.inspect import ensure_coroutine
from ..utils.logging import LoggingDescriptor
from .dap_types import (
    ErrorBody,
    ErrorResponse,
    Event,
    Message,
    ProtocolMessage,
    Request,
    Response,
)


class DebugAdapterErrorResponseError(JsonRPCException):
    def __init__(
        self,
        error: ErrorResponse,
    ) -> None:
        super().__init__(
            f'{error.message} (seq={error.request_seq} command="{error.command}")'
            f'{f": {error.body.error}" if error.body is not None and error.body.error  else ""}',
        )
        self.error = error


class DebugAdapterRPCErrorException(JsonRPCException):
    def __init__(
        self,
        message: Optional[str] = None,
        request_seq: int = -1,
        command: str = "",
        success: Optional[bool] = None,
        error_message: Optional[Message] = None,
    ) -> None:
        super().__init__(
            f'{(message+" ") if message else ""}(seq={request_seq} command="{command}")'
            f'{f": {error_message}" if error_message else ""}'
        )
        self.message = message
        self.request_seq = request_seq
        self.command = command
        self.success = success
        self.error_message = error_message


TResult = TypeVar("TResult", bound=Any)


class DebugAdapterProtocol(JsonRPCProtocolBase):

    _logger = LoggingDescriptor()
    _message_logger = LoggingDescriptor(postfix=".message")

    def __init__(self) -> None:
        super().__init__()
        self._sended_request_lock = threading.RLock()
        self._sended_request: OrderedDict[int, SendedRequestEntry] = OrderedDict()
        self._received_request_lock = threading.RLock()
        self._received_request: OrderedDict[int, asyncio.Future[Any]] = OrderedDict()
        self._initialized = False

    @_logger.call
    def send_message(self, message: ProtocolMessage) -> None:
        body = as_json(message, indent=self._message_logger.is_enabled_for(logging.DEBUG) or None).encode(self.CHARSET)

        header = (f"Content-Length: {len(body)}\r\n\r\n").encode("ascii")

        self._message_logger.debug(
            lambda: "write ->\n" + (header.decode("ascii") + body.decode(self.CHARSET)).replace("\r\n", "\n")
        )

        if self.write_transport is not None:
            self.write_transport.write(header + body)

    def send_error(
        self,
        message: Optional[str] = None,
        request_seq: int = -1,
        command: str = "",
        success: Optional[bool] = None,
        error_message: Optional[Message] = None,
    ) -> None:
        self.send_message(
            ErrorResponse(
                success=success or False,
                request_seq=request_seq,
                message=message,
                command=command,
                body=ErrorBody(error=error_message),
            )
        )

    @staticmethod
    def _generate_json_rpc_messages_from_dict(
        data: Union[Dict[Any, Any], List[Dict[Any, Any]]]
    ) -> Iterator[ProtocolMessage]:
        def inner(d: Dict[Any, Any]) -> ProtocolMessage:
            # if "type" in d:
            #     type = d.get("type")
            #     if type == "request":
            #         return Request(**d)
            #     elif type == "response":
            #         if "success" in d and d["success"] is not True:
            #             return ErrorResponse(**d)
            #         return Response(**d)
            #     elif type == "event":
            #         return Event(**d)

            # raise JsonRPCException(f"Invalid Debug Adapter Message {repr(d)}")
            return from_dict(d, (Request, Response, Event))  # type: ignore

        if isinstance(data, list):
            for e in data:
                yield inner(e)
        else:
            yield inner(data)

    def _handle_body(self, body: bytes, charset: str) -> None:
        try:
            self._handle_messages(self._generate_json_rpc_messages_from_dict(json.loads(body.decode(charset))))
        except (asyncio.CancelledError, SystemExit, KeyboardInterrupt):
            raise
        except BaseException as e:
            self._logger.exception(e)
            self.send_error(f"Invalid Message: {type(e).__name__}: {str(e)} -> {str(body)}")

    def _handle_messages(self, iterator: Iterator[ProtocolMessage]) -> None:
        def done(f: asyncio.Future[Any]) -> None:
            ex = f.exception()
            if ex is not None and not isinstance(ex, asyncio.CancelledError):
                self._logger.exception(ex, exc_info=ex)

        for m in iterator:
            task = asyncio.create_task(self.handle_message(m))
            task.add_done_callback(done)

    @_logger.call
    async def handle_message(self, message: ProtocolMessage) -> None:
        if isinstance(message, Request):
            await self.handle_request(message)
        if isinstance(message, Event):
            await self.handle_event(message)
        elif isinstance(message, ErrorResponse):
            await self.handle_error_response(message)
        elif isinstance(message, Response):
            await self.handle_response(message)

    @staticmethod
    def _convert_params(
        callable: Callable[..., Any], param_type: Optional[Type[Any]], params: Any
    ) -> Tuple[List[Any], Dict[str, Any]]:
        if params is None:
            return [], {}
        if param_type is None:
            if isinstance(params, Mapping):
                return [], dict(**params)
            else:
                return [params], {}

        converted_params = from_dict(params, param_type)

        signature = inspect.signature(callable)

        has_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in signature.parameters.values())

        kw_args = {}
        args = []
        params_added = False
        rest = set(converted_params.__dict__.keys())
        if isinstance(params, dict):
            rest = set.union(rest, params.keys())

        for v in signature.parameters.values():
            if v.name in converted_params.__dict__:
                if v.kind == inspect.Parameter.POSITIONAL_ONLY:
                    args.append(getattr(converted_params, v.name))
                else:
                    kw_args[v.name] = getattr(converted_params, v.name)
                rest.remove(v.name)
            elif v.name == "arguments":
                if v.kind == inspect.Parameter.POSITIONAL_ONLY:
                    args.append(converted_params)
                    params_added = True
                else:
                    kw_args[v.name] = converted_params
                    params_added = True
            elif isinstance(params, dict) and v.name in params:
                if v.kind == inspect.Parameter.POSITIONAL_ONLY:
                    args.append(params[v.name])
                else:
                    kw_args[v.name] = params[v.name]
        if has_var_kw:
            for r in rest:
                if hasattr(converted_params, r):
                    kw_args[r] = getattr(converted_params, r)
                elif isinstance(params, dict) and r in params:
                    kw_args[r] = params[r]

            if not params_added:
                kw_args["arguments"] = converted_params
        return args, kw_args

    async def handle_unknown_command(self, message: Request) -> Any:
        raise DebugAdapterRPCErrorException(
            f"Unknown Command '{message.command}'",
            error_message=Message(
                format='Unknown command "{command}"', variables={"command": str(message.command)}, show_user=True
            ),
        )

    @_logger.call
    async def handle_request(self, message: Request) -> None:
        e = self.registry.get_entry(message.command)

        try:
            if e is None or not callable(e.method):
                result = asyncio.create_task(self.handle_unknown_command(message))
            else:
                params = self._convert_params(e.method, e.param_type, message.arguments)

                result = asyncio.create_task(ensure_coroutine(e.method)(*params[0], **params[1]))

            with self._received_request_lock:
                self._received_request[message.seq] = result

            try:
                self.send_response(message.seq, message.command, await result)
            finally:
                with self._received_request_lock:
                    self._received_request.pop(message.seq, None)

        except asyncio.CancelledError:
            self._logger.info(f"request message {repr(message)} canceled")
        except (SystemExit, KeyboardInterrupt):
            raise
        except DebugAdapterRPCErrorException as ex:
            self._logger.exception(ex)
            self.send_error(
                message=ex.message,
                request_seq=message.seq,
                command=ex.command or message.command,
                success=ex.success or False,
                error_message=ex.error_message,
            )
        except DebugAdapterErrorResponseError as ex:
            self.send_error(
                ex.error.message,
                message.seq,
                message.command,
                False,
                error_message=ex.error.body.error if ex.error.body is not None else None,
            )
        except BaseException as e:
            self._logger.exception(e)
            self.send_error(
                str(type(e).__name__),
                message.seq,
                message.command,
                False,
                error_message=Message(format=f"{type(e).__name__}: {e}", show_user=True),
            )

    @_logger.call
    def send_response(
        self,
        request_seq: int,
        command: str,
        result: Optional[Any] = None,
        success: bool = True,
        message: Optional[str] = None,
    ) -> None:
        self.send_message(
            Response(request_seq=request_seq, command=command, success=success, message=message, body=result)
        )

    def send_request(
        self,
        request: Request,
        return_type: Optional[Type[TResult]] = None,
    ) -> asyncio.Future[TResult]:

        result: asyncio.Future[TResult] = asyncio.get_event_loop().create_future()

        with self._sended_request_lock:
            self._sended_request[request.seq] = SendedRequestEntry(result, return_type)

        self.send_message(request)

        return result

    async def send_request_async(
        self,
        request: Request,
        return_type: Optional[Type[TResult]] = None,
    ) -> TResult:
        return await self.send_request(request, return_type)

    @_logger.call
    def send_event(self, event: Event) -> None:
        self.send_message(event)

    @_logger.call
    async def send_event_async(self, event: Event) -> None:
        self.send_event(event)

    @_logger.call
    async def handle_error_response(self, message: ErrorResponse) -> None:
        with self._sended_request_lock:
            entry = self._sended_request.pop(message.request_seq, None)

        exception = DebugAdapterErrorResponseError(message)
        if entry is None:
            raise exception

        try:
            entry.future.set_exception(exception)
        except (SystemExit, KeyboardInterrupt):
            raise

    @_logger.call
    async def handle_response(self, message: Response) -> None:
        with self._sended_request_lock:
            entry = self._sended_request.pop(message.request_seq, None)

        if entry is None:
            error = f"Invalid response. Could not find id '{message.request_seq}' in our request list"
            self._logger.warning(error)
            self.send_error("invalid response", error_message=Message(format=error, show_user=True))
            return

        try:
            if message.success:
                if not entry.future.done():
                    entry.future.set_result(from_dict(message.body, entry.result_type))
            else:
                raise DebugAdapterErrorResponseError(ErrorResponse(**as_dict(message)))
        except (SystemExit, KeyboardInterrupt):
            raise
        except BaseException as e:
            if not entry.future.done():
                entry.future.set_exception(e)

    @_logger.call
    async def handle_event(self, message: Event) -> None:
        raise NotImplementedError()
