"""
Bot de Telegram: URL -> resolver.py -> manifiesto HLS (.m3u8) -> FFmpeg -> .mp4 -> Telegram.

Alcance: streams HLS sin cifrar (sin EXT-X-KEY) y accesibles públicamente.
Los manifiestos con cualquier tipo de cifrado se rechazan.

Requisitos:
    pip install "python-telegram-bot>=21" httpx m3u8
    ffmpeg y ffprobe en el PATH

Variables de entorno:
    BOT_TOKEN        token de @BotFather (obligatorio)
    ALLOWED_USERS    IDs de Telegram permitidos, separados por comas (recomendado)
    MAX_UPLOAD_MB    50 con la API en la nube; hasta 2000 con un Bot API server local
    MAX_JOBS         trabajos simultáneos de FFmpeg (por defecto 2)
"""

import asyncio
import ipaddress
import logging
import os
import socket
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import httpx
import m3u8
from telegram import Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters

from resolver import ResolveError, resolve_url

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("hls-bot")

TOKEN = os.environ["BOT_TOKEN"]
ALLOWED_USERS = {int(x) for x in os.environ.get("ALLOWED_USERS", "").split(",") if x.strip()}
MAX_UPLOAD = int(os.environ.get("MAX_UPLOAD_MB", "50")) * 1024 * 1024
MAX_DURATION = 2 * 60 * 60          # segundos de vídeo aceptados por trabajo
FFMPEG_TIMEOUT = 20 * 60            # segundos de procesamiento como máximo
JOBS = asyncio.Semaphore(int(os.environ.get("MAX_JOBS", "2")))


class JobError(Exception):
    """Error que se le muestra tal cual al usuario."""


# ---------------------------------------------------------------- 1. Validación

