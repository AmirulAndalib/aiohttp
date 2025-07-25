import asyncio
import contextlib
import inspect
import warnings
from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    Iterator,
    Optional,
    Protocol,
    Type,
    TypeVar,
    Union,
    overload,
)

import pytest

from .test_utils import (
    BaseTestServer,
    RawTestServer,
    TestClient,
    TestServer,
    loop_context,
    setup_test_loop,
    teardown_test_loop,
    unused_port as _unused_port,
)
from .web import Application, BaseRequest, Request
from .web_protocol import _RequestHandler

try:
    import uvloop
except ImportError:
    uvloop = None  # type: ignore[assignment]

_Request = TypeVar("_Request", bound=BaseRequest)


class AiohttpClient(Protocol):
    # TODO(PY311): Use Unpack to specify ClientSession kwargs.
    @overload
    async def __call__(
        self,
        __param: Application,
        *,
        server_kwargs: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> TestClient[Request, Application]: ...
    @overload
    async def __call__(
        self,
        __param: BaseTestServer[_Request],
        *,
        server_kwargs: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> TestClient[_Request, None]: ...


class AiohttpServer(Protocol):
    def __call__(
        self, app: Application, *, port: Optional[int] = None, **kwargs: Any
    ) -> Awaitable[TestServer]: ...


class AiohttpRawServer(Protocol):
    def __call__(
        self,
        handler: _RequestHandler[BaseRequest],
        *,
        port: Optional[int] = None,
        **kwargs: Any,
    ) -> Awaitable[RawTestServer]: ...


def pytest_addoption(parser):  # type: ignore[no-untyped-def]
    parser.addoption(
        "--aiohttp-fast",
        action="store_true",
        default=False,
        help="run tests faster by disabling extra checks",
    )
    parser.addoption(
        "--aiohttp-loop",
        action="store",
        default="pyloop",
        help="run tests with specific loop: pyloop, uvloop or all",
    )
    parser.addoption(
        "--aiohttp-enable-loop-debug",
        action="store_true",
        default=False,
        help="enable event loop debug mode",
    )


def pytest_fixture_setup(fixturedef):  # type: ignore[no-untyped-def]
    """Set up pytest fixture.

    Allow fixtures to be coroutines. Run coroutine fixtures in an event loop.
    """
    func = fixturedef.func

    if inspect.isasyncgenfunction(func):
        # async generator fixture
        is_async_gen = True
    elif inspect.iscoroutinefunction(func):
        # regular async fixture
        is_async_gen = False
    else:
        # not an async fixture, nothing to do
        return

    strip_request = False
    if "request" not in fixturedef.argnames:
        fixturedef.argnames += ("request",)
        strip_request = True

    def wrapper(*args, **kwargs):  # type: ignore[no-untyped-def]
        request = kwargs["request"]
        if strip_request:
            del kwargs["request"]

        # if neither the fixture nor the test use the 'loop' fixture,
        # 'getfixturevalue' will fail because the test is not parameterized
        # (this can be removed someday if 'loop' is no longer parameterized)
        if "loop" not in request.fixturenames:
            raise Exception(
                "Asynchronous fixtures must depend on the 'loop' fixture or "
                "be used in tests depending from it."
            )

        _loop = request.getfixturevalue("loop")

        if is_async_gen:
            # for async generators, we need to advance the generator once,
            # then advance it again in a finalizer
            gen = func(*args, **kwargs)

            def finalizer():  # type: ignore[no-untyped-def]
                try:
                    return _loop.run_until_complete(gen.__anext__())
                except StopAsyncIteration:
                    pass

            request.addfinalizer(finalizer)
            return _loop.run_until_complete(gen.__anext__())
        else:
            return _loop.run_until_complete(func(*args, **kwargs))

    fixturedef.func = wrapper


@pytest.fixture
def fast(request: pytest.FixtureRequest) -> bool:
    """--fast config option"""
    return request.config.getoption("--aiohttp-fast")  # type: ignore[no-any-return]


@pytest.fixture
def loop_debug(request: pytest.FixtureRequest) -> bool:
    """--enable-loop-debug config option"""
    return request.config.getoption("--aiohttp-enable-loop-debug")  # type: ignore[no-any-return]


@contextlib.contextmanager
def _runtime_warning_context() -> Iterator[None]:
    """Context manager which checks for RuntimeWarnings.

    This exists specifically to
    avoid "coroutine 'X' was never awaited" warnings being missed.

    If RuntimeWarnings occur in the context a RuntimeError is raised.
    """
    with warnings.catch_warnings(record=True) as _warnings:
        yield
        rw = [
            "{w.filename}:{w.lineno}:{w.message}".format(w=w)
            for w in _warnings
            if w.category == RuntimeWarning
        ]
        if rw:
            raise RuntimeError(
                "{} Runtime Warning{},\n{}".format(
                    len(rw), "" if len(rw) == 1 else "s", "\n".join(rw)
                )
            )

    # Propagate warnings to pytest
    for msg in _warnings:
        warnings.showwarning(
            msg.message, msg.category, msg.filename, msg.lineno, msg.file, msg.line
        )


@contextlib.contextmanager
def _passthrough_loop_context(
    loop: Optional[asyncio.AbstractEventLoop], fast: bool = False
) -> Iterator[asyncio.AbstractEventLoop]:
    """Passthrough loop context.

    Sets up and tears down a loop unless one is passed in via the loop
    argument when it's passed straight through.
    """
    if loop:
        # loop already exists, pass it straight through
        yield loop
    else:
        # this shadows loop_context's standard behavior
        loop = setup_test_loop()
        yield loop
        teardown_test_loop(loop, fast=fast)


def pytest_pycollect_makeitem(collector, name, obj):  # type: ignore[no-untyped-def]
    """Fix pytest collecting for coroutines."""
    if collector.funcnamefilter(name) and inspect.iscoroutinefunction(obj):
        return list(collector._genfunctions(name, obj))


def pytest_pyfunc_call(pyfuncitem):  # type: ignore[no-untyped-def]
    """Run coroutines in an event loop instead of a normal function call."""
    fast = pyfuncitem.config.getoption("--aiohttp-fast")
    if inspect.iscoroutinefunction(pyfuncitem.function):
        existing_loop = (
            pyfuncitem.funcargs.get("proactor_loop")
            or pyfuncitem.funcargs.get("selector_loop")
            or pyfuncitem.funcargs.get("uvloop_loop")
            or pyfuncitem.funcargs.get("loop", None)
        )

        with _runtime_warning_context():
            with _passthrough_loop_context(existing_loop, fast=fast) as _loop:
                testargs = {
                    arg: pyfuncitem.funcargs[arg]
                    for arg in pyfuncitem._fixtureinfo.argnames
                }
                _loop.run_until_complete(pyfuncitem.obj(**testargs))

        return True


def pytest_generate_tests(metafunc):  # type: ignore[no-untyped-def]
    if "loop_factory" not in metafunc.fixturenames:
        return

    loops = metafunc.config.option.aiohttp_loop
    avail_factories: dict[str, Callable[[], asyncio.AbstractEventLoop]]
    avail_factories = {"pyloop": asyncio.new_event_loop}

    if uvloop is not None:
        avail_factories["uvloop"] = uvloop.new_event_loop

    if loops == "all":
        loops = "pyloop,uvloop?"

    factories = {}  # type: ignore[var-annotated]
    for name in loops.split(","):
        required = not name.endswith("?")
        name = name.strip(" ?")
        if name not in avail_factories:
            if required:
                raise ValueError(
                    "Unknown loop '%s', available loops: %s"
                    % (name, list(factories.keys()))
                )
            else:
                continue
        factories[name] = avail_factories[name]
    metafunc.parametrize(
        "loop_factory", list(factories.values()), ids=list(factories.keys())
    )


@pytest.fixture
def loop(
    loop_factory: Callable[[], asyncio.AbstractEventLoop],
    fast: bool,
    loop_debug: bool,
) -> Iterator[asyncio.AbstractEventLoop]:
    """Return an instance of the event loop."""
    with loop_context(loop_factory, fast=fast) as _loop:
        if loop_debug:
            _loop.set_debug(True)
        asyncio.set_event_loop(_loop)
        yield _loop


@pytest.fixture
def proactor_loop() -> Iterator[asyncio.AbstractEventLoop]:
    factory = asyncio.ProactorEventLoop  # type: ignore[attr-defined]

    with loop_context(factory) as _loop:
        asyncio.set_event_loop(_loop)
        yield _loop


@pytest.fixture
def aiohttp_unused_port() -> Callable[[], int]:
    """Return a port that is unused on the current host."""
    return _unused_port


@pytest.fixture
def aiohttp_server(loop: asyncio.AbstractEventLoop) -> Iterator[AiohttpServer]:
    """Factory to create a TestServer instance, given an app.

    aiohttp_server(app, **kwargs)
    """
    servers = []

    async def go(
        app: Application,
        *,
        host: str = "127.0.0.1",
        port: Optional[int] = None,
        **kwargs: Any,
    ) -> TestServer:
        server = TestServer(app, host=host, port=port)
        await server.start_server(**kwargs)
        servers.append(server)
        return server

    yield go

    async def finalize() -> None:
        while servers:
            await servers.pop().close()

    loop.run_until_complete(finalize())


@pytest.fixture
def aiohttp_raw_server(loop: asyncio.AbstractEventLoop) -> Iterator[AiohttpRawServer]:
    """Factory to create a RawTestServer instance, given a web handler.

    aiohttp_raw_server(handler, **kwargs)
    """
    servers = []

    async def go(
        handler: _RequestHandler[BaseRequest],
        *,
        port: Optional[int] = None,
        **kwargs: Any,
    ) -> RawTestServer:
        server = RawTestServer(handler, port=port)
        await server.start_server(**kwargs)
        servers.append(server)
        return server

    yield go

    async def finalize() -> None:
        while servers:
            await servers.pop().close()

    loop.run_until_complete(finalize())


@pytest.fixture
def aiohttp_client_cls() -> Type[TestClient[Any, Any]]:
    """
    Client class to use in ``aiohttp_client`` factory.

    Use it for passing custom ``TestClient`` implementations.

    Example::

       class MyClient(TestClient):
           async def login(self, *, user, pw):
               payload = {"username": user, "password": pw}
               return await self.post("/login", json=payload)

       @pytest.fixture
       def aiohttp_client_cls():
           return MyClient

       def test_login(aiohttp_client):
           app = web.Application()
           client = await aiohttp_client(app)
           await client.login(user="admin", pw="s3cr3t")

    """
    return TestClient


@pytest.fixture
def aiohttp_client(
    loop: asyncio.AbstractEventLoop, aiohttp_client_cls: Type[TestClient[Any, Any]]
) -> Iterator[AiohttpClient]:
    """Factory to create a TestClient instance.

    aiohttp_client(app, **kwargs)
    aiohttp_client(server, **kwargs)
    aiohttp_client(raw_server, **kwargs)
    """
    clients = []

    @overload
    async def go(
        __param: Application,
        *,
        server_kwargs: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> TestClient[Request, Application]: ...
    @overload
    async def go(
        __param: BaseTestServer[_Request],
        *,
        server_kwargs: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> TestClient[_Request, None]: ...
    async def go(
        __param: Union[Application, BaseTestServer[Any]],
        *,
        server_kwargs: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> TestClient[Any, Any]:
        # TODO(PY311): Use Unpack to specify ClientSession kwargs and server_kwargs.
        if isinstance(__param, Application):
            server_kwargs = server_kwargs or {}
            server = TestServer(__param, **server_kwargs)
            client = aiohttp_client_cls(server, **kwargs)
        elif isinstance(__param, BaseTestServer):
            client = aiohttp_client_cls(__param, **kwargs)
        else:
            raise ValueError("Unknown argument type: %r" % type(__param))

        await client.start_server()
        clients.append(client)
        return client

    yield go

    async def finalize() -> None:
        while clients:
            await clients.pop().close()

    loop.run_until_complete(finalize())
