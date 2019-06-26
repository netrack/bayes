import aiohttp
import aiohttp.web
import asyncio
import inspect
import logging
import pathlib
import pid

import polynome
import polynome.logging
import polynome.model
import polynome.storage.local

from aiojobs.aiohttp import setup
from functools import partial

from polynome import handlers
from polynome.storage import metadata
from polynome.middleware import route_to


class Server:
    """Serve the models."""

    @classmethod
    async def new(cls, data_root: str, pidfile: str,
                  host: str = None, port: str = None,
                  preload: bool = False,
                  close_timeout: int = 10,
                  strategy: str = polynome.model.Strategy.No.value,
                  logger: logging.Logger = polynome.logging.internal_logger):
        """Create new instance of the server."""

        self = cls()

        pidfile = pathlib.Path(pidfile)
        self.pid = pid.PidFile(piddir=pidfile.parent, pidname=pidfile.name)

        # Create a data root directory where all server data is persisted.
        data_root = pathlib.Path(data_root)
        data_root.mkdir(parents=True, exist_ok=True)

        # TODO: use different execution strategies for models and
        # fallback to the server-default execution strategy.
        loader = polynome.model.Loader(strategy=strategy, logger=logger)

        # A metadata storage with models details.
        meta = metadata.DB.new(path=data_root)

        storage = polynome.storage.local.FileSystem.new(
            path=data_root, meta=meta, loader=loader)

        models = await polynome.model.Cache.new(
            storage=storage, preload=preload)

        self.app = aiohttp.web.Application()

        self.app.on_startup.append(cls.app_callback(self.pid.create))
        self.app.on_response_prepare.append(self._prepare_response)
        self.app.on_shutdown.append(cls.app_callback(meta.close))
        self.app.on_shutdown.append(cls.app_callback(self.pid.close))

        route = partial(route_to, api_version=polynome.__apiversion__)

        self.app.add_routes([
            aiohttp.web.put(
                "/models/{name}/{tag}",
                route(handlers.Push(models))),
            aiohttp.web.delete(
                "/models/{name}/{tag}",
                route(handlers.Remove(models))),
            aiohttp.web.post(
                "/models/{name}/{tag}/predict",
                route(handlers.Predict(models))),

            aiohttp.web.get("/models", route(handlers.List(models))),
            aiohttp.web.get("/status", route(handlers.Status()))])

        setup(self.app)
        logger.info("Server initialization completed")

        return self

    async def _prepare_response(self, request, response):
        server = "Polynome/{0}".format(polynome.__version__)
        response.headers["Server"] = server

    @classmethod
    def start(cls, **kwargs):
        """Start serving the models.

        Run event loop to handle the requests.
        """
        argnames = inspect.getfullargspec(cls.new)
        kv = {k: v for k, v in kwargs.items() if k in argnames.args}

        async def application_factory():
            s = await cls.new(**kv)
            return s.app

        aiohttp.web.run_app(application_factory(), print=None,
                            host=kv.get("host"), port=kv.get("port"))

    @classmethod
    def app_callback(cls, awaitable):
        async def on_signal(app):
            coroutine = awaitable()
            if asyncio.iscoroutine(coroutine):
                await coroutine
        return on_signal