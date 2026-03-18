
with open("src/feishu/ws_client.py", "r") as f:
    content = f.read()

imports_to_add = """from ..utils.rate_limit import RateLimiter, RateLimitExceededException
from ..utils.circuit_breaker import CircuitBreaker, CircuitBreakerOpenException
"""

if "from ..utils.rate_limit" not in content:
    content = imports_to_add + content

with open("src/feishu/ws_client.py", "w") as f:
    f.write(content)
