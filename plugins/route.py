from aiohttp import web

routes = web.RouteTableDef()

@routes.get("/", allow_head=True)
async def root_route_handler(request):
    response = {"service": "TeleDrop", "status": "ok"}
    bot = request.app.get("bot")
    if bot is not None:
        response.update(bot.health_snapshot())
    return web.json_response(response)
