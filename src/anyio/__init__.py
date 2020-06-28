import os
import socket
import sys
import threading
import typing
from contextlib import contextmanager
from importlib import import_module
from subprocess import CompletedProcess, PIPE, DEVNULL, CalledProcessError
from types import FrameType, CoroutineType, GeneratorType
from typing import (
    overload, TypeVar, Callable, Union, Optional, Awaitable, Coroutine, Any, Dict, List, Type,
    Sequence)

import attr
import sniffio

from ._utils import get_bind_address
from .abc.streams import UnreliableReceiveMessageStream, MessageStream
from .abc.subprocesses import AsyncProcess
from .abc.synchronization import Lock, Condition, Event, Semaphore, CapacityLimiter
from .abc.tasks import CancelScope, TaskGroup, BlockingPortal
from .abc.networking import (
    TCPSocketStream, UNIXSocketStream, UDPSocket, IPAddressType, TCPListener, UNIXListener)
from .abc.testing import TestRunner
from .fileio import AsyncFile

BACKENDS = 'asyncio', 'curio', 'trio'
IPPROTO_IPV6 = getattr(socket, 'IPPROTO_IPV6', 41)  # https://bugs.python.org/issue29515

T_Retval = TypeVar('T_Retval')
T_Agen = TypeVar('T_Agen')
T_Item = TypeVar('T_Item')
_local = threading.local()


#
# Event loop
#

def run(func: Callable[..., Coroutine[Any, Any, T_Retval]], *args,
        backend: str = BACKENDS[0], backend_options: Optional[Dict[str, Any]] = None) -> T_Retval:
    """
    Run the given coroutine function in an asynchronous event loop.

    The current thread must not be already running an event loop.

    :param func: a coroutine function
    :param args: positional arguments to ``func``
    :param backend: name of the asynchronous event loop implementation – one of ``asyncio``,
        ``curio`` and ``trio``
    :param backend_options: keyword arguments to call the backend ``run()`` implementation with
    :return: the return value of the coroutine function
    :raises RuntimeError: if an asynchronous event loop is already running in this thread
    :raises LookupError: if the named backend is not found

    """
    try:
        asynclib_name = sniffio.current_async_library()
    except sniffio.AsyncLibraryNotFoundError:
        pass
    else:
        raise RuntimeError('Already running {} in this thread'.format(asynclib_name))

    try:
        asynclib = import_module('{}._backends._{}'.format(__name__, backend))
    except ImportError as exc:
        raise LookupError('No such backend: {}'.format(backend)) from exc

    token = None
    if sniffio.current_async_library_cvar.get(None) is None:
        # Since we're in control of the event loop, we can cache the name of the async library
        token = sniffio.current_async_library_cvar.set(backend)

    try:
        backend_options = backend_options or {}
        return asynclib.run(func, *args, **backend_options)  # type: ignore
    finally:
        if token:
            sniffio.current_async_library_cvar.reset(token)


@contextmanager
def claim_worker_thread(backend) -> typing.Generator[Any, None, None]:
    module = sys.modules['anyio._backends._' + backend]
    _local.current_async_module = module
    token = sniffio.current_async_library_cvar.set(backend)
    try:
        yield
    finally:
        sniffio.current_async_library_cvar.reset(token)
        del _local.current_async_module


def _get_asynclib(asynclib_name: Optional[str] = None):
    if asynclib_name is None:
        asynclib_name = sniffio.current_async_library()

    modulename = 'anyio._backends._' + asynclib_name
    try:
        return sys.modules[modulename]
    except KeyError:
        return import_module(modulename)


#
# Miscellaneous
#

def sleep(delay: float) -> Coroutine[Any, Any, None]:
    """
    Pause the current task for the specified duration.

    :param delay: the duration, in seconds

    """
    return _get_asynclib().sleep(delay)


def get_cancelled_exc_class() -> typing.Type[BaseException]:
    """Return the current async library's cancellation exception class."""
    return _get_asynclib().CancelledError


#
# Timeouts and cancellation
#

