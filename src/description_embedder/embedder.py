"""
Thin wrapper around BAAI/bge-m3 for generating dense embeddings.

Uses sentence-transformers (compatible with transformers 5.x).
The model (~2 GB) is downloaded to ~/.cache/huggingface/hub/ on first run.

Only dense embeddings are used here — that is all we need for vector search.
"""
from sentence_transformers import SentenceTransformer


class Embedder:
    MODEL_NAME = "BAAI/bge-m3"

    def __init__(self):
        """
        Load the BGE-M3 model.
        sentence-transformers automatically uses GPU if available, CPU otherwise.
        """
        print(f"Loading embedding model: {self.MODEL_NAME} ...")
        self._model = SentenceTransformer(self.MODEL_NAME)
        print("Embedding model ready.")

    def embed(self, texts: list[str], batch_size: int = 32) -> list[list[float]]:
        """
        Embed a list of texts and return dense vectors.
        Returns a list of 1024-dimensional float lists — one per input text.
        """
        embeddings = self._model.encode(
            texts,
            batch_size=batch_size,
            show_progress_bar=False,
            normalize_embeddings=True,   # cosine similarity ready
        )
        return embeddings.tolist()
