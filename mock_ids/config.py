"""Settings and assumptions for the mock. Credentials are fake and for local use only."""

HOST = "127.0.0.1"          # never bind a mock with demo credentials to a public interface
HTTP_PORT = 8080
FIAS_PORT = 5100
TOKEN_TTL_SECONDS = 900

# Two demo API clients. "readonly-client" models a partner agreement that grants read access only.
CLIENTS = {
    "demo-client": {"secret": "demo-secret", "scopes": {"read", "write"}},
    "readonly-client": {"secret": "demo-secret", "scopes": {"read"}},
}

# ASSUMPTION: FIAS room-status codes. The real IDS Next mapping must be confirmed with the vendor.
HK_TO_FIAS = {"dirty": "1", "clean": "2", "inspected": "3", "out_of_order": "4"}
FIAS_TO_HK = {v: k for k, v in HK_TO_FIAS.items()}