def open_cancel_scope(*, shield: bool = False) -> CancelScope:
    """
    Open a cancel scope.

    :param shield: ``True`` to shield the cancel scope from external cancellation
    :return: a cancel scope

    """
    return _get_asynclib().CancelScope(shield=shield)


def fail_after(delay: Optional[float], *,
               shield: bool = False) -> 'typing.AsyncContextManager[CancelScope]':
    """
    Create an async context manager which raises an exception if does not finish in time.

    :param delay: maximum allowed time (in seconds) before raising the exception, or ``None`` to
        disable the timeout
    :param shield: ``True`` to shield the cancel scope from external cancellation
    :return: an asynchronous context manager that yields a cancel scope
    :raises TimeoutError: if the block does not complete within the allotted time

    """
    if delay is None:
        return _get_asynclib().CancelScope(shield=shield)
    else:
        return _get_asynclib().fail_after(delay, shield=shield)


def move_on_after(delay: Optional[float], *,
                  shield: bool = False) -> 'typing.AsyncContextManager[CancelScope]':
    """
    Create an async context manager which is exited if it does not complete within the given time.

    :param delay: maximum allowed time (in seconds) before exiting the context block, or ``None``
        to disable the timeout
    :param shield: ``True`` to shield the cancel scope from external cancellation
    :return: an asynchronous context manager that yields a cancel scope

    """
    if delay is None:
        return _get_asynclib().CancelScope(shield=shield)
    else:
        return _get_asynclib().move_on_after(delay, shield=shield)


def current_effective_deadline() -> Coroutine[Any, Any, float]:
    """
    Return the nearest deadline among all the cancel scopes effective for the current task.

    :return: a clock value from the event loop's internal clock (``float('inf')`` if there is no
        deadline in effect)
    :rtype: float

    """
    return _get_asynclib().current_effective_deadline()


def current_time() -> Coroutine[Any, Any, float]:
    """
    Return the current value of the event loop's internal clock.

    :return the clock value (seconds)
    :rtype: float

    """
    return _get_asynclib().current_time()


#
# Task groups
#

def create_task_group() -> TaskGroup:
    """
    Create a task group.

    :return: a task group

    """
    return _get_asynclib().TaskGroup()


#
# Threads
#

def run_sync_in_worker_thread(func: Callable[..., T_Retval], *args, cancellable: bool = False,
                              limiter: Optional[CapacityLimiter] = None) -> Awaitable[T_Retval]:
    """
    Start a thread that calls the given function with the given arguments.

    If the ``cancellable`` option is enabled and the task waiting for its completion is cancelled,
    the thread will still run its course but its return value (or any raised exception) will be
    ignored.

    :param func: a callable
    :param args: positional arguments for the callable
    :param cancellable: ``True`` to allow cancellation of the operation
    :param limiter: capacity limiter to use to limit the total amount of threads running
        (if omitted, the default limiter is used)
    :return: an awaitable that yields the return value of the function.

    """
    return _get_asynclib().run_in_thread(func, *args, cancellable=cancellable, limiter=limiter)


def run_async_from_thread(func: Callable[..., Coroutine[Any, Any, T_Retval]], *args) -> T_Retval:
    """
    Call a coroutine function from a worker thread.

    :param func: a coroutine function
    :param args: positional arguments for the callable
    :return: the return value of the coroutine function

    """
    try:
        asynclib = _local.current_async_module
    except AttributeError:
        raise RuntimeError('This function can only be run from an AnyIO worker thread')

    return asynclib.run_async_from_thread(func, *args)


def current_default_worker_thread_limiter() -> CapacityLimiter:
    """
    Return the capacity limiter that is used by default to limit the number of concurrent threads.

    :return: a capacity limiter object

    """
    return _get_asynclib().current_default_thread_limiter()


def create_blocking_portal() -> BlockingPortal:
    """Create a portal for running functions in the event loop thread."""
    return _get_asynclib().BlockingPortal()


