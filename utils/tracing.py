from opentelemetry import trace

def get_tracer(name: str):
    return trace.get_tracer(name)
