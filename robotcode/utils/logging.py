from __future__ import annotations

import collections
import functools
import inspect
import logging
import time
from enum import Enum
from types import FunctionType, MethodType
from typing import Any, Callable, List, Optional, Type, TypeVar, Union, cast, overload

__all__ = ["LoggingDescriptor"]


def get_class_that_defined_method(meth: Callable[..., Any]) -> Optional[Type[Any]]:
    if inspect.ismethod(meth):
        for c in inspect.getmro(cast(MethodType, meth).__self__.__class__):
            if c.__dict__.get(meth.__name__) is meth:
                return c
        meth = cast(MethodType, meth).__func__  # fallback to __qualname__ parsing
    if inspect.isfunction(meth):
        class_name = meth.__qualname__.split(".<locals>", 1)[0].rsplit(".", 1)[0]
        try:
            cls = getattr(inspect.getmodule(meth), class_name)
        except AttributeError:
            cls = cast(FunctionType, meth).__globals__.get(class_name)
        if isinstance(cls, type):
            return cls
    return None


def _get_callable_has_self_or_cls_parameter(func: Any) -> bool:
    if inspect.ismethod(func) or isinstance(func, (classmethod, staticmethod)):
        return True
    if inspect.isfunction(func) and len(func.__qualname__.split(".<locals>.", 1)[-1].rsplit(".", 1)) > 1:
        return True
    return False


def get_unwrapped_func(func: Callable[..., Any]) -> Callable[..., Any]:
    result = inspect.unwrap(func)
    if isinstance(result, (staticmethod, classmethod)):
        return get_unwrapped_func(result.__func__)
    return cast(Callable[..., Any], result)


_LoggerEntry = collections.namedtuple("_LoggerEntry", "level prefix condition states")


class _HasLoggerEntries:
    __logging_entries__: List[_LoggerEntry]


_FUNC_TYPE = Union[Callable[[], logging.Logger], Callable[[], None], staticmethod, None]

_F = TypeVar("_F", bound=Callable[..., Any])


class CallState(Enum):
    ENTERING = "entering"
    EXITING = "exiting"
    EXCEPTION = "exception"


