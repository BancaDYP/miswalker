"""
resolver.py — Resolución modular de URL de entrada -> URL de manifiesto HLS (.m3u8).

Cada resolver sabe atender uno o varios dominios. El registro elige el primero
que acepte la URL recibida. Añadir una fuente nueva es escribir una clase que
herede de Resolver y registrarla con @register; el bot y el pipeline de FFmpeg
no cambian.

Diseñado para fuentes legítimas: manifiestos HLS directos y backends propios
que exponen una API documentada y a la que accedes con tu propio token.
"""

from __future__ import annotations

import os
import re
from abc import ABC, abstractmethod
from urllib.parse import urlparse

import httpx


class ResolveError(Exception):
    """La URL no se pudo resolver a un manifiesto."""


class Resolver(ABC):
    """Contrato común. Un resolver acepta una URL y devuelve un .m3u8."""

    @abstractmethod
    def matches(self, url: str) -> bool:
        """¿Este resolver sabe atender esta URL?"""

    @abstractmethod
    async def resolve(self, url: str, client: httpx.AsyncClient) -> str:
        """Devuelve la URL absoluta del manifiesto .m3u8."""


_REGISTRY: list[Resolver] = []


def register(cls: type[Resolver]) -> type[Resolver]:
    """Decorador: registra una instancia del resolver."""
    _REGISTRY.append(cls())
    return cls


async def resolve_url(url: str, client: httpx.AsyncClient | None = None) -> str:
    """Punto de entrada único que usa el bot. Prueba los resolvers en orden."""
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=15, follow_redirects=True, max_redirects=3)
    try:
        for resolver in _REGISTRY:
            if resolver.matches(url):
                return await resolver.resolve(url, client)
        raise ResolveError("Ningún resolver acepta esta URL.")
    finally:
        if owns_client:
            await client.aclose()


# --------------------------------------------------------------------------
# 1. Resolver genérico: la URL ya apunta a un .m3u8
# --------------------------------------------------------------------------

@register
class DirectManifestResolver(Resolver):
    """Para enlaces que ya son un manifiesto HLS."""

    def matches(self, url: str) -> bool:
        path = urlparse(url).path.lower()
        return path.endswith(".m3u8")

    async def resolve(self, url: str, client: httpx.AsyncClient) -> str:
        # Un HEAD basta para confirmar que existe y es alcanzable;
        # la validación de cifrado y variantes la hace el bot al leer el playlist.
        resp = await client.head(url)
        resp.raise_for_status()
        return str(resp.url)   # URL final tras redirecciones


# --------------------------------------------------------------------------
# 2. Resolver de backend propio: API documentada + token
# --------------------------------------------------------------------------

@register
class OwnBackendResolver(Resolver):
    """
    Ejemplo para TU propia plataforma.

    Supone una API documentada que, dado un id de contenido y tu token,
    devuelve JSON con la ruta del manifiesto. Ajusta el dominio, la ruta
    de la API, el nombre de los parámetros y la forma del JSON a tu backend real.

    Config por entorno:
        OWN_BACKEND_HOST     p. ej. "videos.mi-dominio.com"
        OWN_BACKEND_API      p. ej. "https://videos.mi-dominio.com/api/v1/manifest"
        OWN_BACKEND_TOKEN    tu token de API
    """

    HOST = os.environ.get("OWN_BACKEND_HOST", "videos.mi-dominio.com")
    API = os.environ.get("OWN_BACKEND_API", "https://videos.mi-dominio.com/api/v1/manifest")
    TOKEN = os.environ.get("OWN_BACKEND_TOKEN", "")

    # /play?bid=123&cid=456  ->  capturamos ambos ids
    _ID_RE = re.compile(r"[?&]bid=(?P<bid>\d+).*?[?&]cid=(?P<cid>\d+)")

    def matches(self, url: str) -> bool:
        return urlparse(url).hostname == self.HOST

    async def resolve(self, url: str, client: httpx.AsyncClient) -> str:
        match = self._ID_RE.search(url)
        if not match:
            raise ResolveError("No se encontraron los identificadores bid/cid en la URL.")
        if not self.TOKEN:
            raise ResolveError("Falta OWN_BACKEND_TOKEN para autenticar con el backend.")

        # Llamada a TU API, autenticada con TU token. Nada de simular un navegador.
        resp = await client.get(
            self.API,
            params={"bid": match["bid"], "cid": match["cid"]},
            headers={
                "Authorization": f"Bearer {self.TOKEN}",
                "Accept": "application/json",
            },
        )
        if resp.status_code == 401:
            raise ResolveError("El backend rechazó el token (401).")
        resp.raise_for_status()

        data = resp.json()
        # Forma de ejemplo: {"manifest_url": "https://.../master.m3u8"}
        manifest = data.get("manifest_url")
        if not manifest:
            raise ResolveError("La respuesta del backend no incluye manifest_url.")
        return manifest


# --------------------------------------------------------------------------
# Prueba manual
# --------------------------------------------------------------------------

if __name__ == "__main__":
    import asyncio
    import sys

    async def _main() -> None:
        if len(sys.argv) < 2:
            print("uso: python resolver.py <url>")
            return
        try:
            print(await resolve_url(sys.argv[1]))
        except (ResolveError, httpx.HTTPError) as e:
            print(f"error: {e}")

    asyncio.run(_main())
