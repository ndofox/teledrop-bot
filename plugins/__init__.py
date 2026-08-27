#(©) PythonBotz 
#@metaui





from aiohttp import web
from .route import routes


async def web_server(bot=None):
    web_app = web.Application(client_max_size=30000000)
    if bot is not None:
        web_app["bot"] = bot
    web_app.add_routes(routes)
    return web_app
