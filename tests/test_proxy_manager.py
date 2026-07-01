import pytest
import asyncio
import json
import os
from unittest.mock import AsyncMock, patch, MagicMock
from src.proxy_manager import Proxy, ProxyManager, ProxyServer

@pytest.mark.asyncio
async def test_proxy_class():
    p = Proxy(ip="127.0.0.1", port=8080, username="user", password="pwd")
    assert p.ip == "127.0.0.1"
    assert p.port == 8080
    assert p.get_url() == "http://127.0.0.1:8080"
    assert p.proxy_type == "public"


@pytest.mark.asyncio
async def test_proxy_manager_load_local_pool(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    pool_file = config_dir / "proxy_pool.json"
    
    pool_data = {
        "proxies": [
            {"ip": "1.1.1.1", "port": 80, "type": "vps_ipv6"},
            {"ip": "2.2.2.2", "port": 8080, "type": "public"}
        ]
    }
    with open(pool_file, "w", encoding="utf-8") as f:
        json.dump(pool_data, f)

    manager = ProxyManager(config_dir=str(config_dir))
    assert len(manager.proxies) == 2
    assert manager.proxies[0].ip == "1.1.1.1"
    assert manager.proxies[0].proxy_type == "vps_ipv6"


@pytest.mark.asyncio
async def test_proxy_manager_harvest_and_validate():
    mock_text = "10.0.0.1:8080\n10.0.0.2:9090\ninvalid_line\n"
    manager = ProxyManager(config_dir="non_existent")
    manager.sources = ["http://mock-source.com"]
    
    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.text = AsyncMock(return_value=mock_text)
    
    mock_get = MagicMock()
    mock_get.__aenter__ = AsyncMock(return_value=mock_response)
    mock_get.__aexit__ = AsyncMock(return_value=None)
    
    with patch('aiohttp.ClientSession.get', return_value=mock_get):
        async def mock_validate_single(proxy, sem):
            if proxy.ip == "10.0.0.1":
                proxy.health_score = 100.0
                proxy.latency = 50.0
            else:
                proxy.health_score = 0.0

        with patch.object(ProxyManager, '_validate_single', side_effect=mock_validate_single):
            await manager.harvest_free_proxies()
            
            assert len(manager.proxies) == 1
            assert manager.proxies[0].ip == "10.0.0.1"
            assert manager.proxies[0].latency == 50.0


@pytest.mark.asyncio
async def test_proxy_server_direct_fallback():
    manager = ProxyManager(config_dir="non_existent")
    
    echo_received = asyncio.Event()
    received_data = []
    
    async def handle_echo(reader, writer):
        try:
            data = await reader.read(1024)
            received_data.append(data)
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 4\r\n\r\nECHO")
            await writer.drain()
            echo_received.set()
        finally:
            writer.close()
            
    echo_server = await asyncio.start_server(handle_echo, "127.0.0.1", 4001)
    
    proxy_server = ProxyServer(manager, host="127.0.0.1", port=3019)
    await proxy_server.start()
    
    try:
        client_reader, client_writer = await asyncio.open_connection("127.0.0.1", 3019)
        client_writer.write(b"CONNECT 127.0.0.1:4001 HTTP/1.1\r\n\r\n")
        await client_writer.drain()
        
        resp = await client_reader.readuntil(b"\r\n\r\n")
        assert b"200 Connection Established" in resp
        
        client_writer.write(b"HELLO")
        await client_writer.drain()
        
        await asyncio.wait_for(echo_received.wait(), timeout=5.0)
        assert received_data[0] == b"HELLO"
        
        echo_resp = await client_reader.read(1024)
        assert b"ECHO" in echo_resp
        
    finally:
        client_writer.close()
        proxy_server.server.close()
        await proxy_server.server.wait_closed()
        echo_server.close()
        await echo_server.wait_closed()