def start_blocking_portal(
        backend: str = BACKENDS[0],
        backend_options: Optional[Dict[str, Any]] = None) -> BlockingPortal:
    """
    Start a new event loop in a new thread and run a blocking portal in its main task.

    :param backend:
    :param backend_options:
    :return: a blocking portal object

    """
    async def run_portal():
        nonlocal portal
        async with create_blocking_portal() as portal:
            event.set()
            await portal.sleep_until_stopped()

    portal: Optional[BlockingPortal]
    event = threading.Event()
    kwargs = {'func': run_portal, 'backend': backend, 'backend_options': backend_options}
    thread = threading.Thread(target=run, kwargs=kwargs)
    thread.start()
    event.wait()
    return typing.cast(BlockingPortal, portal)


#
# Subprocesses
#

async def run_process(command: Union[str, Sequence[str]], *, input: Optional[bytes] = None,
                      stdout: int = PIPE, stderr: int = PIPE,
                      check: bool = True) -> CompletedProcess:
    """
    Run an external command in a subprocess and wait until it completes.

    .. seealso:: :func:`subprocess.run`

    :param command: either a string to pass to the shell, or an iterable of strings containing the
        executable name or path and its arguments
    :param input: bytes passed to the standard input of the subprocess
    :param stdout: either :data:`subprocess.PIPE` or :data:`subprocess.DEVNULL`
    :param stderr: one of :data:`subprocess.PIPE`, :data:`subprocess.DEVNULL` or
        :data:`subprocess.STDOUT`
    :param check: if ``True``, raise :exc:`~subprocess.CalledProcessError` if the process
        terminates with a return code other than 0
    :return: an object representing the completed process
    :raises CalledProcessError: if ``check`` is ``True`` and the process exits with a nonzero
        return code

    """
    async def drain_stream(stream, index):
        chunks = [chunk async for chunk in stream]
        stream_contents[index] = b''.join(chunks)

    process = await open_process(command, stdin=PIPE if input else DEVNULL, stdout=stdout,
                                 stderr=stderr)
    stream_contents = [None, None]
    try:
        async with create_task_group() as tg:
            if process.stdout:
                await tg.spawn(drain_stream, process.stdout, 0)
            if process.stderr:
                await tg.spawn(drain_stream, process.stderr, 1)
            if process.stdin and input:
                await process.stdin.send(input)
                await process.stdin.aclose()

            await process.wait()
    except BaseException:
        process.kill()
        raise

    output, errors = stream_contents
    if check and process.returncode != 0:
        raise CalledProcessError(typing.cast(int, process.returncode), command, output, errors)

    return CompletedProcess(command, typing.cast(int, process.returncode), output, errors)


def open_process(command: Union[str, Sequence[str]], *, stdin: int = PIPE,
                 stdout: int = PIPE, stderr: int = PIPE) -> Coroutine[Any, Any, AsyncProcess]:
    """
    Start an external command in a subprocess.

    .. seealso:: :class:`subprocess.Popen`

    :param command: either a string to pass to the shell, or an iterable of strings containing the
        executable name or path and its arguments
    :param stdin: either :data:`subprocess.PIPE` or :data:`subprocess.DEVNULL`
    :param stdout: either :data:`subprocess.PIPE` or :data:`subprocess.DEVNULL`
    :param stderr: one of :data:`subprocess.PIPE`, :data:`subprocess.DEVNULL` or
        :data:`subprocess.STDOUT`
    :return: an asynchronous process object

    """
    shell = isinstance(command, str)
    return _get_asynclib().open_process(command, shell=shell, stdin=stdin, stdout=stdout,
                                        stderr=stderr)


#
# Async file I/O
#


async def open_file(file: Union[str, 'os.PathLike', int], mode: str = 'r', buffering: int = -1,
                    encoding: Optional[str] = None, errors: Optional[str] = None,
                    newline: Optional[str] = None, closefd: bool = True,
                    opener: Optional[Callable] = None) -> AsyncFile:
    """
    Open a file asynchronously.

    The arguments are exactly the same as for the builtin :func:`open`.

    :return: an asynchronous file object

    """
    fp = await run_sync_in_worker_thread(open, file, mode, buffering, encoding, errors, newline,
                                         closefd, opener)
    return AsyncFile(fp)


