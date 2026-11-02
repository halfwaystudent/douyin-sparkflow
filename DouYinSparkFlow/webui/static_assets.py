"""Cache and compress public assets without caching authenticated responses."""

from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware


class PublicAssets(StaticFiles):
    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        if response.status_code in {200, 304}:
            response.headers["Cache-Control"] = "public, max-age=3600, must-revalidate"
        return response


def public_assets(directory):
    return GZipMiddleware(PublicAssets(directory=str(directory)), minimum_size=1024, compresslevel=5)
