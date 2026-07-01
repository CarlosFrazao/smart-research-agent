# test_sources.py — Script de diagnóstico de fontes corrigido
import asyncio
import json
import os
import sys

# Garante que 'src' esteja no PYTHONPATH
sys.path.insert(0, os.path.abspath('.'))

from src.config import Config
from src.search.github_searcher import GitHubSearcher
from src.search.reddit_searcher import RedditSearcher
from src.search.hn_searcher import HNSearcher
from src.search.awesome_searcher import AwesomeSearcher
from src.search.arxiv_searcher import ArxivSearcher
from src.search.producthunt_searcher import ProductHuntSearcher
from src.search.web_searcher import WebSearcher
from src.search.firecrawl_searcher import FirecrawlSearcher

async def test_all():
    config = Config()
    cfg = {
        "timeout": config.timeout_per_source,
        "max_results": config.max_results_per_source,
        "github_token": config.github_token,
        "producthunt_token": config.producthunt_token,
        "firecrawl_api_key": config.firecrawl_api_key,
        "firecrawl_base_url": config.firecrawl_base_url,
        "spider_api_key": config.spider_api_key,
        "spider_base_url": config.spider_base_url,
        "enabled": True,
        "steel_api_key": config.steel_api_key,
        "steel_base_url": config.steel_base_url,
    }
    
    query = 'AFFiNE open source'
    results = {}
    sources = {
        'hackernews': HNSearcher(cfg),
        'github': GitHubSearcher(cfg),
        'reddit': RedditSearcher(cfg),
        'arxiv': ArxivSearcher(cfg),
        'producthunt': ProductHuntSearcher(cfg),
        'awesome': AwesomeSearcher(cfg),
        'web': WebSearcher(cfg),
        'firecrawl': FirecrawlSearcher(cfg),
    }
    
    for name, searcher in sources.items():
        try:
            res = await searcher.search(query)
            results[name] = {'count': len(res), 'sample': res[0].__dict__ if res else None, 'error': None}
        except Exception as e:
            results[name] = {'count': 0, 'sample': None, 'error': str(e)}
            
    print(json.dumps(results, indent=2, default=str))
    print('\n=== RESUMO ===')
    for name, data in results.items():
        status = '[OK]' if data['count'] > 0 else '[FAIL]'
        print(f'  {status} {name}: {data["count"]} resultados')
        if data['error']:
            print(f'     Erro: {data["error"][:100]}')

if __name__ == '__main__':
    asyncio.run(test_all())
