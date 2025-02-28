from dataclasses import dataclass
from typing import Any, Dict, Iterator, List, Optional, Union, cast

from .dap_types import Event, Model
from .debugger import Debugger


@dataclass
class RobotExecutionEventBody(Model):
    type: str
    attributes: Optional[Dict[str, Any]] = None
    failed_keywords: Optional[List[Dict[str, Any]]] = None


class ListenerV2:
    ROBOT_LISTENER_API_VERSION = "2"

    def __init__(self, no_debug: bool = False) -> None:
        self.no_debug = no_debug
        self.debug = not no_debug
        self.failed_keywords: Optional[List[Dict[str, Any]]] = None
        self.last_fail_message: Optional[str] = None

    def start_suite(self, name: str, attributes: Dict[str, Any]) -> None:
        Debugger.instance().send_event(
            self, Event(event="robotStarted", body=RobotExecutionEventBody(type="suite", attributes=dict(attributes)))
        )

        Debugger.instance().start_output_group(name, attributes, "SUITE")

        Debugger.instance().start_suite(name, attributes)

    def end_suite(self, name: str, attributes: Dict[str, Any]) -> None:

        Debugger.instance().end_suite(name, attributes)

        Debugger.instance().end_output_group(name, attributes)

        Debugger.instance().send_event(
            self,
            Event(
                event="robotEnded",
                body=RobotExecutionEventBody(
                    type="suite", attributes=dict(attributes), failed_keywords=self.failed_keywords
                ),
            ),
        )

    def start_test(self, name: str, attributes: Dict[str, Any]) -> None:
        self.failed_keywords = None

        Debugger.instance().send_event(
            self, Event(event="robotStarted", body=RobotExecutionEventBody(type="test", attributes=dict(attributes)))
        )

        Debugger.instance().start_output_group(name, attributes, "TEST")

        Debugger.instance().start_test(name, attributes)

    def end_test(self, name: str, attributes: Dict[str, Any]) -> None:
        Debugger.instance().end_test(name, attributes)

        Debugger.instance().end_output_group(name, attributes)

        Debugger.instance().send_event(
            self,
            Event(
                event="robotEnded",
                body=RobotExecutionEventBody(
                    type="test", attributes=dict(attributes), failed_keywords=self.failed_keywords
                ),
            ),
        )

        self.failed_keywords = None

    def start_keyword(self, name: str, attributes: Dict[str, Any]) -> None:
        Debugger.instance().send_event(
            self, Event(event="robotStarted", body=RobotExecutionEventBody(type="keyword", attributes=dict(attributes)))
        )

        Debugger.instance().start_output_group(
            f"{name}({', '.join(repr(v) for v in attributes.get('args', []))})",
            attributes,
            attributes.get("type", None),
        )

        Debugger.instance().start_keyword(name, attributes)

    def end_keyword(self, name: str, attributes: Dict[str, Any]) -> None:
        Debugger.instance().end_keyword(name, attributes)

        Debugger.instance().end_output_group(name, attributes)

        if attributes["status"] == "FAIL" and attributes.get("source", None):
            if self.failed_keywords is None:
                self.failed_keywords = []

            self.failed_keywords.insert(0, {"message": self.last_fail_message, **attributes})

        Debugger.instance().send_event(
            self, Event(event="robotEnded", body=RobotExecutionEventBody(type="keyword", attributes=dict(attributes)))
        )

    def log_message(self, message: Dict[str, Any]) -> None:
        current_frame = Debugger.instance().stack_frames[0] if Debugger.instance().stack_frames else None

        if message["level"] == "FAIL":
            self.last_fail_message = message["message"]
            return

        source = current_frame.source if current_frame else None
        line = current_frame.line if current_frame else None
        column = current_frame.column if current_frame else None

        name = next((e.name for e in Debugger.instance().stack_frames if e.type in ["SUITE", "TEST"]), None)

        Debugger.instance().send_event(
            self,
            Event(
                event="robotLog",
                body={"itemId": name, "source": source, "lineno": line, "column": column, **dict(message)},
            ),
        )

        Debugger.instance().log_message(message)

    def message(self, message: Dict[str, Any]) -> None:
        Debugger.instance().message(message)

    def library_import(self, name: str, attributes: Dict[str, Any]) -> None:
        pass

    def resource_import(self, name: str, attributes: Dict[str, Any]) -> None:
        pass

    def variables_import(self, name: str, attributes: Dict[str, Any]) -> None:
        pass

    def output_file(self, path: str) -> None:
        Debugger.instance().robot_output_file = path

    def log_file(self, path: str) -> None:
        Debugger.instance().robot_log_file = path

    def report_file(self, path: str) -> None:
        Debugger.instance().robot_report_file = path

    def xunit_file(self, path: str) -> None:
        pass

    def debug_file(self, path: str) -> None:
        pass

    def close(self) -> None:
        pass


class ListenerV3:
    ROBOT_LISTENER_API_VERSION = "3"

    def start_suite(self, data: Any, result: Any) -> None:
        from robot.running import TestCase, TestSuite

        def enqueue(item: Union[TestSuite, TestCase]) -> Iterator[str]:
            if isinstance(item, TestSuite):
                for s in item.suites:
                    yield from enqueue(s)
                for s in item.tests:
                    yield from enqueue(s)

            yield item.longname

        items = [i for i in enqueue(cast(TestSuite, data))]

        Debugger.instance().send_event(
            self,
            Event(
                event="robotEnqueued",
                body={"items": items},
            ),
        )

    def end_suite(self, data: Any, result: Any) -> None:
        pass

    def start_test(self, data: Any, result: Any) -> None:
        pass

    def end_test(self, data: Any, result: Any) -> None:
        pass
