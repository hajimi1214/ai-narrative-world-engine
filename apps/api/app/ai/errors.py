class ModelProviderError(Exception):
    def __init__(self, code: str, message: str = "Model provider request failed", upstream_status: int | None = None, usage: dict | None = None):
        super().__init__(message)
        self.code = code
        self.upstream_status = upstream_status
        self.usage = usage or {}


MODEL_AUTH_FAILED = "MODEL_AUTH_FAILED"
MODEL_RATE_LIMITED = "MODEL_RATE_LIMITED"
MODEL_TIMEOUT = "MODEL_TIMEOUT"
MODEL_UPSTREAM_ERROR = "MODEL_UPSTREAM_ERROR"
MODEL_OUTPUT_INVALID = "MODEL_OUTPUT_INVALID"
MODEL_PROVIDER_NOT_CONFIGURED = "MODEL_PROVIDER_NOT_CONFIGURED"
MODEL_PROVIDER_UNSUPPORTED = "MODEL_PROVIDER_UNSUPPORTED"
