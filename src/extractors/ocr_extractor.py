from __future__ import annotations

import io
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class OCRExtractor:
    '''
    Extrai texto de imagens usando Tesseract OCR.
    Fallback gracioso: retorna None quando Tesseract nao instalado.
    Plano SRA v6.0 item 3.2
    '''

    def __init__(self, lang: str = 'eng', tesseract_cmd: str | None = None) -> None:
        self._lang = lang
        self._available: bool | None = None
        self._tesseract_cmd = tesseract_cmd

    def _check_available(self) -> bool:
        if self._available is not None:
            return self._available
        try:
            import pytesseract
            if self._tesseract_cmd:
                pytesseract.pytesseract.tesseract_cmd = self._tesseract_cmd
            pytesseract.get_tesseract_version()
            self._available = True
            logger.info('OCRExtractor: Tesseract disponivel.')
        except Exception as exc:
            self._available = False
            logger.warning(f'OCRExtractor: Tesseract indisponivel ({exc}). Fallback ativo.')
        return self._available

    async def extract_from_bytes(self, image_bytes: bytes, mime_type: str = 'image/png') -> str | None:
        '''Extrai texto de bytes de imagem. Retorna None se Tesseract nao instalado.'''
        if not self._check_available():
            return None
        try:
            import asyncio

            import pytesseract
            from PIL import Image
            img = Image.open(io.BytesIO(image_bytes))
            text: str = await asyncio.to_thread(
                pytesseract.image_to_string, img, lang=self._lang
            )
            return text.strip() or None
        except Exception as exc:
            logger.warning(f'OCRExtractor.extract_from_bytes falhou: {exc}')
            return None

    async def extract_from_file(self, path: str | Path) -> str | None:
        '''Extrai texto de arquivo de imagem no disco.'''
        if not self._check_available():
            return None
        try:
            import asyncio

            import pytesseract
            from PIL import Image
            img = Image.open(str(path))
            text: str = await asyncio.to_thread(
                pytesseract.image_to_string, img, lang=self._lang
            )
            return text.strip() or None
        except Exception as exc:
            logger.warning(f'OCRExtractor.extract_from_file falhou em {path}: {exc}')
            return None

    async def extract_from_url(self, url: str, session: object | None = None) -> str | None:
        '''Baixa imagem da URL e extrai texto.'''
        if not self._check_available():
            return None
        try:
            import asyncio
            import urllib.request
            data: bytes = await asyncio.to_thread(lambda: urllib.request.urlopen(url, timeout=15).read())
            return await self.extract_from_bytes(data)
        except Exception as exc:
            logger.warning(f'OCRExtractor.extract_from_url falhou em {url}: {exc}')
            return None