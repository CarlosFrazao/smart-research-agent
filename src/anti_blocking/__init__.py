"""
src/anti_blocking — Camada de Evasão e Anti-Detecção do SRA v6.

Componentes:
  BrowserFingerprintGenerator  → perfis de browser para evasão de fingerprinting
  TLSFingerprintClient         → curl_cffi para evadir inspeção TLS ao nível de socket
  CaptchaSolver                → integração assíncrona 2captcha/capsolver
  ResidentialProxyProvider     → formatação de URLs para proxies residenciais
"""
