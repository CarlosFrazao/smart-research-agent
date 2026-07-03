from __future__ import annotations
import asyncio, logging, re
from pathlib import Path
from typing import Any, Dict, List, Optional, Union
logger = logging.getLogger(__name__)

_REF_PATTERN = re.compile(r'(?:\[(\d+)\]|\b(\d{4})\b.*?(?:doi|arxiv|http))', re.IGNORECASE)


class PDFParser:
    '''
    Extrai texto, tabelas e referencias de arquivos PDF.
    Usa pdfplumber como backend principal; fallback gracioso sem crash.
    Plano SRA v6.0 item 3.4
    '''

    def __init__(self) -> None:
        self._available: Optional[bool] = None

    def _check_available(self) -> bool:
        if self._available is not None:
            return self._available
        try:
            import pdfplumber
            self._available = True
            logger.info('PDFParser: pdfplumber disponivel.')
        except ImportError:
            self._available = False
            logger.warning('PDFParser: pdfplumber nao instalado. Fallback ativo.')
        return self._available

    def _extract_references(self, text: str) -> List[str]:
        '''Extrai possiveis referencias bibliograficas do texto.'''
        refs: List[str] = []
        lines = text.split('\n')
        in_refs = False
        for line in lines:
            stripped = line.strip()
            if re.match(r'^(References|Bibliography|Bibliogr)', stripped, re.I):
                in_refs = True
                continue
            if in_refs and stripped:
                if re.match(r'^\d+\.|\[\\d+\]', stripped):
                    refs.append(stripped)
                elif len(stripped) > 20:
                    refs.append(stripped)
        return refs[:50]

    async def parse_file(self, path: Union[str, Path]) -> Dict[str, Any]:
        '''Extrai texto, tabelas e referencias de arquivo PDF.'''
        if not self._check_available():
            return {'text': '', 'tables': [], 'references': [], 'pages': 0, 'error': 'pdfplumber not installed'}
        try:
            result = await asyncio.to_thread(self._parse_sync, str(path))
            return result
        except Exception as exc:
            logger.warning(f'PDFParser.parse_file falhou em {path}: {exc}')
            return {'text': '', 'tables': [], 'references': [], 'pages': 0, 'error': str(exc)}

    def _parse_sync(self, path: str) -> Dict[str, Any]:
        import pdfplumber
        all_text: List[str] = []
        all_tables: List[List[List[str]]] = []
        num_pages = 0
        with pdfplumber.open(path) as pdf:
            num_pages = len(pdf.pages)
            for page in pdf.pages:
                text = page.extract_text() or ''
                all_text.append(text)
                tables = page.extract_tables() or []
                for tbl in tables:
                    cleaned = [[str(cell or '').strip() for cell in row] for row in tbl]
                    all_tables.append(cleaned)
        full_text = '\n'.join(all_text)
        refs = self._extract_references(full_text)
        return {
            'text': full_text,
            'tables': all_tables,
            'references': refs,
            'pages': num_pages,
            'error': None,
        }

    async def parse_bytes(self, pdf_bytes: bytes) -> Dict[str, Any]:
        '''Extrai texto e tabelas de bytes de PDF.'''
        if not self._check_available():
            return {'text': '', 'tables': [], 'references': [], 'pages': 0, 'error': 'pdfplumber not installed'}
        try:
            import io
            result = await asyncio.to_thread(self._parse_sync_bytes, pdf_bytes)
            return result
        except Exception as exc:
            logger.warning(f'PDFParser.parse_bytes falhou: {exc}')
            return {'text': '', 'tables': [], 'references': [], 'pages': 0, 'error': str(exc)}

    def _parse_sync_bytes(self, pdf_bytes: bytes) -> Dict[str, Any]:
        import pdfplumber, io
        all_text: List[str] = []
        all_tables: List[List[List[str]]] = []
        num_pages = 0
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            num_pages = len(pdf.pages)
            for page in pdf.pages:
                text = page.extract_text() or ''
                all_text.append(text)
                for tbl in (page.extract_tables() or []):
                    all_tables.append([[str(c or '').strip() for c in row] for row in tbl])
        full_text = '\n'.join(all_text)
        return {
            'text': full_text,
            'tables': all_tables,
            'references': self._extract_references(full_text),
            'pages': num_pages,
            'error': None,
        }