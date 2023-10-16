# ruff: noqa: ARG002

from __future__ import annotations

import logging
import re
import socket
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import (
    TYPE_CHECKING,
    Any,
    AnyStr,
    ClassVar,
    Generic,
    Literal,
    Optional,
    TypeVar,
    Union,
    cast,
)

from mopidy.config import validators
from mopidy.internal import log, path

if TYPE_CHECKING:
    from collections.abc import Iterable

T = TypeVar("T")
K = TypeVar("K", bound="ConfigValue")
V = TypeVar("V", bound="ConfigValue")


def decode(value: AnyStr) -> str:
    result = (
        value.decode(errors="surrogateescape") if isinstance(value, bytes) else value
    )

    for char in ("\\", "\n", "\t"):
        result = result.replace(char.encode(encoding="unicode-escape").decode(), char)

    return result


def encode(value: AnyStr) -> str:
    result = (
        value.decode(errors="surrogateescape") if isinstance(value, bytes) else value
    )

    for char in ("\\", "\n", "\t"):
        result = result.replace(char, char.encode(encoding="unicode-escape").decode())

    return result


class DeprecatedValue:
    pass


class _TransformedValue(str):
    def __new__(cls, _original, transformed):
        return super().__new__(cls, transformed)

    def __init__(self, original, _transformed):
        self.original = original


class ConfigValue(ABC, Generic[T]):
    """Represents a config key's value and how to handle it.

    Normally you will only be interacting with sub-classes for config values
    that encode either deserialization behavior and/or validation.

    Each config value should be used for the following actions:

    1. Deserializing from a raw string and validating, raising ValueError on
       failure.
    2. Serializing a value back to a string that can be stored in a config.
    3. Formatting a value to a printable form (useful for masking secrets).

    :class:`None` values should not be deserialized, serialized or formatted,
    the code interacting with the config should simply skip None config values.
    """

    @abstractmethod
    def deserialize(self, value: AnyStr) -> Optional[T]:
        """Cast raw string to appropriate type."""
        raise NotImplementedError

    def serialize(self, value: T, display: bool = False) -> str:
        """Convert value back to string for saving."""
        if value is None:
            return ""
        return str(value)


class Deprecated(ConfigValue[Any]):
    """Deprecated value.

    Used for ignoring old config values that are no longer in use, but should
    not cause the config parser to crash.
    """

    def deserialize(self, value: AnyStr):
        return DeprecatedValue()

    def serialize(self, value: Any, display: bool = False):
        return DeprecatedValue()


class String(ConfigValue[str]):
    r"""String value.

    Is decoded as utf-8, and \n and \t escapes should work and be preserved.
    """

    def __init__(
        self,
        optional: bool = False,
        choices: Optional[Iterable[str]] = None,
        transformer: Optional[Callable[[str], str]] = None,
    ) -> None:
        self._required = not optional
        self._choices = choices
        self._transformer = transformer

    def deserialize(self, value: AnyStr) -> Optional[str]:
        result = decode(value).strip()
        validators.validate_required(result, self._required)
        if not result:
            return None

        # This is necessary for backwards-compatibility, in case subclasses
        # aren't calling their parent constructor.
        transformer = getattr(self, "_transformer", None)
        if transformer:
            transformed_value = transformer(result)
            result = _TransformedValue(result, transformed_value)

        validators.validate_choice(result, self._choices)
        return result

    def serialize(self, value: str, display: bool = False) -> str:
        if value is None:
            return ""
        if isinstance(value, _TransformedValue):
            value = value.original
        return encode(value)


class Secret(String):
    r"""Secret string value.

    Is decoded as utf-8, and \n and \t escapes should work and be preserved.

    Should be used for passwords, auth tokens etc. Will mask value when being
    displayed.
    """

    def __init__(
        self,
        optional: bool = False,
        choices: None = None,
        transformer: Optional[Callable[[str], str]] = None,
    ) -> None:
        super().__init__(
            optional=optional,
            choices=None,  # Choices doesn't make sense for secrets
            transformer=transformer,
        )

    def serialize(self, value: str, display: bool = False) -> str:
        if value is not None and display:
            return "********"
        return super().serialize(value, display)


