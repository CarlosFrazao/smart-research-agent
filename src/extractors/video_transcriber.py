from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)


class VideoTranscriber:
    '''
    Transcreve audio de videos usando OpenAI Whisper.
    Suporta URLs YouTube/web via yt-dlp e arquivos locais.
    Fallback gracioso: retorna None se Whisper nao instalado.
    Plano SRA v6.0 item 3.3
    '''

    def __init__(self, model_name: str = 'base', device: str = 'cpu') -> None:
        self._model_name = model_name
        self._device = device
        self._model = None
        self._model_loaded = False

    def _get_model(self) -> object | None:
        '''Carrega modelo Whisper de forma lazy. Retorna None se indisponivel.'''
        if self._model_loaded:
            return self._model
        self._model_loaded = True
        try:
            import whisper
            self._model = whisper.load_model(self._model_name, device=self._device)
            logger.info(f'VideoTranscriber: modelo Whisper {self._model_name!r} carregado.')
        except Exception as exc:
            self._model = None
            logger.warning(f'VideoTranscriber: Whisper indisponivel ({exc}). Fallback ativo.')
        return self._model

    async def _download_audio(self, url: str) -> str | None:
        '''Baixa audio de URL via yt-dlp. Retorna caminho do arquivo ou None.'''
        try:
            import yt_dlp
        except ImportError:
            logger.warning('VideoTranscriber: yt-dlp nao instalado. Instale com: pip install yt-dlp')
            return None
        with tempfile.TemporaryDirectory() as tmpdir:
            outtmpl = os.path.join(tmpdir, 'audio.%(ext)s')
            ydl_opts = {
                'format': 'bestaudio/best',
                'outtmpl': outtmpl,
                'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3'}],
                'quiet': True,
                'no_warnings': True,
            }
            try:
                await asyncio.to_thread(self._run_ydl, url, ydl_opts)
                mp3_path = os.path.join(tmpdir, 'audio.mp3')
                if os.path.exists(mp3_path):
                    return mp3_path
            except Exception as exc:
                logger.warning(f'VideoTranscriber: download de {url} falhou ({exc}).')
        return None

    @staticmethod
    def _run_ydl(url: str, opts: dict) -> None:
        import yt_dlp
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])

    async def transcribe_file(self, path: str | Path) -> str | None:
        '''Transcreve audio de arquivo local. Retorna texto ou None.'''
        model = await asyncio.to_thread(self._get_model)
        if model is None:
            return None
        try:
            result = await asyncio.to_thread(model.transcribe, str(path))
            return result.get('text', '').strip() or None
        except Exception as exc:
            logger.warning(f'VideoTranscriber.transcribe_file falhou em {path}: {exc}')
            return None

    async def transcribe_url(self, url: str) -> str | None:
        '''Baixa audio de URL e transcreve. Retorna texto ou None.'''
        model = await asyncio.to_thread(self._get_model)
        if model is None:
            return None
        with tempfile.TemporaryDirectory() as tmpdir:
            try:
                import yt_dlp
                outtmpl = os.path.join(tmpdir, 'audio.%(ext)s')
                ydl_opts = {
                    'format': 'bestaudio/best',
                    'outtmpl': outtmpl,
                    'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3'}],
                    'quiet': True,
                }
                await asyncio.to_thread(self._run_ydl, url, ydl_opts)
                mp3_path = os.path.join(tmpdir, 'audio.mp3')
                if os.path.exists(mp3_path):
                    result = await asyncio.to_thread(model.transcribe, mp3_path)
                    return result.get('text', '').strip() or None
            except ImportError:
                logger.warning('VideoTranscriber: yt-dlp nao instalado.')
            except Exception as exc:
                logger.warning(f'VideoTranscriber.transcribe_url falhou em {url}: {exc}')
        return None