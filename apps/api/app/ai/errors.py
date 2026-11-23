class ModelProviderError(Exception):
    def __init__(self, code: str, message: str = "Model provider request failed", upstream_status: int | None = None, usage: dict | None = None, accounted_calls: int = 1, retry_after: int | None = None):
        super().__init__(message)
        self.code = code
        self.upstream_status = upstream_status
        self.usage = usage or {}
        self.accounted_calls = max(0, int(accounted_calls))
        self.retry_after = max(0, int(retry_after)) if retry_after is not None else None


MODEL_AUTH_FAILED = "MODEL_AUTH_FAILED"
MODEL_RATE_LIMITED = "MODEL_RATE_LIMITED"
MODEL_TIMEOUT = "MODEL_TIMEOUT"
MODEL_UPSTREAM_ERROR = "MODEL_UPSTREAM_ERROR"
MODEL_OUTPUT_INVALID = "MODEL_OUTPUT_INVALID"
MODEL_PROVIDER_NOT_CONFIGURED = "MODEL_PROVIDER_NOT_CONFIGURED"
MODEL_PROVIDER_UNSUPPORTED = "MODEL_PROVIDER_UNSUPPORTED"
MODEL_CREDENTIAL_VAULT_NOT_CONFIGURED = "MODEL_CREDENTIAL_VAULT_NOT_CONFIGURED"
MODEL_CREDENTIAL_INVALID = "MODEL_CREDENTIAL_INVALID"