class Integer(ConfigValue[int]):
    """Integer value."""

    def __init__(
        self,
        minimum: Optional[int] = None,
        maximum: Optional[int] = None,
        choices: Optional[Iterable[int]] = None,
        optional: bool = False,
    ) -> None:
        self._required = not optional
        self._minimum = minimum
        self._maximum = maximum
        self._choices = choices

    def deserialize(self, value: AnyStr) -> Optional[int]:
        result = decode(value)
        validators.validate_required(result, self._required)
        if not result:
            return None
        result = int(result)
        validators.validate_choice(result, self._choices)
        validators.validate_minimum(result, self._minimum)
        validators.validate_maximum(result, self._maximum)
        return result


class Float(ConfigValue[float]):
    """Float value."""

    def __init__(
        self,
        minimum: Optional[float] = None,
        maximum: Optional[float] = None,
        optional: bool = False,
    ) -> None:
        self._required = not optional
        self._minimum = minimum
        self._maximum = maximum

    def deserialize(self, value: AnyStr) -> Optional[float]:
        result = decode(value)
        validators.validate_required(result, self._required)
        if not value:
            return None
        result = float(result)
        validators.validate_minimum(result, self._minimum)
        validators.validate_maximum(result, self._maximum)
        return result


class Boolean(ConfigValue[bool]):
    """Boolean value.

    Accepts ``1``, ``yes``, ``true``, and ``on`` with any casing as
    :class:`True`.

    Accepts ``0``, ``no``, ``false``, and ``off`` with any casing as
    :class:`False`.
    """

    true_values = ("1", "yes", "true", "on")
    false_values = ("0", "no", "false", "off")

    def __init__(self, optional: bool = False) -> None:
        self._required = not optional

    def deserialize(self, value: AnyStr) -> Optional[bool]:
        result = decode(value)
        validators.validate_required(result, self._required)
        if not result:
            return None
        if result.lower() in self.true_values:
            return True
        if result.lower() in self.false_values:
            return False
        raise ValueError(f"invalid value for boolean: {result!r}")

    def serialize(
        self,
        value: bool,
        display: bool = False,
    ) -> Literal["true", "false"]:
        if value is True:
            return "true"
        if value in (False, None):
            return "false"
        raise ValueError(f"{value!r} is not a boolean")


class Pair(ConfigValue[tuple[K, V]]):
    """Pair value.

    The value is expected to be a pair of elements, separated by a specified delimiter.
    Values can optionally not be a pair, in which case the whole input is provided for
    both sides of the value.
    """

    _subtypes: tuple[K, V]

    def __init__(
        self,
        optional: bool = False,
        optional_pair: bool = False,
        separator: str = "|",
        subtypes: tuple[K, V] = (String(), String()),
    ) -> None:
        self._required = not optional
        self._optional_pair = optional_pair
        self._separator = separator
        self._subtypes = subtypes

    def deserialize(self, value: str) -> Optional[tuple[K, V]]:
        raw_value = decode(value).strip()
        validators.validate_required(raw_value, self._required)
        if not raw_value:
            return None

        if self._separator in raw_value:
            values = raw_value.split(self._separator, 1)
        elif self._optional_pair:
            values = (raw_value, raw_value)
        else:
            raise ValueError(
                f"Config value must include {self._separator!r} separator: {raw_value}"
            )

        return cast(
            tuple[K, V],
            (
                self._subtypes[0].deserialize(encode(values[0])),
                self._subtypes[1].deserialize(encode(values[1])),
            ),
        )

    def serialize(self, value: tuple[K, V], display: bool = False) -> str:
        serialized_first_value = self._subtypes[0].serialize(value[0], display=display)
        serialized_second_value = self._subtypes[1].serialize(value[1], display=display)

        if (
            not display
            and self._optional_pair
            and serialized_first_value == serialized_second_value
        ):
            return serialized_first_value

        return f"{serialized_first_value}{self._separator}{serialized_second_value}"


