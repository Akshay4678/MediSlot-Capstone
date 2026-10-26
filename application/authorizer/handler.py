"""Authorizer placeholder: do not attach until JWT verification is implemented."""
def lambda_handler(event, context):
    # Failing closed prevents unauthenticated access.
    raise Exception("Unauthorized: configure JWT signature/issuer/audience validation before use")
