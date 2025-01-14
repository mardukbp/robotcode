from __future__ import annotations

import io
import os
from typing import TYPE_CHECKING, Any, List, Optional, cast

from ....utils.logging import LoggingDescriptor
from ...common.language import language_id
from ...common.lsp_types import (
    FormattingOptions,
    MessageType,
    Position,
    Range,
    TextEdit,
)
from ...common.text_document import TextDocument

if TYPE_CHECKING:
    from ..protocol import RobotLanguageServerProtocol

from ..configuration import RoboTidyConfig
from .model_helper import ModelHelperMixin
from .protocol_part import RobotLanguageServerProtocolPart


def robotidy_installed() -> bool:
    try:
        __import__("robotidy")
    except ImportError:
        return False
    return True


class RobotFormattingProtocolPart(RobotLanguageServerProtocolPart, ModelHelperMixin):
    _logger = LoggingDescriptor()

    def __init__(self, parent: RobotLanguageServerProtocol) -> None:
        super().__init__(parent)

        parent.formatting.format.add(self.format)
        # TODO implement range formatting
        # parent.formatting.format_range.add(self.format_range)

        self.space_count = 4
        self.use_pipes = False
        self.line_separator = os.linesep
        self.short_test_name_length = 18
        self.setting_and_variable_name_length = 14

    async def get_config(self, document: TextDocument) -> Optional[RoboTidyConfig]:
        folder = self.parent.workspace.get_workspace_folder(document.uri)
        if folder is None:
            return None

        return await self.parent.workspace.get_configuration(RoboTidyConfig, folder.uri)

    @language_id("robotframework")
    async def format(
        self, sender: Any, document: TextDocument, options: FormattingOptions, **further_options: Any
    ) -> Optional[List[TextEdit]]:
        config = await self.get_config(document)
        if config and config.enabled and robotidy_installed():
            return await self.format_robot_tidy(document, options, **further_options)
        return await self.format_internal(document, options, **further_options)

    async def format_robot_tidy(
        self, document: TextDocument, options: FormattingOptions, **further_options: Any
    ) -> Optional[List[TextEdit]]:

        from difflib import SequenceMatcher

        from robotidy.api import RobotidyAPI

        try:
            model = await self.parent.documents_cache.get_model(document)

            robot_tidy = RobotidyAPI(document.uri.to_path(), None)

            changed, _, new = robot_tidy.transform(model)

            if not changed:
                return None

            new_lines = new.text.splitlines()

            result: List[TextEdit] = []
            matcher = SequenceMatcher(a=document.lines, b=new_lines, autojunk=False)
            for code, old_start, old_end, new_start, new_end in matcher.get_opcodes():
                if code == "insert" or code == "replace":
                    result.append(
                        TextEdit(
                            range=Range(
                                start=Position(line=old_start, character=0),
                                end=Position(line=old_end, character=0),
                            ),
                            new_text=os.linesep.join(new_lines[new_start:new_end]) + os.linesep,
                        )
                    )

                elif code == "delete":
                    result.append(
                        TextEdit(
                            range=Range(
                                start=Position(line=old_start, character=0),
                                end=Position(line=old_end, character=0),
                            ),
                            new_text="",
                        )
                    )

            if result:
                return result

        except BaseException as e:
            self.parent.window.show_message(str(e), MessageType.Error)
        return None

    async def format_internal(
        self, document: TextDocument, options: FormattingOptions, **further_options: Any
    ) -> Optional[List[TextEdit]]:

        from robot.parsing.model.blocks import File
        from robot.tidypkg import (
            Aligner,
            Cleaner,
            NewlineNormalizer,
            SeparatorNormalizer,
        )

        model = cast(File, await self.parent.documents_cache.get_model(document))

        Cleaner().visit(model)
        NewlineNormalizer(self.line_separator, self.short_test_name_length).visit(model)
        SeparatorNormalizer(self.use_pipes, self.space_count).visit(model)
        Aligner(self.short_test_name_length, self.setting_and_variable_name_length, self.use_pipes).visit(model)

        with io.StringIO() as s:
            model.save(s)

            return [
                TextEdit(
                    range=Range(
                        start=Position(line=0, character=0),
                        end=Position(line=len(document.lines), character=len(document.lines[-1])),
                    ),
                    new_text=s.getvalue(),
                )
            ]

    @language_id("robotframework")
    async def format_range(
        self, sender: Any, document: TextDocument, range: Range, options: FormattingOptions, **further_options: Any
    ) -> Optional[List[TextEdit]]:
        # TODO implement range formatting
        # config = await self.get_config(document)
        # if config and config.enabled and robotidy_installed():
        #     return await self.format_robot_tidy(document, options, range=range, **further_options)
        return None
