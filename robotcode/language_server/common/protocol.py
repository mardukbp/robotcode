from __future__ import annotations

import asyncio
from typing import Any, List, Optional, Union, cast

from ...jsonrpc2.protocol import (
    JsonRPCErrorException,
    JsonRPCErrors,
    JsonRPCException,
    JsonRPCProtocol,
    ProtocolPartDescriptor,
    rpc_method,
)
from ...jsonrpc2.server import JsonRPCServer
from ...utils.async_event import async_event
from ...utils.logging import LoggingDescriptor
from .has_extend_capabilities import HasExtendCapabilities
from .lsp_types import (
    CancelParams,
    ClientCapabilities,
    ClientInfo,
    InitializedParams,
    InitializeError,
    InitializeParams,
    InitializeResult,
    InitializeResultServerInfo,
    Registration,
    RegistrationParams,
    SaveOptions,
    ServerCapabilities,
    SetTraceParams,
    TextDocumentSyncKind,
    TextDocumentSyncOptions,
    TraceValue,
    Unregistration,
    UnregistrationParams,
    WorkspaceFolder,
)
from .parts.code_lens import CodeLensProtocolPart
from .parts.completion import CompletionProtocolPart
from .parts.declaration import DeclarationProtocolPart
from .parts.definition import DefinitionProtocolPart
from .parts.diagnostics import DiagnosticsProtocolPart
from .parts.document_symbols import DocumentSymbolsProtocolPart
from .parts.documents import TextDocumentProtocolPart
from .parts.folding_range import FoldingRangeProtocolPart
from .parts.formatting import FormattingProtocolPart
from .parts.hover import HoverProtocolPart
from .parts.implementation import ImplementationProtocolPart
from .parts.semantic_tokens import SemanticTokensProtocolPart
from .parts.signature_help import SignatureHelpProtocolPart
from .parts.window import WindowProtocolPart
from .parts.workspace import Workspace

__all__ = ["LanguageServerException", "LanguageServerProtocol", "HasExtendCapabilities"]


class LanguageServerException(JsonRPCException):
    pass


