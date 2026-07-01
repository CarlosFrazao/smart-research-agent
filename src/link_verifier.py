"""
LinkVerifier — Verificador de Links Concorrente

Valida assincronamente todas as URLs de citações extraídas dos resultados de busca.
Utiliza asyncio.Semaphore para limitar a concorrência e evitar abuso.
Penaliza a confiança do resultado caso ele apresente referências quebradas (404, timeouts, etc).
"""
import re
import asyncio
import httpx
import logging
from typing import List, Dict, Set, Tuple
from src.types import SearchResult

logger = logging.getLogger(__name__)

_URL_PATTERN = re.compile(r"https?://[^\s\"'<>]+")

class LinkVerifier:
    def __init__(self, max_concurrency: int = 10, timeout: float = 8.0):
        self.semaphore = asyncio.Semaphore(max_concurrency)
        self.timeout = timeout

    def extract_links(self, text: str) -> List[str]:
        """Extrai URLs de um bloco de texto e limpa pontuações finais."""
        if not text:
            return []
        raw_urls = _URL_PATTERN.findall(text)
        cleaned = []
        for url in raw_urls:
            # Limpa caracteres de pontuação colados no final da URL
            cleaned_url = url.rstrip(".,;:!?()[]{}")
            if cleaned_url not in cleaned:
                cleaned.append(cleaned_url)
        return cleaned

    async def verify_url(self, client: httpx.AsyncClient, url: str) -> Tuple[str, bool, int, str]:
        """
        Verifica a saúde de uma única URL usando o semáforo de concorrência.
        Retorna: (url, is_alive, status_code, error_message)
        """
        async with self.semaphore:
            try:
                # Tenta requisição HEAD primeiro (mais rápido e consome menos banda)
                response = await client.head(url, timeout=self.timeout, follow_redirects=True)
                
                # Se HEAD não for suportado (404, 405) ou der outro erro, tenta GET parcial
                if response.status_code in (404, 405):
                    response = await client.get(url, timeout=self.timeout, follow_redirects=True)
                
                is_alive = 200 <= response.status_code < 400
                return url, is_alive, response.status_code, ""
            except httpx.HTTPStatusError as e:
                return url, False, e.response.status_code, str(e)
            except httpx.TimeoutException:
                return url, False, 408, "Timeout"
            except Exception as e:
                return url, False, 0, str(e)

    async def verify_results(self, results: List[SearchResult]) -> List[SearchResult]:
        """
        Analisa um lote de SearchResult, extrai seus links citados, valida-os
        concorrentemente e aplica penalidades nos que tiverem links quebrados.
        """
        # 1. Coleta todas as URLs únicas citadas para evitar requisições duplicadas
        all_urls_to_verify: Set[str] = set()
        result_to_links_map: Dict[str, List[str]] = {}

        for result in results:
            content = result.description or ""
            # Também extrai links de citações já existentes se houver
            existing_citations = getattr(result, "citations", []) or []
            
            extracted = self.extract_links(content)
            combined_links = list(set(extracted + list(existing_citations)))
            
            # Filtra links internos ou de serviços comuns conhecidos
            valid_links = [l for l in combined_links if not l.startswith("http://localhost") and not l.startswith("http://127.0.0.1")]
            
            result_to_links_map[result.url] = valid_links
            all_urls_to_verify.update(valid_links)

        if not all_urls_to_verify:
            return results

        logger.info(f"LinkVerifier: Verificando {len(all_urls_to_verify)} URLs citadas concorrentemente...")

        # 2. Executa as validações HTTP em paralelo respeitando o semáforo
        # Usamos User-Agent realista para evitar bloqueios em sites protegidos
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        
        url_status_map: Dict[str, Tuple[bool, int, str]] = {}
        
        async with httpx.AsyncClient(headers=headers, verify=False) as client:
            tasks = [self.verify_url(client, url) for url in all_urls_to_verify]
            verifications = await asyncio.gather(*tasks)
            
            for url, is_alive, status, err in verifications:
                url_status_map[url] = (is_alive, status, err)
                if not is_alive:
                    logger.warning(f"LinkVerifier: Link quebrado detectado: {url} (Status {status} / {err})")

        # 3. Atualiza os SearchResults e aplica penalidades
        for result in results:
            links = result_to_links_map.get(result.url, [])
            if not links:
                continue
                
            dead_links = []
            alive_links = []
            
            for url in links:
                is_alive, status, err = url_status_map.get(url, (False, 0, "Not Verified"))
                if is_alive:
                    alive_links.append(url)
                else:
                    dead_links.append(url)

            # Define as citações válidas atualizadas
            result.citations = alive_links

            # Se houver links mortos, aplica penalidade no score
            if dead_links:
                result.metrics["dead_links"] = dead_links
                penalty = 0.15 if len(dead_links) > 1 else 0.08
                result.confidence_score = round(max(0.0, result.confidence_score - penalty), 3)
                
                if "dead_links_detected" not in result.hallucination_flags:
                    result.hallucination_flags.append("dead_links_detected")
                    
                logger.info(f"LinkVerifier: Penalizado {result.url[:50]} com -{penalty} por conter links quebrados.")

        return results
