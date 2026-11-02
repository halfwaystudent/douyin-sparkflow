"""Cache and compress public assets without caching authenticated responses."""

from hashlib import sha256
from pathlib import Path
from urllib.parse import parse_qs, quote

from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware


class AssetVersions:
    def __init__(self, directory):
        self.directory = Path(directory)
        self._versions = {}

    def digest(self, name):
        # Only application-owned top-level assets can receive a long cache life.
        if Path(name).name != name:
            return None
        path = self.directory / name
        try:
            stat = path.stat()
        except OSError:
            return None
        stamp = (stat.st_mtime_ns, stat.st_size, stat.st_ino)
        cached = self._versions.get(name)
        if cached is None or cached[0] != stamp:
            cached = (stamp, sha256(path.read_bytes()).hexdigest()[:16])
            self._versions[name] = cached
        return cached[1]

    def url(self, name):
        digest = self.digest(name)
        if digest is None:
            raise ValueError("Unknown public asset")
        return "/static/" + quote(name) + "?v=" + digest


class PublicAssets(StaticFiles):
    def __init__(self, *args, versions=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.versions = versions or AssetVersions(self.directory)

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        if response.status_code in {200, 304}:
            supplied = parse_qs(scope.get("query_string", b"").decode("ascii", errors="ignore")).get("v", [])
            current = self.versions.digest(path)
            if current and supplied == [current]:
                response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
            else:
                response.headers["Cache-Control"] = "public, max-age=0, must-revalidate"
        return response


def public_assets(directory, versions=None):
    return GZipMiddleware(PublicAssets(directory=str(directory), versions=versions), minimum_size=1024, compresslevel=5)
