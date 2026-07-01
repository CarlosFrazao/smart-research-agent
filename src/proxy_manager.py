import asyncio
import aiohttp
import logging
import base64
import json
import os
import random
from datetime import datetime
from typing import List, Dict, Any, Optional

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

class Proxy:
    def __init__(self, ip: str, port: int, protocol: str = "http", username: Optional[str] = None, password: Optional[str] = None, proxy_type: str = "public"):
        self.ip = ip
        self.port = port
        self.protocol = protocol
        self.username = username
        self.password = password
        self.proxy_type = proxy_type  # "public" ou "vps_ipv6"
        self.health_score = 100.0
        self.latency = 0.0
        self.blocked_domains: List[str] = []
        self.last_checked = datetime.now()

    def get_url(self) -> str:
        return f"{self.protocol}://{self.ip}:{self.port}"


class ProxyManager:
    def __init__(self, config_dir: str = "config"):
        self.config_dir = config_dir
        self.proxies: List[Proxy] = []
        self.sources = [
            "https://api.proxyscrape.com/v2/?request=getproxies&protocol=http&timeout=10000&country=all&ssl=all&anonymity=all",
            "https://raw.githubusercontent.com/TheSpeedX/SOCKS-List/master/http.txt",
            "https://raw.githubusercontent.com/clarketm/proxy-list/master/proxy-list-raw.txt"
        ]
        self._load_local_pool()

    def _load_local_pool(self):
        """Carrega proxies locais (incluindo o túnel IPv6 do usuário se configurado)"""
        pool_path = os.path.join(self.config_dir, "proxy_pool.json")
        if os.path.exists(pool_path):
            try:
                with open(pool_path, "r", encoding="utf-8") as f:
                    config = json.load(f)
                    for item in config.get("proxies", []):
                        self.proxies.append(Proxy(
                            ip=item["ip"],
                            port=item["port"],
                            protocol=item.get("protocol", "http"),
                            username=item.get("username"),
                            password=item.get("password"),
                            proxy_type=item.get("type", "vps_ipv6")
                        ))
                logging.info(f"Loaded {len(self.proxies)} proxies from {pool_path}")
            except Exception as e:
                logging.error(f"Error loading proxy_pool.json: {e}")

    async def harvest_free_proxies(self):
        """Raspa listas públicas de proxies (Nexus Proxy Shield Harvester)"""
        logging.info("Starting free proxy harvesting...")
        new_count = 0
        async with aiohttp.ClientSession() as session:
            for url in self.sources:
                try:
                    async with session.get(url, timeout=10) as resp:
                        if resp.status == 200:
                            text = await resp.text()
                            new_count += self._parse_proxies(text)
                except Exception as e:
                    logging.warning(f"Error harvesting from {url}: {e}")
        
        logging.info(f"Harvested {new_count} new proxies. Starting validation...")
        await self.validate_all_proxies()

    def _parse_proxies(self, raw_text: str) -> int:
        added = 0
        for line in raw_text.splitlines():
            line = line.strip()
            if ":" in line and not line.startswith("#"):
                parts = line.split(":")
                if len(parts) >= 2:
                    try:
                        ip, port = parts[0], int(parts[1])
                        # Evita duplicados
                        if not any(p.ip == ip and p.port == port for p in self.proxies):
                            self.proxies.append(Proxy(ip, port, proxy_type="public"))
                            added += 1
                    except ValueError:
                        continue
        return added

    async def validate_all_proxies(self):
        """Valida proxies em paralelo contra o HTTPBin"""
        sem = asyncio.Semaphore(50)  # Limita concorrência para evitar rate limits locais
        tasks = [self._validate_single(p, sem) for p in self.proxies]
        await asyncio.gather(*tasks)
        
        # Filtra apenas proxies de alta qualidade (VPS configurados ou públicos com score > 50)
        original_len = len(self.proxies)
        self.proxies = [p for p in self.proxies if p.proxy_type == "vps_ipv6" or p.health_score > 50]
        logging.info(f"Proxy validation complete. Active pool: {len(self.proxies)} / {original_len} proxies.")

    async def _validate_single(self, proxy: Proxy, sem: asyncio.Semaphore):
        async with sem:
            test_url = "https://httpbin.org/ip"
            start_time = asyncio.get_event_loop().time()
            try:
                # Ignora validação SSL para proxies públicos para evitar falhas de certificado falsas
                connector = aiohttp.TCPConnector(ssl=False)
                async with aiohttp.ClientSession(connector=connector) as session:
                    async with session.get(
                        test_url, 
                        proxy=proxy.get_url(), 
                        timeout=aiohttp.ClientTimeout(total=8)
                    ) as resp:
                        if resp.status == 200:
                            proxy.latency = (asyncio.get_event_loop().time() - start_time) * 1000
                            proxy.health_score = 100.0
                            proxy.last_checked = datetime.now()
                            return
            except Exception:
                pass
            
            # Se for VPS configurado, reduz o score de forma gradual em vez de excluir imediatamente
            if proxy.proxy_type == "vps_ipv6":
                proxy.health_score = max(0.0, proxy.health_score - 20.0)
            else:
                proxy.health_score = 0.0

    def get_best_proxy(self, target_domain: str) -> Optional[Proxy]:
        # Filtra candidatos ativos
        candidates = [p for p in self.proxies if p.health_score > 30 and target_domain not in p.blocked_domains]
        if not candidates:
            return None
        
        # Prioriza túneis VPS dedicados IPv6 devido à estabilidade e velocidade
        vps_candidates = [p for p in candidates if p.proxy_type == "vps_ipv6"]
        if vps_candidates:
            return random.choice(vps_candidates)
            
        # Caso contrário, seleciona o proxy público de menor latência
        candidates.sort(key=lambda p: p.latency)
        return candidates[0]

    def report_result(self, proxy: Proxy, target_domain: str, success: bool, response_time: float = 0.0):
        if success:
            proxy.health_score = min(100.0, proxy.health_score + 5.0)
            if proxy.latency > 0:
                proxy.latency = (proxy.latency * 0.8) + (response_time * 0.2)
        else:
            proxy.health_score = max(0.0, proxy.health_score - 15.0)
            if target_domain not in proxy.blocked_domains:
                proxy.blocked_domains.append(target_domain)


