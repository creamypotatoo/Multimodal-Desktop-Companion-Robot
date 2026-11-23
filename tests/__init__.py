import os

# Keep the suite hermetic: never contact a real Ollama server, even when the
# shell running the tests has ELI_OLLAMA_URL exported (e.g. on the Pi).
os.environ.pop("ELI_OLLAMA_URL", None)
