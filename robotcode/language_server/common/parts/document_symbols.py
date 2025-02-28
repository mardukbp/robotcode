from __future__ import annotations

from asyncio import CancelledError
from typing import (
    TYPE_CHECKING,
    Any,
    List,
    Optional,
    Protocol,
    Union,
    cast,
    runtime_checkable,
)

from ....jsonrpc2.protocol import rpc_method
from ....utils.async_event import async_tasking_event
from ....utils.logging import LoggingDescriptor
from ..has_extend_capabilities import HasExtendCapabilities
from ..language import HasLanguageId
from ..lsp_types import (
    DocumentSymbol,
    DocumentSymbolClientCapabilitiesSymbolKind,
    DocumentSymbolClientCapabilitiesTagSupport,
    DocumentSymbolOptions,
    DocumentSymbolParams,
    ServerCapabilities,
    SymbolInformation,
    TextDocumentIdentifier,
)
from ..text_document import TextDocument

if TYPE_CHECKING:
    from ..protocol import LanguageServerProtocol

from .protocol_part import LanguageServerProtocolPart


@runtime_checkable
class HasSymbolInformationLabel(Protocol):
    symbol_information_label: str


class DocumentSymbolsProtocolPart(LanguageServerProtocolPart, HasExtendCapabilities):

    _logger = LoggingDescriptor()

    def __init__(self, parent: LanguageServerProtocol) -> None:
        super().__init__(parent)
        self.hierarchical_document_symbol_support = False
        self.symbol_kind: Optional[DocumentSymbolClientCapabilitiesSymbolKind] = None
        self.tag_support: Optional[DocumentSymbolClientCapabilitiesTagSupport] = None

    @async_tasking_event
    async def collect(
        sender, document: TextDocument
    ) -> Optional[Union[List[DocumentSymbol], List[SymbolInformation], None]]:
        ...

    def extend_capabilities(self, capabilities: ServerCapabilities) -> None:

        if (
            self.parent.client_capabilities is not None
            and self.parent.client_capabilities.text_document is not None
            and self.parent.client_capabilities.text_document.document_symbol is not None
        ):
            document_symbol = self.parent.client_capabilities.text_document.document_symbol

            label_suppport = document_symbol.label_support or False
            self.hierarchical_document_symbol_support = document_symbol.hierarchical_document_symbol_support or False
            self.symbol_kind = document_symbol.symbol_kind
            self.tag_support = document_symbol.tag_support

            if len(self.collect):
                if label_suppport:
                    label = (
                        cast(HasSymbolInformationLabel, self.parent).symbol_information_label
                        if isinstance(self.parent, HasSymbolInformationLabel)
                        else None
                    )

                    capabilities.document_symbol_provider = (
                        DocumentSymbolOptions(label=label) if label else DocumentSymbolOptions()
                    )
                else:
                    capabilities.document_symbol_provider = True

    @rpc_method(name="textDocument/documentSymbol", param_type=DocumentSymbolParams)
    async def _text_document_symbol(
        self, text_document: TextDocumentIdentifier, *args: Any, **kwargs: Any
    ) -> Optional[Union[List[DocumentSymbol], List[SymbolInformation], None]]:

        document_symbols: List[DocumentSymbol] = []
        symbol_informations: List[SymbolInformation] = []

        document = self.parent.documents.get(text_document.uri, None)
        if not document:
            return None

        for result in await self.collect(
            self,
            document,
            callback_filter=lambda c: not isinstance(c, HasLanguageId) or c.__language_id__ == document.language_id,
        ):
            if isinstance(result, BaseException):
                if not isinstance(result, CancelledError):
                    self._logger.exception(result, exc_info=result)
            else:
                if result is not None:
                    if all(isinstance(e, DocumentSymbol) for e in result):
                        document_symbols.extend(result)
                    elif all(isinstance(e, SymbolInformation) for e in result):
                        symbol_informations.extend(result)
                    else:
                        self._logger.warning(
                            "Result contains DocumentSymbol and SymbolInformation results, result is skipped."
                        )

        if document_symbols and symbol_informations:
            self._logger.warning(
                "Result contains DocumentSymbol and SymbolInformation results, only DocumentSymbols returned."
            )
            return document_symbols

        if document_symbols:
            return document_symbols

        if symbol_informations:
            return symbol_informations

        return None