class List(ConfigValue[V]):
    """List value.

    Supports elements split by commas or newlines. Newlines take precedence and
    empty list items will be filtered out.

    Enforcing unique entries in the list will result in a set data structure
    being used. This does not preserve ordering, which could result in the
    serialized output being unstable.
    """

    def __init__(
        self,
        optional: bool = False,
        unique: bool = False,
        subtype: V = String(),  # noqa: B008
    ) -> None:
        self._required = not optional
        self._unique = unique
        self._subtype = subtype

    def deserialize(self, value: str) -> Union[tuple[V, ...], frozenset[V]]:
        value = decode(value)

        strings: list[str]
        if "\n" in value:
            strings = re.split(r"\s*\n\s*", value)
        else:
            strings = re.split(r"\s*,\s*", value)

        # This is necessary for backwards-compatibility, in case subclasses
        # aren't calling their parent constructor.
        subtype: ConfigValue = getattr(self, "_subtype", String())

        values_iter = (subtype.deserialize(s.strip()) for s in strings if s.strip())
        values = frozenset(values_iter) if self._unique else tuple(values_iter)

        validators.validate_required(values, self._required)
        return cast(Union[tuple[V, ...], frozenset[V]], values)

    def serialize(
        self, value: Union[tuple[V, ...], frozenset[V]], display: bool = False
    ) -> str:
        if not value:
            return ""

        # This is necessary for backwards-compatibility, in case subclasses
        # aren't calling their parent constructor.
        subtype: V = getattr(
            self, "_subtype", String()
        )  # pyright: ignore[reportGeneralTypeIssues]

        serialized_values = []
        for item in value:
            serialized_value = subtype.serialize(item, display=display)
            if serialized_value:
                serialized_values.append(serialized_value)

        return "\n  " + "\n  ".join(serialized_values)


class LogColor(ConfigValue[log.LogColorName]):
    def deserialize(self, value: str) -> str:
        value = decode(value)
        validators.validate_choice(value.lower(), log.COLORS)
        return value.lower()

    def serialize(self, value: log.LogColorName, display: bool = False) -> str:
        if value.lower() in log.COLORS:
            return encode(value.lower())
        return ""


class LogLevel(ConfigValue[log.LogLevelName]):
    """Log level value.

    Expects one of ``critical``, ``error``, ``warning``, ``info``, ``debug``,
    ``trace``, or ``all``, with any casing.
    """

    levels: ClassVar[dict[str, int]] = {
        "critical": logging.CRITICAL,
        "error": logging.ERROR,
        "warning": logging.WARNING,
        "info": logging.INFO,
        "debug": logging.DEBUG,
        "trace": log.TRACE_LOG_LEVEL,
        "all": logging.NOTSET,
    }

    def deserialize(self, value: str) -> Optional[int]:
        value = decode(value)
        validators.validate_choice(value.lower(), self.levels.keys())
        return self.levels.get(value.lower())

    def serialize(self, value: log.LogLevelName, display: bool = False) -> str:
        lookup = {v: k for k, v in self.levels.items()}
        if value in lookup:
            return encode(lookup[value])
        return ""


class Hostname(ConfigValue):
    """Network hostname value."""

    def __init__(self, optional: bool = False) -> None:
        self._required = not optional

    def deserialize(self, value: str, display: bool = False) -> Optional[str]:
        value = decode(value).strip()
        validators.validate_required(value, self._required)
        if not value:
            return None

        socket_path = path.get_unix_socket_path(value)
        if socket_path is not None:
            path_str = Path(not self._required).deserialize(socket_path)
            return f"unix:{path_str}"

        try:
            socket.getaddrinfo(value, None)
        except OSError as exc:
            raise ValueError("must be a resolveable hostname or valid IP") from exc

        return value


class Port(Integer):
    """Network port value.

    Expects integer in the range 0-65535, zero tells the kernel to simply
    allocate a port for us.
    """

    def __init__(self, choices=None, optional=False):
        super().__init__(
            minimum=0, maximum=2**16 - 1, choices=choices, optional=optional
        )


# Keep this for backwards compatibility
class _ExpandedPath(_TransformedValue):
    pass


class Path(ConfigValue[_ExpandedPath]):
    """File system path.

    The following expansions of the path will be done:

    - ``~`` to the current user's home directory
    - ``$XDG_CACHE_DIR`` according to the XDG spec
    - ``$XDG_CONFIG_DIR`` according to the XDG spec
    - ``$XDG_DATA_DIR`` according to the XDG spec
    - ``$XDG_MUSIC_DIR`` according to the XDG spec
    """

    def __init__(self, optional=False):
        self._required = not optional

    def deserialize(self, value: str) -> Optional[_ExpandedPath]:
        value = decode(value).strip()
        expanded = path.expand_path(value)
        validators.validate_required(value, self._required)
        validators.validate_required(expanded, self._required)
        if not value or expanded is None:
            return None
        return _ExpandedPath(value, expanded)

    def serialize(
        self,
        value: Union[None, _ExpandedPath, bytes],
        display: bool = False,
    ) -> str:
        if value is None:
            return ""
        result = value
        if isinstance(result, _ExpandedPath):
            result = result.original
        if isinstance(result, bytes):
            result = result.decode(errors="surrogateescape")
        return str(result)
