class GatewayError(Exception):
    def __init__(self, status: int, code: str, message: str, *, diagnostic: str | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.diagnostic = diagnostic
        self.query_id: str | None = None


def query_timeout() -> GatewayError:
    return GatewayError(504, "QUERY_TIMEOUT", "The query exceeded its time limit.")


def result_limit() -> GatewayError:
    return GatewayError(422, "RESULT_LIMIT_EXCEEDED", "The result exceeded the response size limit.")