class LanguageServerProtocol(JsonRPCProtocol):

    _logger = LoggingDescriptor()

    window = ProtocolPartDescriptor(WindowProtocolPart)
    documents = ProtocolPartDescriptor(TextDocumentProtocolPart)
    diagnostics = ProtocolPartDescriptor(DiagnosticsProtocolPart)
    folding_ranges = ProtocolPartDescriptor(FoldingRangeProtocolPart)
    definition = ProtocolPartDescriptor(DefinitionProtocolPart)
    implementation = ProtocolPartDescriptor(ImplementationProtocolPart)
    declaration = ProtocolPartDescriptor(DeclarationProtocolPart)
    hover = ProtocolPartDescriptor(HoverProtocolPart)
    completion = ProtocolPartDescriptor(CompletionProtocolPart)
    signature_help = ProtocolPartDescriptor(SignatureHelpProtocolPart)
    code_lens = ProtocolPartDescriptor(CodeLensProtocolPart)
    document_symbols = ProtocolPartDescriptor(DocumentSymbolsProtocolPart)
    formatting = ProtocolPartDescriptor(FormattingProtocolPart)
    semantic_tokens = ProtocolPartDescriptor(SemanticTokensProtocolPart)

    name: Optional[str] = None
    version: Optional[str] = None

    def __init__(self, server: JsonRPCServer[Any]):
        super().__init__()
        self.server = server

        self.initialization_options: Any = None
        self.client_info: Optional[ClientInfo] = None
        self._workspace: Optional[Workspace] = None
        self.client_capabilities: Optional[ClientCapabilities] = None
        self.shutdown_received = False
        self._capabilities: Optional[ServerCapabilities] = None
        self._base_capabilities = ServerCapabilities(
            text_document_sync=TextDocumentSyncOptions(
                open_close=True,
                change=TextDocumentSyncKind.INCREMENTAL,
                will_save=True,
                will_save_wait_until=True,
                save=SaveOptions(include_text=True),
            )
        )

        self._trace = TraceValue.OFF

    @async_event
    async def on_shutdown(sender) -> None:
        ...

    @property
    def trace(self) -> TraceValue:
        return self._trace

    @trace.setter
    def trace(self, value: TraceValue) -> None:
        self._trace = value

    @property
    def workspace(self) -> Workspace:
        if self._workspace is None:
            raise LanguageServerException(f"{type(self).__name__} not initialized")

        return self._workspace

    @property
    def capabilities(self) -> ServerCapabilities:
        if self._capabilities is None:
            self._capabilities = self._collect_capabilities()
        return self._capabilities

    def _collect_capabilities(self) -> ServerCapabilities:
        from dataclasses import replace

        base_capabilities = replace(self._base_capabilities)

        for p in self.registry.parts:
            if isinstance(p, HasExtendCapabilities):
                cast(HasExtendCapabilities, p).extend_capabilities(base_capabilities)

        return base_capabilities

    @rpc_method(name="initialize", param_type=InitializeParams)
    @_logger.call
    async def _initialize(
        self,
        capabilities: ClientCapabilities,
        root_path: Optional[str] = None,
        root_uri: Optional[str] = None,
        initialization_options: Optional[Any] = None,
        trace: Optional[TraceValue] = None,
        client_info: Optional[ClientInfo] = None,
        workspace_folders: Optional[List[WorkspaceFolder]] = None,
        **kwargs: Any,
    ) -> InitializeResult:

        self.trace = trace or TraceValue.OFF
        self.client_info = client_info

        self.client_capabilities = capabilities

        self._workspace = Workspace(self, root_uri=root_uri, root_path=root_path, workspace_folders=workspace_folders)

        self.initialization_options = initialization_options
        try:
            await self.on_initialize(self, initialization_options)
        except (asyncio.CancelledError, SystemExit, KeyboardInterrupt):
            raise
        except JsonRPCErrorException:
            raise
        except BaseException as e:
            raise JsonRPCErrorException(
                JsonRPCErrors.INTERNAL_ERROR, f"Can't start language server: {e}", InitializeError(retry=False)
            ) from e

        return InitializeResult(
            capabilities=self.capabilities,
            **(
                {"server_info": InitializeResultServerInfo(name=self.name, version=self.version)}
                if self.name is not None
                else {}
            ),
        )

    @async_event
    async def on_initialize(sender, initialization_options: Optional[Any] = None) -> None:
        ...

    @rpc_method(name="initialized", param_type=InitializedParams)
    async def _initialized(self, params: InitializedParams) -> None:
        await self.on_initialized(self)

    @async_event
    async def on_initialized(sender) -> None:
        ...

    @rpc_method(name="shutdown")
    @_logger.call
    async def shutdown(self) -> None:
        self.shutdown_received = True

        try:
            await asyncio.wait_for(self.cancel_all_received_request(), 1)
        except BaseException:
            pass

        await self.on_shutdown(self)
        if self.server is not None:
            self.server.shutdown_protocol(self)

    @rpc_method(name="exit")
    @_logger.call
    async def _exit(self) -> None:
        raise SystemExit(0 if self.shutdown_received else 1)

    @rpc_method(name="$/setTrace", param_type=SetTraceParams)
    @_logger.call
    async def _set_trace(self, value: TraceValue, **kwargs: Any) -> None:
        self.trace = value

    @rpc_method(name="$/cancelRequest", param_type=CancelParams)
    @_logger.call
    async def _cancel_request(self, id: Union[int, str], **kwargs: Any) -> None:
        await self.cancel_received_request(id)

    async def register_capability(self, id: str, method: str, register_options: Optional[Any]) -> None:
        await self.register_capabilities([Registration(id=id, method=method, register_options=register_options)])

    async def register_capabilities(self, registrations: List[Registration]) -> None:
        if not registrations:
            return
        await self.send_request_async("client/registerCapability", RegistrationParams(registrations=registrations))

    async def unregister_capability(self, id: str, method: str) -> None:
        await self.unregister_capabilities([Unregistration(id=id, method=method)])

    async def unregister_capabilities(self, unregisterations: List[Unregistration]) -> None:
        if not unregisterations:
            return
        await self.send_request_async(
            "client/unregisterCapability", UnregistrationParams(unregisterations=unregisterations)
        )