#
# Sockets and networking
#

async def connect_tcp(
    address: IPAddressType, port: int, *, bind_host: Optional[IPAddressType] = None,
    bind_port: Optional[int] = None, happy_eyeballs_delay: float = 0.25
) -> TCPSocketStream:
    """
    Connect to a host using the TCP protocol.

    This function implements the stateless version of the Happy Eyeballs algorithm (RFC 6555).
    If ``address`` is a host name that resolves to multiple IP addresses, each one is tried until
    one connection attempt succeeds. If the first attempt does not connected within 250
    milliseconds, a second attempt is started using the next address in the list, and so on.
    For IPv6 enabled systems, IPv6 addresses are tried first.

    :param address: the IP address or host name to connect to
    :param port: port on the target host to connect to
    :param bind_host: the interface address or name to bind the socket to before connecting
    :param bind_port: the port to bind the socket to before connecting
    :param happy_eyeballs_delay: delay (in seconds) before starting the next connection attempt
    :return: a socket stream object
    :raises OSError: if the connection attempt fails

    """
    # Placed here due to https://github.com/python/mypy/issues/7057
    stream: Optional[TCPSocketStream] = None

    async def try_connect(af: int, sa: tuple, event: Event):
        nonlocal stream
        try:
            raw_socket = socket.socket(af, socket.SOCK_STREAM)
        except OSError as exc:
            oserrors.append(exc)
            await event.set()
            return

        try:
            raw_socket.setblocking(False)
            raw_socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            if interface is not None and bind_port is not None:
                raw_socket.bind((interface, bind_port))

            result = await asynclib.connect_tcp(raw_socket, sa)
            if stream is None:
                stream = result
                await tg.cancel_scope.cancel()
        except OSError as exc:
            oserrors.append(exc)
            raw_socket.close()
        except BaseException:
            raw_socket.close()
            raise
        finally:
            await event.set()

    asynclib = _get_asynclib()
    interface: Optional[str] = None
    family: int = 0
    if bind_host:
        interface, family, _v6only = await get_bind_address(bind_host)

    target_addrs = await run_sync_in_worker_thread(socket.getaddrinfo, address, port, family,
                                                   socket.SOCK_STREAM, cancellable=True)
    oserrors: List[OSError] = []
    async with create_task_group() as tg:
        for i, (af, *rest, sa) in enumerate(target_addrs):
            event = create_event()
            await tg.spawn(try_connect, af, sa, event)
            async with move_on_after(happy_eyeballs_delay):
                await event.wait()

    if stream is None:
        cause = oserrors[0] if len(oserrors) == 1 else asynclib.ExceptionGroup(oserrors)
        raise OSError('All connection attempts failed') from cause

    return stream


async def connect_unix(path: Union[str, 'os.PathLike']) -> UNIXSocketStream:
    """
    Connect to the given UNIX socket.

    Not available on Windows.

    :param path: path to the socket
    :return: a socket stream object

    """
    raw_socket = socket.socket(socket.AF_UNIX)
    raw_socket.setblocking(False)
    try:
        return await _get_asynclib().connect_unix(raw_socket, str(path))
    except BaseException:
        raw_socket.close()
        raise


