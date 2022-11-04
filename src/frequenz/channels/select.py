# License: MIT
# Copyright © 2022 Frequenz Energy-as-a-Service GmbH

"""Select the first among multiple Receivers.

Expects Receiver class to raise `StopAsyncIteration`
exception once no more messages are expected or the channel
is closed in case of `Receiver` class.
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set, TypeVar

from frequenz.channels.base_classes import Receiver

logger = logging.Logger(__name__)
T = TypeVar("T")


@dataclass
class _Selected:
    """A wrapper class for holding values in `Select`.

    Using this wrapper class allows `Select` to inform user code when a
    receiver gets closed.
    """

    inner: Optional[Any]


@dataclass
class _ReadyReceiver:
    """A class for tracking receivers that have a message ready to be read.

    Used to make sure that receivers are not consumed from until messages are accessed
    by user code, at which point, it will be converted into a `_Selected` object.

    When a channel has closed,  `recv` should be `None`.
    """

    recv: Optional[Receiver[Any]]

    def get(self) -> _Selected:
        """Consume a message from the receiver and return a `_Selected` object.

        Returns:
            An instance of `_Selected` holding a value from the receiver.
        """
        if self.recv is None:
            return _Selected(None)
        return _Selected(self.recv._get())  # pylint: disable=protected-access


class Select:
    """Select the next available message from a group of Receivers.

    If `Select` was created with more `Receiver` than what are read in
    the if-chain after each call to [ready()][frequenz.channels.Select.ready],
    messages coming in the additional receivers are dropped, and
    a warning message is logged.

    [Receiver][frequenz.channels.Receiver]s also function as `Receiver`.

    Example:
        For example, if there are two receivers that you want to
        simultaneously wait on, this can be done with:

        ```python
        select = Select(name1 = receiver1, name2 = receiver2)
        while await select.ready():
            if msg := select.name1:
                if val := msg.inner:
                    # do something with `val`
                    pass
                else:
                    # handle closure of receiver.
                    pass
            elif msg := select.name2:
                # do something with `msg.inner`
                pass
        ```
    """

    def __init__(self, **kwargs: Receiver[Any]) -> None:
        """Create a `Select` instance.

        Args:
            **kwargs: sequence of receivers
        """
        self._receivers = kwargs
        self._pending: Set[asyncio.Task[None]] = set()

        for name, recv in self._receivers.items():
            # can replace __anext__() to anext() (Only Python 3.10>=)
            ready = recv._ready()  # pylint: disable=unnecessary-dunder-call
            self._pending.add(asyncio.create_task(ready, name=name))

        self._ready_count = 0
        self._prev_ready_count = 0
        self._result: Dict[str, Optional[_ReadyReceiver]] = {
            name: None for name in self._receivers
        }

    def __del__(self) -> None:
        """Cleanup any pending tasks."""
        for task in self._pending:
            task.cancel()

    async def ready(self) -> bool:
        """Wait until there is a message in any of the receivers.

        Returns `True` if there is a message available, and `False` if all
        receivers have closed.

        Returns:
            Whether there are further messages or not.
        """
        if self._ready_count > 0:
            if self._ready_count == self._prev_ready_count:
                dropped_names: List[str] = []
                for name, value in self._result.items():
                    if value is not None:
                        dropped_names.append(name)
                        if value.recv is not None:
                            value.recv._get()  # pylint: disable=protected-access
                        self._result[name] = None
                self._ready_count = 0
                self._prev_ready_count = 0
                logger.warning(
                    "Select.ready() dropped data from receiver(s): %s, "
                    "because no messages have been fetched since the last call to ready().",
                    dropped_names,
                )
            else:
                self._prev_ready_count = self._ready_count
                return True
        if len(self._pending) == 0:
            return False

        # once all the pending messages have been consumed, reset the
        # `_prev_ready_count` as well, and wait for new messages.
        self._prev_ready_count = 0

        done, self._pending = await asyncio.wait(
            self._pending, return_when=asyncio.FIRST_COMPLETED
        )
        for item in done:
            name = item.get_name()
            recv = self._receivers[name]
            if isinstance(item.exception(), StopAsyncIteration):
                result = None
            else:
                result = recv
            self._ready_count += 1
            self._result[name] = _ReadyReceiver(result)
            # if channel or Receiver is closed
            # don't add a task for it again.
            if result is None:
                continue
            ready = recv._ready()  # pylint: disable=protected-access
            self._pending.add(asyncio.create_task(ready, name=name))
        return True

    def __getattr__(self, name: str) -> Optional[Any]:
        """Return the latest unread message from a `Receiver`, if available.

        Args:
            name: Name of the channel.

        Returns:
            Latest unread message for the specified `Receiver`, or `None`.

        Raises:
            KeyError: when the name was not specified when creating the
                `Select` instance.
        """
        result = self._result[name]
        if result is None:
            return result
        self._result[name] = None
        self._ready_count -= 1
        return result.get()
