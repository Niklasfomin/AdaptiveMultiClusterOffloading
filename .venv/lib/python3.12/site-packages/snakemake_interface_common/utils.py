import collections
from datetime import datetime
import os
import subprocess as sp
from typing import Any, Callable, Optional, Tuple


def not_iterable(value: Any) -> bool:
    return (
        isinstance(value, str)
        or isinstance(value, dict)
        or not isinstance(value, collections.abc.Iterable)
    )


class lazy_property(property):
    __slots__ = ["method", "cached", "__doc__"]

    @staticmethod
    def clean(instance: Any, method: str) -> None:
        delattr(instance, method)

    def __init__(self, method: Callable) -> None:
        self.method = method
        self.cached = f"_{method.__name__}"
        super().__init__(method, doc=method.__doc__)

    def __get__(self, instance: Any, owner: Optional[Any] = None) -> Any:
        cached = (
            getattr(instance, self.cached) if hasattr(instance, self.cached) else None
        )
        if cached is not None:
            return cached
        value = self.method(instance)
        setattr(instance, self.cached, value)
        return value


def lutime(f: str, times: Optional[Tuple[float, float]]) -> bool:
    """Set utime for a file or symlink. Do not follow symlink.

    Parameters
    ----------
    f : str
        The file or symlink.
    times : Optional[Tuple[float, float]]
        The access and modification times (atime, mtime) in seconds.

    Returns
    -------
    bool
        True if successful.
        False if the system command fails.
    """
    # In some cases, we have a platform where os.supports_follow_symlink includes
    # stat() but not utime().  This leads to an anomaly.  In any case we never want
    # to touch the target of a link.
    if os.utime in os.supports_follow_symlinks:
        # ...utime is well behaved
        os.utime(f, times, follow_symlinks=False)
    elif not os.path.islink(f):
        # ...symlinks not an issue here
        os.utime(f, times)
    else:
        try:
            # try the system command
            if times:

                def fmt_time(sec: float) -> str:
                    return datetime.fromtimestamp(sec).strftime("%Y%m%d%H%M.%S")

                atime, mtime = times
                sp.check_call(["touch", "-h", f, "-a", "-t", fmt_time(atime)])
                sp.check_call(["touch", "-h", f, "-m", "-t", fmt_time(mtime)])
            else:
                sp.check_call(["touch", "-h", f])
        except sp.CalledProcessError:
            # problem system. Do nothing.
            return False
    return True


if os.chmod in os.supports_follow_symlinks:

    def lchmod(f: str, mode: int) -> None:
        os.chmod(f, mode, follow_symlinks=False)

else:

    def lchmod(f: str, mode: int) -> None:
        os.chmod(f, mode)
