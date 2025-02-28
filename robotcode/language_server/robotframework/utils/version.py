import re
from typing import Optional, Tuple

__all__ = ["InvalidRobotVersionError", "get_robot_version"]


class InvalidRobotVersionError(Exception):
    def __init__(self) -> None:
        super().__init__("Invalid robot version string.")

    pass


def get_robot_version() -> Tuple[int, int, Optional[int], Optional[str], Optional[int], Optional[int]]:
    import robot

    def s_to_i(s: Optional[str]) -> Optional[int]:
        return int(s) if s is not None else None

    robot_version = robot.get_version()
    try:
        m = re.match(
            r"(?P<major>\d+)"
            r"(\.(?P<minor>\d+))"
            r"(\.(?P<patch>\d+))?"
            r"((?P<pre_id>a|b|rc)(?P<pre_number>\d+))?"
            r"(\.(dev(?P<dev>\d+)))?"
            r"(?P<rest>.+)?",
            # robot.get_version(),
            robot_version,
        )

        if m is not None and m.group("rest") is None:
            return (
                int(m.group("major")),
                int(m.group("minor")),
                s_to_i(m.group("patch")),
                m.group("pre_id"),
                s_to_i(m.group("pre_number")),
                s_to_i(m.group("dev")),
            )
    except BaseException as ex:
        raise InvalidRobotVersionError() from ex

    raise InvalidRobotVersionError()
