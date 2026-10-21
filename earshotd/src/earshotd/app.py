"""FastAPI application: authenticated page upload."""

from __future__ import annotations

import asyncio
import ctypes
import errno
import os
import socket
import sys

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import pages, read_api, records, store
from .auth import require
from .config import Config, ConfigError, is_loopback, load
from .tls import server_context

UPLOAD_SLOTS = 2
UPLOAD_READ_SECONDS = 120


async def _read_body(request: Request) -> bytes:
    """The request body, refused with 413 past pages.MAX_BODY (before reading when the
    Content-Length already says so) and with 408 if it has not all arrived in time."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > pages.MAX_BODY:
        raise HTTPException(413, f"body over {pages.MAX_BODY} bytes")
    body = bytearray()
    try:
        async with asyncio.timeout(UPLOAD_READ_SECONDS):
            async for chunk in request.stream():
                body += chunk
                if len(body) > pages.MAX_BODY:
                    raise HTTPException(413, f"body over {pages.MAX_BODY} bytes")
    except TimeoutError:
        raise HTTPException(408, "body not received in time") from None
    return bytes(body)


def create_app(config: Config | None = None) -> FastAPI:
    config = config or load()
    store.init(config.db_path)
    # No slash redirects: behind the TLS proxy one points the client at plain http (#16).
    app = FastAPI(
        title="earshotd",
        openapi_url=None,
        docs_url=None,
        redoc_url=None,
        redirect_slashes=False,
    )
    app.state.config = config

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        return JSONResponse(
            {"error": exc.detail}, status_code=exc.status_code, headers=exc.headers
        )

    @app.exception_handler(RequestValidationError)
    async def _bad_input(_: Request, exc: RequestValidationError) -> JSONResponse:
        first = exc.errors()[0]
        where = ".".join(str(p) for p in first["loc"])
        return JSONResponse({"error": f"{where}: {first['msg']}"}, status_code=400)

    slots = asyncio.Semaphore(UPLOAD_SLOTS)

    @app.post("/v1/pages", dependencies=[Depends(require("upload"))])
    async def upload_pages(request: Request) -> JSONResponse:
        ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if ctype not in (pages.CONTENT_TYPE, pages.CONTENT_TYPE_V2):
            return JSONResponse(
                {
                    "error": f"Content-Type must be {pages.CONTENT_TYPE}"
                    f" or {pages.CONTENT_TYPE_V2}"
                },
                400,
            )
        if slots.locked():
            return JSONResponse(
                {"error": "uploads in flight; retry later"},
                503,
                headers={"Retry-After": "30"},
            )
        async with slots:
            body = await _read_body(request)
            if ctype == pages.CONTENT_TYPE_V2:
                return await _upload_v2(body)
            return await _upload_v1(body)

    async def _upload_v1(body: bytes) -> JSONResponse:
        try:
            parsed = pages.parse(body)
        except pages.MalformedBody as e:
            return JSONResponse({"error": str(e)}, 400)
        stored, duplicates = await run_in_threadpool(
            store.store_pages, config.db_path, parsed
        )
        return JSONResponse({"stored": stored, "duplicates": duplicates})

    async def _upload_v2(body: bytes) -> JSONResponse:
        try:
            raws = pages.parse_v2(body)
            stored, duplicates = await run_in_threadpool(
                store.store_pages_v2, config.db_path, raws
            )
        except (pages.MalformedBody, records.DecodeError) as e:
            return JSONResponse({"error": str(e)}, 400)
        return JSONResponse({"stored": stored, "duplicates": duplicates})

    app.include_router(read_api.router)

    @app.get(
        "/v1/openapi.json",
        include_in_schema=False,
        dependencies=[Depends(require("read"))],
    )
    async def openapi_document() -> JSONResponse:
        return JSONResponse(app.openapi())

    return app


_SD_LISTEN_FDS_START = 3  # sd_listen_fds(3): passed sockets start at fd 3
_SD_ENV = ("LISTEN_PID", "LISTEN_FDS", "LISTEN_FDNAMES")


# The name of the Sockets entry in the launchd plist (deploy/macos/launchd.py renders it).
LAUNCHD_SOCKET = "Listeners"


def _launchd_lib() -> ctypes.CDLL:
    """libSystem with launch_activate_socket and free typed."""
    lib = ctypes.CDLL(None)
    lib.launch_activate_socket.argtypes = [
        ctypes.c_char_p,
        ctypes.POINTER(ctypes.POINTER(ctypes.c_int)),
        ctypes.POINTER(ctypes.c_size_t),
    ]
    lib.launch_activate_socket.restype = ctypes.c_int
    lib.free.argtypes = [ctypes.c_void_p]
    lib.free.restype = None
    return lib


def _launchd_socket() -> int | None:
    """The one socket launchd holds for this job, or None when this is not a launchd socket job.

    launch_activate_socket answers ESRCH to a hand run and to a job with no Sockets: only that
    gives None. Any other error, or a count other than one, exits: the sandbox would deny a bind.
    """
    lib = _launchd_lib()
    fds = ctypes.POINTER(ctypes.c_int)()
    count = ctypes.c_size_t()
    err = lib.launch_activate_socket(
        LAUNCHD_SOCKET.encode(), ctypes.byref(fds), ctypes.byref(count)
    )
    if err == errno.ESRCH:
        return None
    if err:
        raise SystemExit(
            f"earshotd: launch_activate_socket({LAUNCHD_SOCKET!r}) failed: {err}"
            f" {os.strerror(err)}"
        )
    try:
        got = [fds[i] for i in range(count.value)]
    finally:
        lib.free(fds)
    if len(got) != 1:
        for fd in got:
            os.close(fd)
        raise SystemExit(
            f"earshotd: launch_activate_socket({LAUNCHD_SOCKET!r}) returned {len(got)} sockets;"
            " expected 1"
        )
    return got[0]


def _passed_socket(host: str, port: int) -> int | None:
    """The fd of the one socket the service manager passed for this process, else None.

    systemd: fd 3 when LISTEN_FDS is 1 and LISTEN_PID is ours. launchd, on macOS only: the
    socket launch_activate_socket returns. Exits with an error naming both addresses when the
    passed socket is bound elsewhere than `listen`.
    """
    if os.environ.get("LISTEN_FDS") == "1" and os.environ.get("LISTEN_PID") == str(
        os.getpid()
    ):
        fd, source = _SD_LISTEN_FDS_START, "systemd"
        rerun = "deploy/install.sh to render earshotd.socket"
    elif sys.platform == "darwin":
        launchd_fd = _launchd_socket()
        if launchd_fd is None:
            return None
        fd, source = launchd_fd, "launchd"
        rerun = "deploy/macos/install.sh to render the launchd plist"
    else:
        return None
    sock = socket.socket(fileno=fd)
    try:
        name = sock.getsockname()
    finally:
        sock.detach()  # the fd stays open for uvicorn
    wanted = (host.strip("[]"), port)
    if (name[0], name[1]) != wanted:
        raise SystemExit(
            f"earshotd: {source} passed a socket bound to {name[0]}:{name[1]} but listen is"
            f" {wanted[0]}:{wanted[1]}; rerun {rerun} from the config"
        )
    return fd


def _tls_options(config: Config, host: str) -> dict:
    """uvicorn.run's TLS argument: a mutual-TLS context when the config sets the TLS settings.

    Exits naming the file the context refuses. A network listen served plain by the
    insecure_plain_http opt-in says so on stderr; a loopback listen is plain without comment.
    """
    if config.tls:
        try:
            context = server_context(config)
        except ConfigError as e:
            raise SystemExit(f"earshotd: {e}") from None
        return {"ssl_context_factory": lambda *_: context}
    if config.insecure_plain_http and not is_loopback(host):
        print(
            f"earshotd: the proxy hop on {config.listen} is plain http by insecure_plain_http"
            " = true; only the firewall guards it",
            file=sys.stderr,
        )
    return {}


def main() -> None:
    import uvicorn

    from . import worker

    os.umask(0o077)  # first: everything this process creates is private
    config = load()
    host, port = config.host_port
    tls = _tls_options(
        config, host
    )  # before the models load: a bad key stops the start here
    fd = _passed_socket(host, port)
    app = create_app(config)
    app.state.worker_status = worker.start(config).status
    if fd is not None:
        for name in _SD_ENV:
            os.environ.pop(name, None)
        uvicorn.run(app, fd=fd, access_log=False, server_header=False, **tls)
    else:
        uvicorn.run(
            app, host=host, port=port, access_log=False, server_header=False, **tls
        )
