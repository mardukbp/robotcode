from __future__ import annotations

import asyncio
import uuid
import weakref
from dataclasses import dataclass
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Coroutine,
    Dict,
    List,
    Mapping,
    NamedTuple,
    Optional,
    Protocol,
    Tuple,
    Type,
    TypeVar,
    Union,
    cast,
    runtime_checkable,
)

from ....jsonrpc2.protocol import rpc_method
from ....utils.async_event import async_event
from ....utils.dataclasses import from_dict
from ....utils.logging import LoggingDescriptor
from ....utils.path import path_is_relative_to
from ....utils.uri import Uri
from ..lsp_types import (
    ConfigurationItem,
    ConfigurationParams,
    CreateFilesParams,
    DeleteFilesParams,
    DidChangeConfigurationParams,
    DidChangeWatchedFilesParams,
    DidChangeWatchedFilesRegistrationOptions,
    DidChangeWorkspaceFoldersParams,
    DocumentUri,
    FileCreate,
    FileDelete,
    FileEvent,
    FileOperationFilter,
    FileOperationPattern,
    FileOperationRegistrationOptions,
    FileRename,
    FileSystemWatcher,
    Model,
    RenameFilesParams,
    ServerCapabilities,
    ServerCapabilitiesWorkspace,
    ServerCapabilitiesWorkspaceFileOperations,
    TextEdit,
    WatchKind,
    WorkspaceEdit,
)
from ..lsp_types import WorkspaceFolder as TypesWorkspaceFolder
from ..lsp_types import WorkspaceFoldersChangeEvent, WorkspaceFoldersServerCapabilities
from .protocol_part import LanguageServerProtocolPart

__all__ = ["WorkspaceFolder", "Workspace", "ConfigBase", "config_section", "FileWatcherEntry"]

if TYPE_CHECKING:
    from ..protocol import LanguageServerProtocol


class FileWatcher(NamedTuple):
    glob_pattern: str
    kind: Optional[WatchKind] = None


class FileWatcherEntry:
    def __init__(
        self,
        id: str,
        callback: Callable[[Any, List[FileEvent]], Coroutine[Any, Any, None]],
        watchers: List[FileWatcher],
    ) -> None:
        self.id = id
        self.callback = callback
        self.watchers = watchers
        self.parent: Optional[FileWatcherEntry] = None
        self.finalizer: Any = None

    @async_event
    async def child_callbacks(sender, changes: List[FileEvent]) -> None:
        ...

    async def call_childrens(self, sender: Any, changes: List[FileEvent]) -> None:
        await self.child_callbacks(sender, changes)

    def __str__(self) -> str:
        return self.id

    def __repr__(self) -> str:
        return f"{type(self).__qualname__}(id={repr(self.id)}, watchers={repr(self.watchers)})"


class WorkspaceFolder:
    def __init__(self, name: str, uri: Uri, document_uri: DocumentUri) -> None:
        super().__init__()
        self.name = name
        self.uri = uri
        self.document_uri = document_uri


def config_section(name: str) -> Callable[[_F], _F]:
    def decorator(func: _F) -> _F:
        setattr(func, "__config_section__", name)
        return func

    return decorator


@runtime_checkable
class HasConfigSection(Protocol):
    __config_section__: str


@dataclass
class ConfigBase(Model):
    pass


_TConfig = TypeVar("_TConfig", bound=(ConfigBase))
_F = TypeVar("_F", bound=Callable[..., Any])