class LoggingDescriptor:
    __func: _FUNC_TYPE = None
    __name: Optional[str] = None
    __level: int = logging.NOTSET
    __owner: Any = None
    __owner_name: Optional[str] = None
    __postfix: str = ""
    __logger: Optional[logging.Logger] = None

    def __init__(
        self,
        _func: _FUNC_TYPE = None,
        *,
        name: Optional[str] = None,
        postfix: str = "",
        level: int = logging.NOTSET,
    ) -> None:
        self.__func = _func

        if _func is not None:
            functools.update_wrapper(self, _func)  # type: ignore

        self.__name = name
        self.__level = level
        self.__postfix = postfix

    def __init_logger(self) -> LoggingDescriptor:
        if self.__logger is None:
            returned_logger = None

            if self.__func is not None:

                if isinstance(self.__func, staticmethod):
                    returned_logger = cast(staticmethod, self.__func).__func__()
                else:
                    returned_logger = self.__func()

            self.__logger = (
                returned_logger
                if returned_logger is not None
                else logging.getLogger(
                    self.__name
                    if self.__name is not None
                    else (
                        ("" if self.__owner is None else self.__owner.__module__ + "." + self.__owner.__qualname__)
                        if self.__owner is not None
                        else get_unwrapped_func(self.__func).__module__
                        if self.__func is not None
                        else "<unknown>"
                    )
                    + self.__postfix
                )
            )

            if self.__logger is None:
                raise NotImplementedError("Can't get or create a Logger object")

            self.set_level(self.__level)

        return self

    @property
    def logger(self) -> logging.Logger:
        if self.__logger is None:
            self.__init_logger()

        if self.__logger is None:
            raise Exception("Logger not initialized")

        return self.__logger

    def __set_name__(self, owner: Any, name: str) -> None:
        self.__owner = owner
        self.__owner_name = name

    def __call__(self, _func: _FUNC_TYPE = None) -> LoggingDescriptor:
        if _func is not None:
            self.__func = _func

        return self

    def __get__(self, obj: Any, objtype: Type[Any]) -> LoggingDescriptor:
        return self

    def log(
        self, level: int, msg: Any, condition: Optional[Callable[[], bool]] = None, *args: Any, **kwargs: Any
    ) -> None:
        if self.is_enabled_for(level) and (condition is not None and condition() or condition is None):
            self.logger.log(level, msg() if callable(msg) else msg, *args, **kwargs)

    def debug(
        self,
        msg: Union[str, Callable[[], str]],
        condition: Optional[Callable[[], bool]] = None,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        self.log(logging.DEBUG, msg, condition, *args, **kwargs)

    def info(
        self,
        msg: Union[str, Callable[[], str]],
        condition: Optional[Callable[[], bool]] = None,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        self.log(logging.INFO, msg, condition, *args, **kwargs)

    def warning(
        self,
        msg: Union[str, Callable[[], str]],
        condition: Optional[Callable[[], bool]] = None,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        self.log(logging.WARNING, msg, condition, *args, **kwargs)

    def error(
        self,
        msg: Union[str, Callable[[], str]],
        condition: Optional[Callable[[], bool]] = None,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        self.log(logging.ERROR, msg, condition, *args, **kwargs)

    def exception(
        self,
        msg: Union[BaseException, str, Callable[[], Union[BaseException, str]]],
        condition: Optional[Callable[[], bool]] = None,
        exc_info: Any = True,
        *args: Any,
        level: Optional[int] = None,
        **kwargs: Any,
    ) -> None:
        if isinstance(msg, BaseException):
            sm = str(msg)
            s = type(msg).__qualname__
            if sm:
                s += ": " + sm
            self.log(logging.ERROR if level is None else level, s, condition, *args, exc_info=exc_info, **kwargs)
        else:
            self.log(logging.ERROR if level is None else level, msg, condition, *args, exc_info=exc_info, **kwargs)

    def critical(
        self,
        msg: Union[str, Callable[[], str]],
        condition: Optional[Callable[[], bool]] = None,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        self.log(logging.CRITICAL, msg, condition, *args, **kwargs)

    def is_enabled_for(self, level: int) -> bool:
        return self.logger.isEnabledFor(level)

    def set_level(self, level: int) -> None:
        self.logger.setLevel(level)

    def get_effective_level(self) -> int:
        return self.logger.getEffectiveLevel()

    def has_handlers(self) -> bool:
        return self.logger.hasHandlers()

    @property
    def name(self) -> str:
        return self.logger.name

    def __repr__(self) -> str:
        logger = self.logger
        level = logging.getLevelName(logger.getEffectiveLevel())
        return f"{self.__class__.__name__}(name={repr(logger.name)}, level={repr(level)})"

    _call_tracing_enabled = False
    _call_tracing_default_level = logging.DEBUG

    @classmethod
    def set_call_tracing(cls, value: bool) -> None:
        cls._call_tracing_enabled = value

    @classmethod
    def set_call_tracing_default_level(cls, level: int) -> None:
        cls._call_tracing_default_level = level

    @overload
    def call(self, _func: _F) -> _F:
        ...

    @overload
    def call(
        self,
        *,
        level: Optional[int] = None,
        prefix: str = "",
        condition: Optional[Callable[..., bool]] = None,
        entering: bool = True,
        exiting: bool = False,
        exception: bool = False,
        timed: bool = False,
    ) -> Callable[[_F], _F]:
        ...

    def call(
        self,
        _func: Optional[_F] = None,
        *,
        level: Optional[int] = None,
        prefix: str = "",
        condition: Optional[Callable[..., bool]] = None,
        entering: bool = True,
        exiting: bool = False,
        exception: bool = False,
        timed: bool = False,
        **kwargs: Any,
    ) -> Callable[[_F], _F]:

        if level is None:
            level = type(self)._call_tracing_default_level

        def _decorator(func: _F) -> Callable[[_F], _F]:
            unwrapped_func = inspect.unwrap(func)

            if not hasattr(unwrapped_func, "__logging_entries__"):
                unwrapped_func.__logging_entries__ = []

            unwrapped_func.__logging_entries__.append(
                _LoggerEntry(
                    level=level,
                    prefix=prefix,
                    condition=condition,
                    states={CallState.ENTERING: entering, CallState.EXITING: exiting, CallState.EXCEPTION: exception},
                )
            )

            skip_first_arg = _get_callable_has_self_or_cls_parameter(unwrapped_func)

            @functools.wraps(func)
            def _wrapper(*wrapper_args: Any, **wrapper_kwargs: Any) -> Any:

                if isinstance(unwrapped_func, staticmethod):
                    real_args = wrapper_args[1:]
                    real_func = unwrapped_func.__func__
                else:
                    real_args = wrapper_args
                    real_func = unwrapped_func

                def has_logging_entries() -> bool:
                    return hasattr(unwrapped_func, "__logging_entries__")

                def func_name() -> str:
                    if isinstance(unwrapped_func, staticmethod):
                        return (
                            unwrapped_func.__func__.__qualname__
                            if self.__owner or self.__name is None
                            else unwrapped_func.__func__.__name__
                        )
                    else:
                        return (
                            str(unwrapped_func.__qualname__)
                            if self.__owner is None or self.__name
                            else str(unwrapped_func.__name__)
                        )

                def log(
                    message: Callable[[], str],
                    *,
                    state: CallState,
                    log_level: Optional[int] = None,
                    **log_kwargs: Any,
                ) -> None:
                    if has_logging_entries():
                        for c in cast(_HasLoggerEntries, unwrapped_func).__logging_entries__:
                            if c.states[state]:

                                def state_msg() -> str:
                                    return (
                                        (str(state.value) + " ")
                                        if state != CallState.ENTERING or c.states[CallState.EXITING]
                                        else ""
                                    )

                                self.log(
                                    log_level if log_level is not None else c.level,
                                    lambda: f"{state_msg()}{prefix}{message()}",
                                    condition=lambda: c.condition is None or c.condition(*real_args, **wrapper_kwargs),
                                    **{**kwargs, **log_kwargs},
                                )

                def build_enter_message() -> str:
                    message_args = wrapper_args[1:] if skip_first_arg else wrapper_args

                    return "{0}({1}{2}{3})".format(
                        func_name(),
                        ", ".join(repr(a) for a in message_args),
                        (", " if len(message_args) > 0 and len(wrapper_kwargs) > 0 else ""),
                        (
                            ", ".join(f"{str(k)}={repr(v)}" for k, v in wrapper_kwargs.items())
                            if len(wrapper_kwargs) > 0
                            else ""
                        ),
                    )

                def build_exit_message(res: Any, duration: Optional[float]) -> str:
                    return "{0}(...) -> {1}{2}".format(
                        func_name(), repr(res), f" duration: {duration}" if duration is not None else ""
                    )

                def build_exception_message(exception: BaseException) -> str:
                    return "{0}(...)->{1}".format(func_name(), exception)

                log(build_enter_message, state=CallState.ENTERING)

                result = None
                try:
                    start_time: float = 0
                    end_time: float = 0
                    if timed:
                        start_time = time.time()

                    result = real_func(*real_args, **wrapper_kwargs)

                    if timed:
                        end_time = time.time()

                except BaseException as e:
                    ex = e
                    log(
                        lambda: build_exception_message(ex),
                        log_level=logging.ERROR,
                        state=CallState.EXCEPTION,
                        exc_info=True,
                    )
                    raise
                else:
                    log(
                        lambda: build_exit_message(result, end_time - start_time if timed else None),
                        state=CallState.EXITING,
                    )
                return result

            return _wrapper

        def _empty__decorator(func: _F) -> Callable[[_F], _F]:
            return func

        if _func is None:
            return cast(Callable[[_F], _F], _decorator if type(self)._call_tracing_enabled else _empty__decorator)

        return _decorator(_func) if type(self)._call_tracing_enabled else _empty__decorator(_func)