async def create_tcp_server(port: int = 0,
                            interface: Optional[IPAddressType] = None) -> TCPListener:
    """
    Start a TCP socket server.

    :param port: port number to listen on
    :param interface: IP address of the interface to listen on. If omitted, listen on all IPv4
        and IPv6 interfaces. To listen on all interfaces on a specific address family, use
        ``0.0.0.0`` for IPv4 or ``::`` for IPv6.
    :param ssl_context: an SSL context object for TLS negotiation
    :param autostart_tls: automatically do the TLS handshake on new connections if ``ssl_context``
        has been provided
    :param tls_standard_compatible: If ``True``, performs the TLS shutdown handshake before closing
        a connected stream and requires that the client does this as well. Otherwise,
        :exc:`~ssl.SSLEOFError` may be raised during reads from a client stream.
        Some protocols, such as HTTP, require this option to be ``False``.
        See :meth:`~ssl.SSLContext.wrap_socket` for details.
    :return: a server object

    """
    interface, family, v6only = await get_bind_address(interface)
    raw_socket = socket.socket(family)
    raw_socket.setblocking(False)
    raw_socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    # Enable/disable dual stack operation as needed
    if family == socket.AF_INET6:
        raw_socket.setsockopt(IPPROTO_IPV6, socket.IPV6_V6ONLY, v6only)

    try:
        if sys.platform == 'win32':
            raw_socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            raw_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        raw_socket.bind((interface or '', port))
        raw_socket.listen()
        return await _get_asynclib().create_tcp_listener(raw_socket)
    except BaseException:
        raw_socket.close()
        raise


async def create_unix_server(path: Union[str, 'os.PathLike'], *,
                             mode: Optional[int] = None) -> UNIXListener:
    """
    Start a UNIX socket server.

    Not available on Windows.

    :param path: path of the socket
    :param mode: permissions to set on the socket
    :return: a server object

    """
    raw_socket = socket.socket(socket.AF_UNIX)
    raw_socket.setblocking(False)
    try:
        raw_socket.bind(str(path))

        if mode is not None:
            os.chmod(path, mode)

        raw_socket.listen()
        return await _get_asynclib().create_unix_listener(raw_socket)
    except BaseException:
        raw_socket.close()
        raise


async def create_udp_socket(
    *, interface: Optional[IPAddressType] = None, port: Optional[int] = None,
    target_host: Optional[IPAddressType] = None, target_port: Optional[int] = None
) -> UDPSocket:
    """
    Create a UDP socket.

    If ``port`` has been given, the socket will be bound to this port on the local machine,
    making this socket suitable for providing UDP based services.

    :param interface: IP address of the interface to bind to
    :param port: port to bind to
    :param target_host: remote host to set as the default target
    :param target_port: port on the remote host to set as the default target
    :return: a UDP socket

    """
    if interface:
        interface, family, _v6only = await get_bind_address(interface)
    else:
        interface, family = None, 0

    if target_host:
        res = await run_sync_in_worker_thread(socket.getaddrinfo, target_host, target_port, family)
        if res:
            family, type_, proto, _cn, sa = res[0]
            target_host, target_port = sa[:2]
        else:
            raise ValueError('{!r} cannot be resolved to an IP address'.format(target_host))

    raw_socket = socket.socket(family=family, type=socket.SOCK_DGRAM)
    raw_socket.setblocking(False)
    try:
        if interface is not None or port is not None:
            raw_socket.bind((interface or '', port or 0))

        if target_host is not None and target_port is not None:
            raw_socket.connect((target_host, target_port))

        return await _get_asynclib().create_udp_socket(raw_socket)
    except BaseException:
        raw_socket.close()
        raise


def notify_socket_close(sock: socket.SocketType) -> Awaitable[None]:
    """
    Notify any relevant tasks that you are about to close a socket.

    This will cause :exc:`~anyio.exceptions.ClosedResourceError` to be raised on any task waiting
    for the socket to become readable or writable.

    :param sock: the socket to be closed after this

    """
    return _get_asynclib().notify_socket_close(sock)


#
# Synchronization
#

def create_lock() -> Lock:
    """
    Create an asynchronous lock.

    :return: a lock object

    """
    return _get_asynclib().Lock()


def create_condition(lock: Lock = None) -> Condition:
    """
    Create an asynchronous condition.

    :param lock: the lock to base the condition object on
    :return: a condition object

    """
    return _get_asynclib().Condition(lock=lock)


def create_event() -> Event:
    """
    Create an asynchronous event object.

    :return: an event object

    """
    return _get_asynclib().Event()


