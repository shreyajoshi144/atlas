FROM python:3.13-slim

# Hugging Face Spaces runs the container as uid 1000.
RUN useradd -m -u 1000 user
ENV HOME=/home/user PATH=/home/user/.local/bin:$PATH \
    PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    HF_HOME=/home/user/.cache/huggingface \
    ATLAS_DATA_DIR=/home/user/app/data \
    APP_ENV=production PORT=7860
WORKDIR /home/user/app

# Build deps for lxml/chromadb wheels are normally prebuilt; keep the image small.
USER user

# CPU-only PyTorch first (much smaller than the default CUDA build), then the rest.
COPY --chown=user requirements.txt .
RUN pip install --user torch --index-url https://download.pytorch.org/whl/cpu \
 && pip install --user -r requirements.txt

# Bake the embedding model into the image so startup never needs the network.
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')"

COPY --chown=user . .
RUN mkdir -p data/sqlite data/chroma data/logs

EXPOSE 7860
CMD ["sh", "deploy/start_space.sh"]