class ProxyServer:
    def __init__(self, manager: ProxyManager, host: str = "0.0.0.0", port: int = 3017):
        self.manager = manager
        self.host = host
        self.port = port
        self.server = None

    async def start(self):
        self.server = await asyncio.start_server(self.handle_client, self.host, self.port)
        logging.info(f"Proxy Local Server rodando em http://{self.host}:{self.port}")

    async def handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            # Lê a requisição inicial (cabeçalho)
            data = await reader.readuntil(b'\r\n\r\n')
            header_lines = data.decode('utf-8', errors='ignore').split('\r\n')
            first_line = header_lines[0]
            
            words = first_line.split()
            if not words or len(words) < 2:
                writer.close()
                return
            
            method, target = words[0], words[1]
            
            if method.upper() == 'CONNECT':
                # HTTPS: tunelamento CONNECT
                if ':' in target:
                    host, port = target.split(':')
                    port = int(port)
                else:
                    host, port = target, 443
                await self.tunnel_https(reader, writer, host, port)
            else:
                # HTTP normal: GET/POST
                await self.tunnel_http(reader, writer, data, target)
        except Exception as e:
            logging.debug(f"Error serving client request: {e}")
            writer.close()

    async def tunnel_https(self, client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter, host: str, port: int):
        proxy = self.manager.get_best_proxy(host)
        
        # Conexão Direta (Direct Fallback) caso nenhum proxy esteja no pool
        if not proxy:
            try:
                remote_reader, remote_writer = await asyncio.open_connection(host, port)
                client_writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await client_writer.drain()
                await self.relay(client_reader, client_writer, remote_reader, remote_writer)
            except Exception as e:
                logging.debug(f"Direct connection failed for {host}:{port}: {e}")
                client_writer.close()
            return

        # Tunelamento através do Proxy escolhido
        start_time = asyncio.get_event_loop().time()
        try:
            remote_reader, remote_writer = await asyncio.open_connection(proxy.ip, proxy.port)
            
            # Formata o CONNECT para o proxy
            connect_req = f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n"
            if proxy.username and proxy.password:
                auth = base64.b64encode(f"{proxy.username}:{proxy.password}".encode()).decode()
                connect_req += f"Proxy-Authorization: Basic {auth}\r\n"
            connect_req += "\r\n"
            
            remote_writer.write(connect_req.encode())
            await remote_writer.drain()
            
            # Lê o retorno do handshake
            resp = await remote_reader.readuntil(b'\r\n\r\n')
            if b"200" in resp.split(b'\r\n')[0]:
                client_writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await client_writer.drain()
                
                await self.relay(client_reader, client_writer, remote_reader, remote_writer)
                duration = (asyncio.get_event_loop().time() - start_time) * 1000
                self.manager.report_result(proxy, host, success=True, response_time=duration)
            else:
                logging.warning(f"Proxy handshake failed for {proxy.get_url()}. Falling back to direct connection to {host}:{port}")
                self.manager.report_result(proxy, host, success=False)
                # Fallback Direto
                try:
                    remote_reader, remote_writer = await asyncio.open_connection(host, port)
                    client_writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                    await client_writer.drain()
                    await self.relay(client_reader, client_writer, remote_reader, remote_writer)
                except Exception as ex:
                    logging.error(f"Fallback direct connection failed to {host}:{port}: {ex}")
                    client_writer.close()
                    remote_writer.close()
        except Exception as e:
            logging.warning(f"Failed tunnel via proxy {proxy.get_url()} to {host}:{port}: {e}. Falling back to direct connection.")
            self.manager.report_result(proxy, host, success=False)
            # Fallback Direto
            try:
                remote_reader, remote_writer = await asyncio.open_connection(host, port)
                client_writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await client_writer.drain()
                await self.relay(client_reader, client_writer, remote_reader, remote_writer)
            except Exception as ex:
                logging.error(f"Fallback direct connection failed to {host}:{port}: {ex}")
                client_writer.close()

    async def tunnel_http(self, client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter, header_data: bytes, target_url: str):
        # Trata requisições HTTP redirecionando o tráfego do cabeçalho
        from urllib.parse import urlparse
        parsed = urlparse(target_url)
        host = parsed.netloc or parsed.path.split('/')[0]
        port = parsed.port or (80 if parsed.scheme == "http" else 443)
        
        proxy = self.manager.get_best_proxy(host)
        
        if not proxy:
            try:
                remote_reader, remote_writer = await asyncio.open_connection(host, port)
                remote_writer.write(header_data)
                await remote_writer.drain()
                await self.relay(client_reader, client_writer, remote_reader, remote_writer)
            except Exception:
                client_writer.close()
            return

        try:
            remote_reader, remote_writer = await asyncio.open_connection(proxy.ip, proxy.port)
            remote_writer.write(header_data)
            await remote_writer.drain()
            await self.relay(client_reader, client_writer, remote_reader, remote_writer)
            self.manager.report_result(proxy, host, success=True)
        except Exception:
            self.manager.report_result(proxy, host, success=False)
            client_writer.close()

    async def relay(self, client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter, remote_reader: asyncio.StreamReader, remote_writer: asyncio.StreamWriter):
        async def forward(src: asyncio.StreamReader, dst: asyncio.StreamWriter):
            try:
                while True:
                    buf = await src.read(4096)
                    if not buf:
                        break
                    dst.write(buf)
                    await dst.drain()
            except Exception:
                pass
            finally:
                dst.close()

        await asyncio.gather(
            forward(client_reader, remote_writer),
            forward(remote_reader, client_writer),
            return_exceptions=True
        )


async def main():
    manager = ProxyManager()
    server = ProxyServer(manager)
    
    # Inicia a colheita inicial e o servidor
    await manager.harvest_free_proxies()
    await server.start()
    
    # Loop periódico de colheita/validação (a cada 20 minutos)
    while True:
        await asyncio.sleep(1200)
        await manager.harvest_free_proxies()


if __name__ == "__main__":
    asyncio.run(main())