def create_semaphore(value: int) -> Semaphore:
    """
    Create an asynchronous semaphore.

    :param value: the semaphore's initial value
    :return: a semaphore object

    """
    return _get_asynclib().Semaphore(value)


def create_capacity_limiter(total_tokens: float) -> CapacityLimiter:
    """
    Create a capacity limiter.

    :param total_tokens: the total number of tokens available for borrowing (can be an integer or
        :data:`math.inf`)
    :return: a capacity limiter object

    """
    return _get_asynclib().CapacityLimiter(total_tokens)


@overload
def create_memory_stream(cls: Type[T_Item]) -> MessageStream[T_Item]:
    ...


@overload
def create_memory_stream() -> MessageStream[Any]:
    ...


def create_memory_stream(cls=None):
    """
    Creates an in-memory message stream.

    :param cls: type of the objects passed in the stream (for static type checking; omit to allow
        any type)
    """
    return _get_asynclib().MemoryMessageStream()


#
# Operating system signals
#

def open_signal_receiver(*signals: int) -> UnreliableReceiveMessageStream[int]:
    """
    Start receiving operating system signals.

    :param signals: signals to receive (e.g. ``signal.SIGINT``)
    :return: a stream of signal numbers

    .. warning:: Windows does not support signals natively so it is best to avoid relying on this
        in cross-platform applications.

    """
    return _get_asynclib().receive_signals(*signals)


#
# Testing and debugging
#

@attr.s(slots=True, frozen=True, auto_attribs=True, eq=False, hash=False)  # type: ignore
class TaskInfo:
    """Represents an asynchronous task."""

    id: int  #: the unique identifier of the task
    parent_id: Optional[int] = attr.ib(repr=False)  #: the identifier of the parent task, if any
    name: Optional[str]  #: the description of the task (if any)
    _coro_or_gen: Union[CoroutineType, GeneratorType] = attr.ib(repr=False)

    @property
    def frame(self) -> Optional[FrameType]:
        """Return the current frame of the task, or ``None`` if the task is not running."""
        if isinstance(self._coro_or_gen, CoroutineType):
            return self._coro_or_gen.cr_frame
        else:
            return self._coro_or_gen.gi_frame

    @property
    def waiting_on(self):
        """
        Return the object the task is currently awaiting on, or ``None`` if it's not awaiting on
        anything).

        """
        if isinstance(self._coro_or_gen, CoroutineType):
            return self._coro_or_gen.cr_await
        else:
            return self._coro_or_gen.gi_yieldfrom

    @property
    def running(self) -> bool:
        """``True`` if the task is currently running, ``False`` if not."""
        if isinstance(self._coro_or_gen, CoroutineType):
            return self._coro_or_gen.cr_running
        else:
            return self._coro_or_gen.gi_running

    def __eq__(self, other):
        if isinstance(other, TaskInfo):
            return self.id == other.id

        return NotImplemented

    def __hash__(self):
        return hash(self.id)


async def get_current_task() -> TaskInfo:
    """
    Return the current task.

    :return: a representation of the current task

    """
    return await _get_asynclib().get_current_task()


async def get_running_tasks() -> List[TaskInfo]:
    """
    Return a list of unfinished tasks in the current event loop.

    :return: a list of task info objects

    """
    return await _get_asynclib().get_running_tasks()


async def wait_all_tasks_blocked() -> None:
    """Wait until all other tasks are waiting for something."""
    await _get_asynclib().wait_all_tasks_blocked()


@contextmanager
def open_test_runner(backend: str, backend_options: Optional[Dict[str, Any]] = None) -> \
        typing.Generator[TestRunner, None, None]:
    asynclib = _get_asynclib(backend)
    token = None
    if sniffio.current_async_library_cvar.get(None) is None:
        # Since we're in control of the event loop, we can cache the name of the async library
        token = sniffio.current_async_library_cvar.set(backend)

    try:
        backend_options = backend_options or {}
        with asynclib.TestRunner(**backend_options) as runner:
            yield runner
    finally:
        if token:
            sniffio.current_async_library_cvar.reset(token)