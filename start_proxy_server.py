"""
Script de inicialização do ProxyServer local na porta 3017.
Inicia o servidor IMEDIATAMENTE e depois colhe proxies em background.
"""
import asyncio
import logging
import sys

sys.path.insert(0, 'src')
from proxy_manager import ProxyManager, ProxyServer

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

async def main():
    manager = ProxyManager()
    server = ProxyServer(manager, port=3017)

    # 1. Inicia o servidor PRIMEIRO (sem esperar proxies)
    await server.start()
    logging.info("ProxyServer UP em 0.0.0.0:3017 — aguardando conexões...")

    # 2. Colhe proxies em background sem bloquear o servidor
    async def harvest_loop():
        while True:
            try:
                logging.info("Iniciando colheita de proxies públicos...")
                await manager.harvest_free_proxies()
                logging.info(f"Pool atualizado: {len(manager.proxies)} proxies ativos.")
            except Exception as e:
                logging.warning(f"Erro na colheita: {e}")
            await asyncio.sleep(1200)  # Re-colhe a cada 20 minutos

    asyncio.create_task(harvest_loop())

    # Mantém o servidor vivo indefinidamente
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