def assert_public_url(url: str) -> None:
    """Evita SSRF: solo http(s) hacia IPs públicas."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise JobError("La URL debe ser http(s).")
    try:
        infos = socket.getaddrinfo(parsed.hostname, parsed.port or 443)
    except socket.gaierror:
        raise JobError("No se pudo resolver el dominio.")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise JobError("La URL apunta a una red privada.")


# ---------------------------------------------------------------- 2. Resolución

async def fetch_playlist(client: httpx.AsyncClient, url: str) -> m3u8.M3U8:
    assert_public_url(url)
    resp = await client.get(url)
    resp.raise_for_status()
    assert_public_url(str(resp.url))            # revalida tras redirecciones
    if "#EXTM3U" not in resp.text[:100]:
        raise JobError("Eso no parece un manifiesto HLS.")
    return m3u8.loads(resp.text, uri=str(resp.url))


def check_unencrypted(playlist: m3u8.M3U8) -> None:
    for key in playlist.keys:
        if key and key.method and key.method.upper() != "NONE":
            raise JobError("El stream está cifrado; este bot solo procesa HLS sin cifrar.")


async def inspect_manifest(url: str) -> tuple[str, float]:
    """Devuelve (URL del media playlist elegido, duración en segundos)."""
    async with httpx.AsyncClient(timeout=15, follow_redirects=True, max_redirects=3) as client:
        top = await fetch_playlist(client, url)

        if not top.is_variant:
            check_unencrypted(top)
            return url, sum(s.duration or 0 for s in top.segments)

        # Master playlist: ordenar variantes por bitrate.
        variants = sorted(top.playlists, key=lambda p: p.stream_info.bandwidth or 0)
        if not variants:
            raise JobError("El manifiesto maestro no tiene variantes.")

        # Todas las variantes duran lo mismo; mide con la más ligera.
        lowest = await fetch_playlist(client, variants[0].absolute_uri)
        check_unencrypted(lowest)
        duration = sum(s.duration or 0 for s in lowest.segments)

        # La de mayor calidad cuyo tamaño estimado quepa en el límite de subida.
        chosen = variants[0]
        for v in variants:
            estimated = (v.stream_info.bandwidth or 0) / 8 * duration
            if estimated <= MAX_UPLOAD * 0.9:
                chosen = v
        if chosen is not variants[0]:
            check_unencrypted(await fetch_playlist(client, chosen.absolute_uri))
        return chosen.absolute_uri, duration


# ---------------------------------------------------------------- 3. FFmpeg

async def run(cmd: list[str], timeout: int) -> None:
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
    )
    try:
        _, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise JobError("El procesamiento tardó demasiado.")
    if proc.returncode != 0:
        log.error("ffmpeg falló: %s", err.decode(errors="replace")[-2000:])
        raise JobError("FFmpeg no pudo procesar el stream.")


async def remux_to_mp4(media_url: str, out: Path) -> None:
    """Descarga los segmentos y los une en MP4 sin recodificar."""
    await run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-protocol_whitelist", "https,http,tls,tcp",   # sin file/crypto: solo red, sin cifrado
        "-rw_timeout", "15000000",                       # 15 s sin datos = fallo
        "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5",
        "-i", media_url,
        "-map", "0:v:0?", "-map", "0:a:0?",              # primera pista de vídeo y audio
        "-c", "copy",                                     # remux, no recodifica
        "-bsf:a", "aac_adtstoasc",                        # ADTS (TS) -> ASC (MP4)
        "-movflags", "+faststart",                        # moov al inicio: reproducible en streaming
        str(out),
    ], FFMPEG_TIMEOUT)


async def split_if_needed(src: Path, duration: float) -> list[Path]:
    size = src.stat().st_size
    if size <= MAX_UPLOAD:
        return [src]
    # Duración por parte proporcional al tamaño, con margen.
    part_seconds = max(10, int(duration * (MAX_UPLOAD / size) * 0.85))
    pattern = src.with_name("part_%03d.mp4")
    await run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-i", str(src), "-c", "copy", "-map", "0",
        "-f", "segment", "-segment_time", str(part_seconds),
        "-reset_timestamps", "1", "-segment_format_options", "movflags=+faststart",
        str(pattern),
    ], FFMPEG_TIMEOUT)
    parts = sorted(src.parent.glob("part_*.mp4"))
    if any(p.stat().st_size > MAX_UPLOAD for p in parts):
        raise JobError("Alguna parte sigue superando el límite (cortes por keyframe).")
    return parts


# ---------------------------------------------------------------- 4. Telegram

async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if ALLOWED_USERS and (not user or user.id not in ALLOWED_USERS):
        return
    url = (update.message.text or "").strip()
    status = await update.message.reply_text("En cola…")

    try:
        async with JOBS:
            # 1) URL de entrada -> URL del manifiesto (resolver.py)
            await status.edit_text("Resolviendo enlace…")
            assert_public_url(url)                       # SSRF: antes de cualquier petición
            manifest_url = await resolve_url(url)

            # 2) Manifiesto -> variante elegida + duración (validación de cifrado incluida)
            await status.edit_text("Leyendo el manifiesto…")
            media_url, duration = await inspect_manifest(manifest_url)
            if duration <= 0:
                raise JobError("El playlist no tiene segmentos.")
            if duration > MAX_DURATION:
                raise JobError("El vídeo supera la duración máxima permitida.")

            with tempfile.TemporaryDirectory(prefix="hls_") as tmp:   # se borra siempre
                out = Path(tmp) / "video.mp4"
                await status.edit_text(f"Descargando y uniendo segmentos ({duration/60:.1f} min)…")
                await remux_to_mp4(media_url, out)

                parts = await split_if_needed(out, duration)
                for i, part in enumerate(parts, 1):
                    await status.edit_text(f"Subiendo parte {i}/{len(parts)}…")
                    with part.open("rb") as f:
                        await update.message.reply_video(
                            video=f,
                            supports_streaming=True,
                            caption=f"Parte {i}/{len(parts)}" if len(parts) > 1 else None,
                        )
            await status.delete()

    except (JobError, ResolveError) as e:
        await status.edit_text(f"❌ {e}")
    except httpx.HTTPError as e:
        log.warning("HTTP: %s", e)
        await status.edit_text("❌ No se pudo descargar el manifiesto.")
    except Exception:
        log.exception("Error inesperado")
        await status.edit_text("❌ Error interno.")


def main() -> None:
    app = (
        Application.builder()
        .token(TOKEN)
        .read_timeout(120)
        .write_timeout(600)          # subidas grandes
        .concurrent_updates(True)    # varios usuarios a la vez; el semáforo limita FFmpeg
        .build()
    )
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex(r"^https?://\S+$"), handle_url))
    app.run_polling()


if __name__ == "__main__":
    main()