class Workspace(LanguageServerProtocolPart):
    _logger = LoggingDescriptor()

    def __init__(
        self,
        parent: LanguageServerProtocol,
        root_uri: Optional[str],
        root_path: Optional[str],
        workspace_folders: Optional[List[TypesWorkspaceFolder]] = None,
    ):
        super().__init__(parent)
        self.root_uri = root_uri
        self.root_path = root_path
        self.workspace_folders_lock = asyncio.Lock()
        self.workspace_folders: List[WorkspaceFolder] = (
            [WorkspaceFolder(w.name, Uri(w.uri), w.uri) for w in workspace_folders]
            if workspace_folders is not None
            else []
        )
        self._settings: Dict[str, Any] = {}

        self._file_watchers: weakref.WeakSet[FileWatcherEntry] = weakref.WeakSet()
        self._loop = asyncio.get_event_loop()

    def extend_capabilities(self, capabilities: ServerCapabilities) -> None:
        capabilities.workspace = ServerCapabilitiesWorkspace(
            workspace_folders=WorkspaceFoldersServerCapabilities(
                supported=True, change_notifications=str(uuid.uuid4())
            ),
            file_operations=ServerCapabilitiesWorkspaceFileOperations(
                did_create=FileOperationRegistrationOptions(
                    filters=[FileOperationFilter(pattern=FileOperationPattern(glob="**/*"))]
                ),
                will_create=FileOperationRegistrationOptions(
                    filters=[FileOperationFilter(pattern=FileOperationPattern(glob="**/*"))]
                ),
                did_rename=FileOperationRegistrationOptions(
                    filters=[FileOperationFilter(pattern=FileOperationPattern(glob="**/*"))]
                ),
                will_rename=FileOperationRegistrationOptions(
                    filters=[FileOperationFilter(pattern=FileOperationPattern(glob="**/*"))]
                ),
                did_delete=FileOperationRegistrationOptions(
                    filters=[FileOperationFilter(pattern=FileOperationPattern(glob="**/*"))]
                ),
                will_delete=FileOperationRegistrationOptions(
                    filters=[FileOperationFilter(pattern=FileOperationPattern(glob="**/*"))]
                ),
            ),
        )

    @property
    def settings(self) -> Dict[str, Any]:
        return self._settings

    @settings.setter
    def settings(self, value: Dict[str, Any]) -> None:
        self._settings = value

    @async_event
    async def did_change_configuration(sender, settings: Dict[str, Any]) -> None:
        ...

    @rpc_method(name="workspace/didChangeConfiguration", param_type=DidChangeConfigurationParams)
    @_logger.call
    async def _workspace_did_change_configuration(self, settings: Dict[str, Any], *args: Any, **kwargs: Any) -> None:
        self.settings = settings
        await self.did_change_configuration(self, settings)

    @async_event
    async def will_create_files(sender, files: List[str]) -> Optional[Mapping[str, List[TextEdit]]]:
        ...

    @async_event
    async def did_create_files(sender, files: List[str]) -> None:
        ...

    @async_event
    async def will_rename_files(sender, files: List[Tuple[str, str]]) -> None:
        ...

    @async_event
    async def did_rename_files(sender, files: List[Tuple[str, str]]) -> None:
        ...

    @async_event
    async def will_delete_files(sender, files: List[str]) -> None:
        ...

    @async_event
    async def did_delete_files(sender, files: List[str]) -> None:
        ...

    @rpc_method(name="workspace/willCreateFiles", param_type=CreateFilesParams)
    @_logger.call
    async def _workspace_will_create_files(
        self, files: List[FileCreate], *args: Any, **kwargs: Any
    ) -> Optional[WorkspaceEdit]:
        results = await self.will_create_files(self, list(f.uri for f in files))
        if len(results) == 0:
            return None

        result: Dict[str, List[TextEdit]] = {}
        for e in results:
            if e is not None and isinstance(e, Mapping):
                result.update(e)

        # TODO: support full WorkspaceEdit

        return WorkspaceEdit(changes=result)

    @rpc_method(name="workspace/didCreateFiles", param_type=CreateFilesParams)
    @_logger.call
    async def _workspace_did_create_files(self, files: List[FileCreate], *args: Any, **kwargs: Any) -> None:
        await self.did_create_files(self, list(f.uri for f in files))

    @rpc_method(name="workspace/willRenameFiles", param_type=RenameFilesParams)
    @_logger.call
    async def _workspace_will_rename_files(self, files: List[FileRename], *args: Any, **kwargs: Any) -> None:
        await self.will_rename_files(self, list((f.old_uri, f.new_uri) for f in files))

        # TODO: return WorkspaceEdit

    @rpc_method(name="workspace/didRenameFiles", param_type=RenameFilesParams)
    @_logger.call
    async def _workspace_did_rename_files(self, files: List[FileRename], *args: Any, **kwargs: Any) -> None:
        await self.did_rename_files(self, list((f.old_uri, f.new_uri) for f in files))

    @rpc_method(name="workspace/willDeleteFiles", param_type=DeleteFilesParams)
    @_logger.call
    async def _workspace_will_delete_files(self, files: List[FileDelete], *args: Any, **kwargs: Any) -> None:
        await self.will_delete_files(self, list(f.uri for f in files))

        # TODO: return WorkspaceEdit

    @rpc_method(name="workspace/didDeleteFiles", param_type=DeleteFilesParams)
    @_logger.call
    async def _workspace_did_delete_files(self, files: List[FileDelete], *args: Any, **kwargs: Any) -> None:
        await self.did_delete_files(self, list(f.uri for f in files))

    async def get_configuration(
        self, section: Union[Type[_TConfig], str], scope_uri: Union[str, Uri, None] = None
    ) -> Union[_TConfig, Any]:

        if isinstance(section, (ConfigBase, HasConfigSection)):
            config = from_dict(
                await self.get_configuration(
                    section=cast(HasConfigSection, section).__config_section__, scope_uri=scope_uri
                ),
                section,
            )
            if config is None:
                return None

            return from_dict(config, section)

        if (
            self.parent.client_capabilities
            and self.parent.client_capabilities.workspace
            and self.parent.client_capabilities.workspace.configuration
        ):
            return (
                await self.parent.send_request(
                    "workspace/configuration",
                    ConfigurationParams(
                        items=[
                            ConfigurationItem(
                                scope_uri=str(scope_uri) if isinstance(scope_uri, Uri) else scope_uri,
                                section=str(section),
                            )
                        ]
                    ),
                    list,
                )
            )[0]

        result = self.settings
        for sub_key in str(section).split("."):
            if sub_key in result:
                result = result.get(sub_key, None)
            else:
                result = {}
                break
        return result

    def get_workspace_folder(self, uri: Union[Uri, str]) -> Optional[WorkspaceFolder]:
        if isinstance(uri, str):
            uri = Uri(uri)

        result = sorted(
            [f for f in self.workspace_folders if path_is_relative_to(uri.to_path(), f.uri.to_path())],
            key=lambda v1: len(v1.uri),
            reverse=True,
        )

        if len(result) > 0:
            return result[0]

        return None

    @rpc_method(name="workspace/didChangeWorkspaceFolders", param_type=DidChangeWorkspaceFoldersParams)
    @_logger.call
    async def _workspace_did_change_workspace_folders(
        self, event: WorkspaceFoldersChangeEvent, *args: Any, **kwargs: Any
    ) -> None:

        async with self.workspace_folders_lock:
            to_remove: List[WorkspaceFolder] = []
            for removed in event.removed:
                to_remove += [w for w in self.workspace_folders if w.uri == removed.uri]

            for removed in event.added:
                to_remove += [w for w in self.workspace_folders if w.uri == removed.uri]

            for r in to_remove:
                self.workspace_folders.remove(r)

            for a in event.added:
                self.workspace_folders.append(WorkspaceFolder(a.name, Uri(a.uri), a.uri))

    @async_event
    async def did_change_watched_files(sender, changes: List[FileEvent]) -> None:
        ...

    @rpc_method(name="workspace/didChangeWatchedFiles", param_type=DidChangeWatchedFilesParams)
    @_logger.call
    async def _workspace_did_change_watched_files(self, changes: List[FileEvent], *args: Any, **kwargs: Any) -> None:
        await self.did_change_watched_files(self, changes)

    async def add_file_watcher(
        self,
        callback: Callable[[Any, List[FileEvent]], Coroutine[Any, Any, None]],
        glob_pattern: str,
        kind: Optional[WatchKind] = None,
    ) -> FileWatcherEntry:
        return await self.add_file_watchers(callback, [(glob_pattern, kind)])

    async def add_file_watchers(
        self,
        callback: Callable[[Any, List[FileEvent]], Coroutine[Any, Any, None]],
        watchers: List[Union[FileWatcher, str, Tuple[str, Optional[WatchKind]]]],
    ) -> FileWatcherEntry:

        _watchers = [
            e if isinstance(e, FileWatcher) else FileWatcher(*e) if isinstance(e, tuple) else FileWatcher(e)
            for e in watchers
        ]

        entry = FileWatcherEntry(id=str(uuid.uuid4()), callback=callback, watchers=_watchers)

        current_entry = next((e for e in self._file_watchers if e.watchers == _watchers), None)

        if current_entry is not None:
            if callback not in self.did_change_watched_files:
                current_entry.child_callbacks.add(callback)  # type: ignore

            entry.parent = current_entry

            if len(current_entry.child_callbacks) > 0:
                self.did_change_watched_files.add(current_entry.call_childrens)
        else:
            self.did_change_watched_files.add(callback)  # type: ignore

            if (
                self.parent.client_capabilities
                and self.parent.client_capabilities.workspace
                and self.parent.client_capabilities.workspace.did_change_watched_files
                and self.parent.client_capabilities.workspace.did_change_watched_files.dynamic_registration
            ):
                await self.parent.register_capability(
                    entry.id,
                    "workspace/didChangeWatchedFiles",
                    DidChangeWatchedFilesRegistrationOptions(
                        watchers=[FileSystemWatcher(glob_pattern=w.glob_pattern, kind=w.kind) for w in _watchers]
                    ),
                )
            # TODO: implement own filewatcher if not supported by language server client

        def remove() -> None:
            if self._loop.is_running():
                asyncio.run_coroutine_threadsafe(self.remove_file_watcher_entry(entry), self._loop).result()

        weakref.finalize(entry, remove)

        self._file_watchers.add(entry)

        return entry

    async def remove_file_watcher_entry(self, entry: FileWatcherEntry) -> None:
        self._file_watchers.remove(entry)

        if entry.parent is not None:
            entry.parent.child_callbacks.remove(entry.callback)  # type: ignore
            if len(entry.child_callbacks) == 0:
                self.did_change_watched_files.remove(entry.call_childrens)
        elif len(entry.child_callbacks) == 0:
            self.did_change_watched_files.remove(entry.callback)  # type: ignore
            if (
                self.parent.client_capabilities
                and self.parent.client_capabilities.workspace
                and self.parent.client_capabilities.workspace.did_change_watched_files
                and self.parent.client_capabilities.workspace.did_change_watched_files.dynamic_registration
            ):
                await self.parent.unregister_capability(entry.id, "workspace/didChangeWatchedFiles")
            # TODO: implement own filewatcher if not supported by language server client
