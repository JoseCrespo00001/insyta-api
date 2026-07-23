"""Rate limiting básico (slowapi) — límites por IP, storage in-memory.

Alcanza para el deploy actual (un solo proceso uvicorn en el EC2). Si la API
pasa a múltiples réplicas/workers, mover el storage a Redis
(`Limiter(..., storage_uri=settings.redis_url)`) para que los contadores sean
compartidos.

Los límites se aplican por endpoint con `@limiter.limit("N/minute")` SOLO en
los POST que crean trabajo (bootstrap, audits, uploads) — nunca en GETs.
"""

from __future__ import annotations

from slowapi import Limiter
from slowapi.util import get_remote_address

# key_func = IP del cliente. In-memory: los contadores viven en el proceso.
limiter = Limiter(key_func=get_remote_address)
