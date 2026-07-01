import asyncio
import aiohttp
import logging
from typing import Dict, Any, Optional, List
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

class ScrapingRaceClient:
    def __init__(self, firecrawl_client: Any, timeout: float = 35.0):
        self.firecrawl_client = firecrawl_client
        self.timeout = timeout

    async def scrape(self, url: str, formats: Optional[List[str]] = None) -> Dict[str, Any]:
        """
        Dispara requisições concorrentes para raspar a URL e retorna o primeiro sucesso válido.
        Cancela as conexões excedentes no mesmo instante.
        """
        formats = formats or ["markdown"]
        tasks = []
        
        # 1. Tarefa Competidora A: Firecrawl Local (Playwright com Stealth e Rotação de Proxies)
        tasks.append(asyncio.create_task(
            self._wrap_task("firecrawl", self._scrape_via_firecrawl(url, formats))
        ))
        
        # 2. Tarefa Competidora B: Requisição Direta Resiliente (aiohttp sem JS, ideal para páginas rápidas e estáticas)
        tasks.append(asyncio.create_task(
            self._wrap_task("direct_http", self._scrape_direct_http(url))
        ))
        
        # 3. Tarefa Competidora C: Jina Reader API (Gratuito, robusto, ideal para bypass de Cloudflare)
        tasks.append(asyncio.create_task(
            self._wrap_task("jina_reader", self._scrape_via_jina(url))
        ))
        
        result = {}
        winner_name = None
        
        try:
            for future in asyncio.as_completed(tasks, timeout=self.timeout):
                try:
                    task_result = await future
                    if task_result and task_result.get("success"):
                        content = task_result.get("markdown", "") or task_result.get("html", "")
                        if len(content.strip()) > 150:
                            result = task_result
                            winner_name = task_result.get("engine")
                            break
                except Exception as e:
                    logger.debug(f"Competidor da corrida falhou com exceção: {e}")
        except asyncio.TimeoutError:
            logger.warning(f"Timeout na corrida de scraping para {url}")
            
        # Cancela todos os competidores que ainda não concluíram
        for task in tasks:
            if not task.done():
                task.cancel()
                
        # Garante o encerramento seguro no event loop
        await asyncio.gather(*tasks, return_exceptions=True)
            
        if result:
            logger.info(f"🏆 Corrida de scraping concluída! Vencedor: {winner_name} para URL: {url}")
            return {
                "success": True,
                "markdown": result.get("markdown", ""),
                "content": result.get("markdown", ""),
                "metadata": {
                    "engine": winner_name,
                    "url": url,
                    "length": len(result.get("markdown", ""))
                }
            }
            
        # Se todos falharam na corrida, tenta um último fallback sequencial completo via Firecrawl simples
        logger.warning(f"⚠️ Todos os competidores rápidos da corrida falharam para {url}. Tentando fallback direto sequencial...")
        try:
            fallback_res = await self._scrape_via_firecrawl(url, formats)
            if fallback_res.get("success"):
                return {
                    "success": True,
                    "markdown": fallback_res.get("markdown", ""),
                    "content": fallback_res.get("markdown", ""),
                    "metadata": {
                        "engine": "firecrawl_fallback",
                        "url": url,
                        "length": len(fallback_res.get("markdown", ""))
                    }
                }
        except Exception as e:
            logger.error(f"Erro fatal no fallback final de scraping para {url}: {e}")
        return {"success": False, "markdown": "", "error": "Todos os motores de scraping falharam."}

    async def _wrap_task(self, name: str, coro) -> Dict[str, Any]:
        """Encapsulador para identificar qual motor venceu a corrida."""
        try:
            res = await coro
            if res:
                res["engine"] = name
                return res
        except Exception as e:
            logger.debug(f"Engine {name} falhou: {e}")
        return {"success": False, "engine": name}

    async def _scrape_via_firecrawl(self, url: str, formats: List[str]) -> Dict[str, Any]:
        """Interface com o FirecrawlClient existente (que chama o contêiner Docker)."""
        try:
            res = await self.firecrawl_client._direct_scrape_call(url, formats=formats)
            if res and (res.get("markdown") or res.get("content")):
                return {
                    "success": True,
                    "markdown": res.get("markdown") or res.get("content") or ""
                }
        except Exception as e:
            logger.debug(f"Erro no scraping via Firecrawl na corrida: {e}")
        return {"success": False}

    async def _scrape_direct_http(self, url: str) -> Dict[str, Any]:
        """Requisição HTTP direta usando aiohttp com cabeçalhos realistas."""
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
            "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache"
        }
        
        # Garante timeouts curtos para não atrasar a corrida
        timeout = aiohttp.ClientTimeout(total=15.0, connect=5.0)
        
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, headers=headers, ssl=False) as response:
                    if response.status == 200:
                        html = await response.text(errors="ignore")
                        
                        # Conversão básica inline de HTML para Markdown simplificado (fallback rápido)
                        # Isso poupa tempo de CPU e rede. Se precisar de markdown perfeito, o Firecrawl vence.
                        markdown = self._simple_html_to_markdown(html)
                        return {
                            "success": True,
                            "markdown": markdown,
                            "html": html
                        }
                    else:
                        logger.debug(f"Direct HTTP returned status {response.status} for {url}")
        except Exception as e:
            logger.debug(f"Direct HTTP failed in race: {e}")
            
        return {"success": False}

    def _simple_html_to_markdown(self, html: str) -> str:
        """Converte HTML básico para markdown semântico simplificado (remoção de tags de script/estilo)."""
        import re
        
        # Remove scripts e estilos
        text = re.sub(r'<script\b[^<]*(?:(?!<\/script>)<[^<]*)*<\/script>', '', html, flags=re.IGNORECASE)
        text = re.sub(r'<style\b[^<]*(?:(?!<\/style>)<[^<]*)*<\/style>', '', text, flags=re.IGNORECASE)
        
        # Converte títulos
        text = re.sub(r'<h[1-6]\b[^>]*>(.*?)</h[1-6]>', r'\n# \1\n', text, flags=re.IGNORECASE)
        
        # Converte parágrafos e quebras
        text = re.sub(r'<p\b[^>]*>(.*?)</p>', r'\n\1\n', text, flags=re.IGNORECASE)
        text = re.sub(r'<br\s*/?>', r'\n', text, flags=re.IGNORECASE)
        
        # Remove todas as outras tags HTML
        text = re.sub(r'<[^>]+>', ' ', text)
        
        # Normaliza espaços em branco e novas linhas
        text = re.sub(r'[ \t]+', ' ', text)
        text = re.sub(r'\n\s*\n+', '\n\n', text)
        
        return text.strip()

    async def _scrape_via_jina(self, url: str) -> Dict[str, Any]:
        """Raspagem via Jina Reader API (https://r.jina.ai/<url>)."""
        import sys
        import os
        if "pytest" in sys.modules or os.environ.get("PYTEST_CURRENT_TEST"):
            return {"success": False}
        jina_url = f"https://r.jina.ai/{url}"
        headers = {
            "Accept": "text/markdown",
            "User-Agent": "curl/8.6.0"
        }
        timeout = aiohttp.ClientTimeout(total=20.0, connect=5.0)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(jina_url, headers=headers) as response:
                    if response.status == 200:
                        content = await response.text()
                        if content and len(content.strip()) > 150:
                            return {
                                "success": True,
                                "markdown": content
                            }
                    logger.debug(f"Jina Reader returned status {response.status} for {url}")
        except Exception as e:
            logger.debug(f"Jina Reader failed in race: {e}")
        return {"success": False}
